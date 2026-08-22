"""From cleared order to tracked parcel.

Three responsibilities, in the order they happen.

**Batch.** Cleared orders become purchase orders grouped *per supplier*. Three
orders for the same supplier in one check-in window become one PO. That is
cheaper in supplier fees, and -- more to the point -- it is one thing for the
owner to look at instead of three, which is the whole promise of a twice-daily
check-in.

**Place.** An approved PO goes to the supplier, and every customer order it
covers advances together. Placement is idempotent: a retried webhook or a
double-clicked approve button cannot buy the same goods twice.

**Confirm.** Tracking comes back, gets attached to the order, and is pushed to
the marketplace the order came from. Shipping late is what actually costs a
dropshipper their account, so :meth:`FulfillmentEngine.late_orders` flags
overdue orders against the promise made at listing time -- before the
marketplace's own metrics notice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .audit import AuditLog
from .channels import ChannelConnector, ChannelError
from .domain import (
    Channel,
    Money,
    Order,
    OrderState,
    POLine,
    POState,
    PurchaseOrder,
    Shipment,
    StorefrontContext,
)
from .orders import OrderPipeline
from .policy import AutomationPolicy
from .security import IdempotencyGuard, idempotency_key


@dataclass(frozen=True)
class LateOrder:
    """An order past the delivery window we promised the customer."""

    order: Order
    promised_by: datetime
    days_late: int

    def describe(self) -> str:
        return (
            f"{self.order.channel.label} {self.order.external_id} is {self.days_late} day(s) "
            f"past its {self.promised_by:%d %b} promise, state {self.order.state.value}"
        )


@dataclass
class FulfillmentEngine:
    """Batches purchases, places them, and closes the loop with tracking."""

    context: StorefrontContext
    pipeline: OrderPipeline
    policy: AutomationPolicy = field(default_factory=AutomationPolicy)
    connectors: dict[Channel, ChannelConnector] = field(default_factory=dict)
    guard: IdempotencyGuard = field(default_factory=IdempotencyGuard)
    _po_counter: int = field(default=0, init=False)

    @property
    def audit(self) -> AuditLog:
        return self.pipeline.audit

    # -- batching ----------------------------------------------------------

    def supplier_for_sku(self, sku: str, channel: Channel) -> str | None:
        listing = self.context.listing_by_sku(sku, channel)
        return listing.supplier_id if listing and listing.supplier_id else None

    def draft_purchase_orders(
        self, orders: list[Order] | None = None, at: datetime | None = None
    ) -> list[PurchaseOrder]:
        """Group cleared orders into one purchase order per supplier."""
        moment = at or datetime.now()
        candidates = orders if orders is not None else self.pipeline.awaiting_purchase()
        by_supplier: dict[str, list[tuple[Order, POLine]]] = {}

        for order in candidates:
            if order.state is not OrderState.RISK_CLEARED:
                continue

            # Stage the whole order before committing any of it. Merging line by
            # line lets an order that turns out to be undraftable leave earlier
            # lines behind in a PO that then gets purchased -- the order is held,
            # but its stock is bought anyway.
            staged: dict[str, list[POLine]] = {}
            unmapped: str | None = None
            for line in order.lines:
                supplier_id = self.supplier_for_sku(line.sku, order.channel)
                if supplier_id is None:
                    unmapped = line.sku
                    break
                staged.setdefault(supplier_id, []).append(
                    POLine(line.sku, line.quantity, line.unit_cost, order.id)
                )

            if unmapped is not None:
                self.pipeline.transition(
                    order,
                    OrderState.HELD,
                    actor="fulfilment",
                    reasoning=f"no supplier mapped for {unmapped}",
                )
                continue
            if len(staged) > 1:
                # An order carries a single purchase_order_id, so splitting it
                # across suppliers would attach the first PO and orphan the rest.
                # Hold it for a manual split rather than half-represent it.
                self.pipeline.transition(
                    order,
                    OrderState.HELD,
                    actor="fulfilment",
                    reasoning=(
                        f"lines span {len(staged)} suppliers; needs a manual split"
                    ),
                )
                continue

            for supplier_id, lines in staged.items():
                for line in lines:
                    by_supplier.setdefault(supplier_id, []).append((order, line))

        drafted: list[PurchaseOrder] = []
        for supplier_id, pairs in sorted(by_supplier.items()):
            self._po_counter += 1
            po = PurchaseOrder(
                id=f"PO-{moment:%Y%m%d}-{self._po_counter:03d}",
                supplier_id=supplier_id,
                lines=tuple(line for _order, line in pairs),
                created_at=moment,
                state=POState.DRAFT,
            )
            self.context.purchase_orders.append(po)
            seen: set[str] = set()
            for order, _line in pairs:
                if order.id not in seen and order.state is OrderState.RISK_CLEARED:
                    self.pipeline.attach_purchase_order(order, po.id)
                    seen.add(order.id)
            self.audit.record(
                actor="fulfilment",
                action="po_drafted",
                subject=po.id,
                reasoning=(
                    f"{po.total} to {supplier_id} covering {len(po.order_ids)} order(s)"
                ),
                metadata={"total_cents": po.total.cents, "orders": len(po.order_ids)},
                at=moment,
            )
            drafted.append(po)
        return drafted

    # -- approval and placement -------------------------------------------

    def approve(self, po: PurchaseOrder, actor: str, reason: str) -> PurchaseOrder:
        """Mark a PO approved and advance every order it covers."""
        if po.state not in (POState.DRAFT, POState.AWAITING_APPROVAL):
            raise ValueError(f"{po.id} is {po.state.value}, not awaiting approval")
        po.state = POState.APPROVED
        po.approved_by = actor
        for order_id in po.order_ids:
            order = self.context.order_by_id(order_id)
            if order is not None and order.state is OrderState.PO_DRAFTED:
                self.pipeline.mark_po_approved(order, actor, reason)
        self.audit.record(
            actor=actor,
            action="po_approved",
            subject=po.id,
            reasoning=reason,
            metadata={"total_cents": po.total.cents},
        )
        return po

    def reject(self, po: PurchaseOrder, actor: str, reason: str) -> PurchaseOrder:
        """Reject a PO and cancel the customer orders behind it."""
        po.state = POState.REJECTED
        po.rejection_reason = reason
        for order_id in po.order_ids:
            order = self.context.order_by_id(order_id)
            if order is not None and not order.state.is_terminal:
                self.pipeline.cancel(order, actor, f"purchase order rejected: {reason}")
        self.audit.record(
            actor=actor, action="po_rejected", subject=po.id, reasoning=reason
        )
        return po

    def hold_for_review(self, po: PurchaseOrder, reason: str) -> PurchaseOrder:
        """Park a PO the owner has not refused, without destroying its orders.

        Distinct from :meth:`reject` on purpose. Rejection is the owner deciding
        not to fulfil, and cancelling the customer orders is the right
        consequence. A policy *block* is different: it usually means the batch
        breached a cap, which is no reason to cancel an unrelated customer who
        happened to share a supplier. Their orders go back to ``HELD`` so they
        resurface at the next check-in as a decision, not as a refund.
        """
        po.state = POState.AWAITING_APPROVAL
        po.rejection_reason = reason
        for order_id in po.order_ids:
            order = self.context.order_by_id(order_id)
            if order is not None and order.state is OrderState.PO_DRAFTED:
                order.hold_reason = reason
                self.pipeline.transition(
                    order,
                    OrderState.HELD,
                    actor="policy",
                    reasoning=f"purchase order {po.id} held: {reason}",
                )
        self.audit.record(
            actor="policy",
            action="po_held",
            subject=po.id,
            reasoning=reason,
            metadata={"total_cents": po.total.cents, "orders": len(po.order_ids)},
        )
        return po

    def place(self, po: PurchaseOrder, at: datetime | None = None) -> bool:
        """Send an approved PO to the supplier. Exactly once.

        The spend is recorded against the daily cap here rather than at approval
        time, because the cap exists to bound money actually leaving the account.
        """
        key = idempotency_key("po.place", po.id)
        if self.guard.seen(key):
            return False  # already placed; a retried call is a no-op, not an error
        if po.state is not POState.APPROVED:
            raise ValueError(f"{po.id} must be approved before placement, is {po.state.value}")
        self.guard.claim(key)
        po.state = POState.PLACED
        po.external_ref = f"SUP-{po.id}"
        self.policy.record_spend(po.total, at)
        for order_id in po.order_ids:
            order = self.context.order_by_id(order_id)
            if order is not None and order.state is OrderState.PO_APPROVED:
                self.pipeline.mark_placed(order, po.external_ref)
        self.audit.record(
            actor="fulfilment",
            action="po_placed",
            subject=po.id,
            reasoning=f"{po.total} sent to {po.supplier_id}, ref {po.external_ref}",
            metadata={"total_cents": po.total.cents},
            at=at,
        )
        self.guard.record(key, True)
        return True

    # -- tracking ----------------------------------------------------------

    def ingest_tracking(self, order_id: str, shipment: Shipment) -> bool:
        """Attach tracking to an order and push it to the marketplace.

        A failed push is recorded and reported rather than swallowed: the order
        really did ship, and losing that fact is how a seller account ends up
        penalised for late shipping it did not commit.
        """
        order = self.context.order_by_id(order_id)
        if order is None:
            return False
        if order.state is not OrderState.PLACED:
            raise ValueError(
                f"{order_id} is {order.state.value}; tracking only applies to placed orders"
            )
        self.pipeline.mark_shipped(order, shipment)
        connector = self.connectors.get(order.channel)
        if connector is None:
            return True
        try:
            pushed = connector.submit_tracking(order, shipment)
        except (ChannelError, NotImplementedError) as exc:
            self.audit.record(
                actor="fulfilment",
                action="tracking_push_failed",
                subject=order.id,
                reasoning=f"{order.channel.label} rejected tracking: {exc}",
                metadata={"carrier": shipment.carrier},
            )
            return False
        if pushed:
            self.audit.record(
                actor="fulfilment",
                action="tracking_pushed",
                subject=order.id,
                reasoning=f"tracking sent to {order.channel.label}",
                metadata={"carrier": shipment.carrier},
            )
        return pushed

    def late_orders(self, now: datetime | None = None) -> list[LateOrder]:
        """Orders past the delivery window promised at listing time."""
        moment = now or datetime.now()
        late: list[LateOrder] = []
        for order in self.context.orders:
            if order.state in (OrderState.DELIVERED, OrderState.CANCELLED, OrderState.SHIPPED):
                continue
            promised_days = max(
                (
                    listing.promised_days
                    for line in order.lines
                    if (listing := self.context.listing_by_sku(line.sku, order.channel))
                ),
                default=0,
            )
            if not promised_days:
                continue
            promised_by = order.placed_at + timedelta(days=promised_days)
            if moment > promised_by:
                late.append(
                    LateOrder(order, promised_by, (moment - promised_by).days)
                )
        return sorted(late, key=lambda l: -l.days_late)

    # -- reporting ---------------------------------------------------------

    def pending_purchase_orders(self) -> list[PurchaseOrder]:
        return [
            po
            for po in self.context.purchase_orders
            if po.state in (POState.DRAFT, POState.AWAITING_APPROVAL)
        ]

    def committed_spend(self) -> Money:
        total = Money.zero()
        for po in self.context.purchase_orders:
            if po.state in (POState.APPROVED, POState.PLACED):
                total = total + po.total
        return total
