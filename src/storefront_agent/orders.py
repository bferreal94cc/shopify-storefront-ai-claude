"""The order pipeline: one audited chokepoint for every state change.

An order moves ``RECEIVED → VALIDATED → RISK_CLEARED → PO_DRAFTED →
PO_APPROVED → PLACED → SHIPPED → DELIVERED``, with ``HELD`` as the risk branch
and ``CANCELLED`` as the exit. :data:`LEGAL_TRANSITIONS` is the whole truth
about what may follow what, and :class:`OrderPipeline` is the only thing
permitted to move an order.

That centralisation is the point. In a system that spends money automatically,
the dangerous bug is not a wrong decision -- it is a state change nobody
recorded. Every transition here writes to the audit log with its reasoning, so
"why did we buy this" always has an answer.

Two edges are deliberately absent. ``PLACED`` cannot go back to ``HELD``: once
the supplier has been paid, holding the customer order does not unspend the
money, and the honest move is a cancellation with a refund. ``SHIPPED`` cannot
be cancelled: a parcel in transit is a returns problem, not a pipeline state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .audit import AuditLog
from .domain import Address, Channel, Order, OrderState, Shipment, StorefrontContext
from .policy import AutomationPolicy
from .risk import RiskAssessment, RiskEngine

LEGAL_TRANSITIONS: dict[OrderState, frozenset[OrderState]] = {
    OrderState.RECEIVED: frozenset(
        {OrderState.VALIDATED, OrderState.HELD, OrderState.CANCELLED}
    ),
    OrderState.VALIDATED: frozenset(
        {OrderState.RISK_CLEARED, OrderState.HELD, OrderState.CANCELLED}
    ),
    OrderState.RISK_CLEARED: frozenset(
        {OrderState.PO_DRAFTED, OrderState.HELD, OrderState.CANCELLED}
    ),
    OrderState.PO_DRAFTED: frozenset(
        {OrderState.PO_APPROVED, OrderState.HELD, OrderState.CANCELLED}
    ),
    OrderState.PO_APPROVED: frozenset(
        {OrderState.PLACED, OrderState.HELD, OrderState.CANCELLED}
    ),
    OrderState.PLACED: frozenset({OrderState.SHIPPED, OrderState.CANCELLED}),
    OrderState.SHIPPED: frozenset({OrderState.DELIVERED}),
    OrderState.HELD: frozenset(
        {OrderState.VALIDATED, OrderState.RISK_CLEARED, OrderState.CANCELLED}
    ),
    OrderState.DELIVERED: frozenset(),
    OrderState.CANCELLED: frozenset(),
}


class IllegalTransition(RuntimeError):
    """An attempt to move an order somewhere it cannot go."""


@dataclass(frozen=True)
class ValidationIssue:
    field: str
    detail: str

    def describe(self) -> str:
        return f"{self.field}: {self.detail}"


def validate_order(order: Order, context: StorefrontContext) -> list[ValidationIssue]:
    """Everything wrong with an order, before we spend anything on it."""
    issues: list[ValidationIssue] = []
    if not order.lines:
        issues.append(ValidationIssue("lines", "order has no line items"))
    for line in order.lines:
        listing = context.listing_by_sku(line.sku, order.channel)
        if listing is None:
            issues.append(
                ValidationIssue("sku", f"{line.sku} is not listed on {order.channel.label}")
            )
        elif listing.quantity < line.quantity:
            issues.append(
                ValidationIssue(
                    "inventory",
                    f"{line.sku} has {listing.quantity} available, order wants {line.quantity}",
                )
            )
    issues.extend(_address_issues(order.ship_to))
    if order.revenue.cents <= 0:
        issues.append(ValidationIssue("revenue", "order total is not positive"))
    return issues


def _address_issues(address: Address) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if not address.line1.strip():
        issues.append(ValidationIssue("address", "street line is empty"))
    if not address.city.strip():
        issues.append(ValidationIssue("address", "city is empty"))
    if not address.postal_code.strip():
        issues.append(ValidationIssue("address", "postal code is empty"))
    if len(address.country) != 2:
        issues.append(
            ValidationIssue("address", f"country must be a 2-letter code, got {address.country!r}")
        )
    return issues


@dataclass
class OrderPipeline:
    """Moves orders through their states, auditing every step."""

    context: StorefrontContext
    policy: AutomationPolicy = field(default_factory=AutomationPolicy)
    audit: AuditLog = field(default_factory=AuditLog)
    risk: RiskEngine = field(default_factory=RiskEngine)

    def __post_init__(self) -> None:
        self.risk.threshold = self.policy.risk_hold_threshold

    # -- the only mutator --------------------------------------------------

    def transition(
        self,
        order: Order,
        to_state: OrderState,
        actor: str,
        reasoning: str,
        metadata: dict | None = None,
        at: datetime | None = None,
    ) -> Order:
        """Move ``order`` to ``to_state``, or raise.

        Every path that changes an order's state comes through here, which is
        what makes the audit trail complete rather than best-effort.
        """
        allowed = LEGAL_TRANSITIONS.get(order.state, frozenset())
        if to_state not in allowed:
            permitted = ", ".join(sorted(s.value for s in allowed)) or "nothing (terminal)"
            raise IllegalTransition(
                f"{order.id}: cannot go {order.state.value} -> {to_state.value}; "
                f"legal next states are {permitted}"
            )
        previous = order.state
        order.state = to_state
        self.audit.record(
            actor=actor,
            action=f"order_{to_state.value}",
            subject=order.id,
            reasoning=reasoning,
            metadata={"from": previous.value, "to": to_state.value, **(metadata or {})},
            at=at,
        )
        return order

    # -- intake ------------------------------------------------------------

    def receive(self, order: Order) -> Order:
        """Register a newly-pulled order."""
        if self.context.order_by_id(order.id) is None:
            self.context.orders.append(order)
        self.audit.record(
            actor="order_pipeline",
            action="order_received",
            subject=order.id,
            reasoning=f"{order.channel.label} order {order.external_id}, {order.revenue}",
            metadata={"channel": order.channel.value, "units": order.units},
        )
        return order

    def validate(self, order: Order) -> tuple[Order, list[ValidationIssue]]:
        """Check the order is fulfillable; hold it if not."""
        issues = validate_order(order, self.context)
        if issues:
            order.hold_reason = "; ".join(i.describe() for i in issues)
            self.transition(
                order,
                OrderState.HELD,
                actor="order_pipeline",
                reasoning=f"validation failed: {order.hold_reason}",
                metadata={"issues": len(issues)},
            )
            return order, issues
        self.transition(
            order,
            OrderState.VALIDATED,
            actor="order_pipeline",
            reasoning=f"{len(order.lines)} line(s) in stock, address complete",
        )
        return order, []

    def screen(self, order: Order) -> tuple[Order, RiskAssessment]:
        """Score fraud risk and either clear or hold."""
        assessment = self.risk.assess(order)
        order.risk_score = assessment.score
        if assessment.should_hold:
            order.hold_reason = assessment.summary()
            self.transition(
                order,
                OrderState.HELD,
                actor="risk_engine",
                reasoning=f"held for review: {assessment.summary()}",
                metadata={"risk_score": assessment.score, "band": assessment.band},
            )
        else:
            self.transition(
                order,
                OrderState.RISK_CLEARED,
                actor="risk_engine",
                reasoning=f"cleared, {assessment.summary()}",
                metadata={"risk_score": assessment.score, "band": assessment.band},
            )
        return order, assessment

    # -- owner actions -----------------------------------------------------

    def release(self, order: Order, actor: str, note: str = "") -> Order:
        """Owner clears a held order. Re-enters the pipeline at risk-cleared."""
        order.hold_reason = ""
        return self.transition(
            order,
            OrderState.RISK_CLEARED,
            actor=actor,
            reasoning=note or "released from hold by owner review",
        )

    def cancel(self, order: Order, actor: str, reason: str) -> Order:
        return self.transition(
            order, OrderState.CANCELLED, actor=actor, reasoning=reason
        )

    # -- fulfilment progression -------------------------------------------

    def attach_purchase_order(self, order: Order, po_id: str) -> Order:
        order.purchase_order_id = po_id
        return self.transition(
            order,
            OrderState.PO_DRAFTED,
            actor="fulfilment",
            reasoning=f"drafted onto purchase order {po_id}",
            metadata={"purchase_order": po_id},
        )

    def mark_po_approved(self, order: Order, actor: str, reason: str) -> Order:
        return self.transition(
            order, OrderState.PO_APPROVED, actor=actor, reasoning=reason
        )

    def mark_placed(self, order: Order, supplier_ref: str = "") -> Order:
        return self.transition(
            order,
            OrderState.PLACED,
            actor="fulfilment",
            reasoning=f"purchase order placed with supplier{f' ({supplier_ref})' if supplier_ref else ''}",
        )

    def mark_shipped(self, order: Order, shipment: Shipment) -> Order:
        order.shipment = shipment
        return self.transition(
            order,
            OrderState.SHIPPED,
            actor="fulfilment",
            reasoning=f"shipped via {shipment.describe()}",
            metadata={"carrier": shipment.carrier},
        )

    def mark_delivered(self, order: Order, at: datetime | None = None) -> Order:
        return self.transition(
            order, OrderState.DELIVERED, actor="fulfilment", reasoning="carrier confirmed delivery", at=at
        )

    # -- queries -----------------------------------------------------------

    def held(self) -> list[Order]:
        return self.context.orders_in(OrderState.HELD)

    def awaiting_purchase(self) -> list[Order]:
        return self.context.orders_in(OrderState.RISK_CLEARED)

    def in_flight(self) -> list[Order]:
        return self.context.orders_in(
            OrderState.PO_DRAFTED, OrderState.PO_APPROVED, OrderState.PLACED
        )

    def intake(self, orders: list[Order]) -> dict[str, int]:
        """Run a batch through receive → validate → screen.

        Returns a tally by resulting state, which is what the check-in digest
        reports and what the dashboard renders as the pipeline summary.
        """
        tally: dict[str, int] = {}
        for order in orders:
            self.receive(order)
            _order, issues = self.validate(order)
            if not issues:
                self.screen(order)
            tally[order.state.value] = tally.get(order.state.value, 0) + 1
        return tally


def channel_of(order: Order) -> Channel:
    return order.channel
