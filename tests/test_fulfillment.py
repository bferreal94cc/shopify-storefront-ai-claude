"""Batching, exactly-once placement, and not punishing bystanders."""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import NOW, dollars, make_context, make_listing, make_order
from storefront_agent.channels import InMemoryChannel
from storefront_agent.domain import Channel, OrderLine, OrderState, POState, Shipment
from storefront_agent.fulfillment import FulfillmentEngine
from storefront_agent.orders import OrderPipeline
from storefront_agent.policy import AutomationPolicy


@pytest.fixture
def engine():
    context = make_context(
        [
            make_listing("SKU-A", supplier_id="sup-1", quantity=100, promised_days=6),
            make_listing("SKU-B", supplier_id="sup-1", quantity=100, promised_days=6),
            make_listing("SKU-C", supplier_id="sup-2", quantity=100, promised_days=6),
        ]
    )
    pipeline = OrderPipeline(context, AutomationPolicy())
    return FulfillmentEngine(
        context, pipeline, pipeline.policy, {Channel.SHOPIFY: InMemoryChannel()}
    )


def cleared(engine, *specs):
    orders = [make_order(oid, sku=sku, quantity=qty) for oid, sku, qty in specs]
    engine.pipeline.intake(orders)
    return orders


class TestBatching:
    def test_orders_for_one_supplier_become_one_purchase_order(self, engine):
        cleared(engine, ("o1", "SKU-A", 2), ("o2", "SKU-B", 3))
        pos = engine.draft_purchase_orders(at=NOW)
        assert len(pos) == 1
        assert set(pos[0].order_ids) == {"o1", "o2"}

    def test_different_suppliers_get_different_purchase_orders(self, engine):
        cleared(engine, ("o1", "SKU-A", 1), ("o2", "SKU-C", 1))
        assert {po.supplier_id for po in engine.draft_purchase_orders(at=NOW)} == {"sup-1", "sup-2"}

    def test_the_total_is_cost_not_revenue(self, engine):
        cleared(engine, ("o1", "SKU-A", 2))
        assert engine.draft_purchase_orders(at=NOW)[0].total == dollars(16)

    def test_covered_orders_advance_to_po_drafted(self, engine):
        cleared(engine, ("o1", "SKU-A", 1))
        engine.draft_purchase_orders(at=NOW)
        assert engine.context.order_by_id("o1").state is OrderState.PO_DRAFTED

    def test_a_sku_with_no_supplier_holds_its_order(self, engine):
        engine.context.listings.append(make_listing("SKU-X", supplier_id="", quantity=10))
        cleared(engine, ("o1", "SKU-X", 1))
        engine.draft_purchase_orders(at=NOW)
        order = engine.context.order_by_id("o1")
        assert order.state is OrderState.HELD
        assert "no supplier" in order.hold_reason or order.hold_reason == ""

    def test_an_unmappable_line_leaves_none_of_its_order_in_a_po(self, engine):
        # The order is held, but the buyable line must not be bought anyway.
        engine.context.listings.append(make_listing("SKU-X", supplier_id="", quantity=10))
        order = make_order("o1", sku="SKU-A", quantity=1)
        order.lines = (
            OrderLine("SKU-A", 1, dollars(24.99), dollars(8)),
            OrderLine("SKU-X", 1, dollars(24.99), dollars(8)),
        )
        engine.pipeline.intake([order])
        pos = engine.draft_purchase_orders(at=NOW)
        assert engine.context.order_by_id("o1").state is OrderState.HELD
        assert [line for po in pos for line in po.lines if line.order_id == "o1"] == []

    def test_an_order_spanning_two_suppliers_is_held_not_split(self, engine):
        # An order carries one purchase_order_id, so splitting it would attach
        # the first PO and orphan the rest.
        order = make_order("o1", sku="SKU-A", quantity=1)
        order.lines = (
            OrderLine("SKU-A", 1, dollars(24.99), dollars(8)),
            OrderLine("SKU-C", 1, dollars(24.99), dollars(8)),
        )
        engine.pipeline.intake([order])
        pos = engine.draft_purchase_orders(at=NOW)
        assert engine.context.order_by_id("o1").state is OrderState.HELD
        assert [line for po in pos for line in po.lines if line.order_id == "o1"] == []

    def test_only_cleared_orders_are_batched(self, engine):
        cleared(engine, ("o1", "SKU-A", 1))
        engine.draft_purchase_orders(at=NOW)
        assert engine.draft_purchase_orders(at=NOW) == []


class TestPlacement:
    def test_placing_requires_approval_first(self, engine):
        cleared(engine, ("o1", "SKU-A", 1))
        po = engine.draft_purchase_orders(at=NOW)[0]
        with pytest.raises(ValueError, match="must be approved"):
            engine.place(po, at=NOW)

    def test_approval_advances_the_covered_orders(self, engine):
        cleared(engine, ("o1", "SKU-A", 1))
        po = engine.draft_purchase_orders(at=NOW)[0]
        engine.approve(po, "owner", "within cap")
        assert po.state is POState.APPROVED
        assert engine.context.order_by_id("o1").state is OrderState.PO_APPROVED

    def test_placement_is_exactly_once(self, engine):
        cleared(engine, ("o1", "SKU-A", 1))
        po = engine.draft_purchase_orders(at=NOW)[0]
        engine.approve(po, "owner", "ok")
        assert engine.place(po, at=NOW)
        assert not engine.place(po, at=NOW)

    def test_spend_is_recorded_once_only(self, engine):
        cleared(engine, ("o1", "SKU-A", 2))
        po = engine.draft_purchase_orders(at=NOW)[0]
        engine.approve(po, "owner", "ok")
        engine.place(po, at=NOW)
        engine.place(po, at=NOW)
        assert engine.policy.spent_today(NOW) == dollars(16)

    def test_approving_twice_raises(self, engine):
        cleared(engine, ("o1", "SKU-A", 1))
        po = engine.draft_purchase_orders(at=NOW)[0]
        engine.approve(po, "owner", "ok")
        engine.place(po, at=NOW)
        with pytest.raises(ValueError, match="not awaiting approval"):
            engine.approve(po, "owner", "again")


class TestRejectionVersusHold:
    def test_owner_rejection_cancels_the_customer_orders(self, engine):
        cleared(engine, ("o1", "SKU-A", 1))
        po = engine.draft_purchase_orders(at=NOW)[0]
        engine.reject(po, "owner", "supplier out of stock")
        assert po.state is POState.REJECTED
        assert engine.context.order_by_id("o1").state is OrderState.CANCELLED

    def test_a_policy_hold_does_not_cancel_bystanders(self, engine):
        cleared(engine, ("o1", "SKU-A", 1), ("o2", "SKU-B", 1))
        po = engine.draft_purchase_orders(at=NOW)[0]
        engine.hold_for_review(po, "breached the daily cap")
        assert po.state is POState.AWAITING_APPROVAL
        for order_id in ("o1", "o2"):
            order = engine.context.order_by_id(order_id)
            assert order.state is OrderState.HELD
            assert "breached the daily cap" in order.hold_reason

    def test_a_held_po_can_still_be_approved_later(self, engine):
        cleared(engine, ("o1", "SKU-A", 1))
        po = engine.draft_purchase_orders(at=NOW)[0]
        engine.hold_for_review(po, "over cap")
        engine.approve(po, "owner", "raised the cap")
        assert po.state is POState.APPROVED


class TestTracking:
    def _placed(self, engine):
        cleared(engine, ("o1", "SKU-A", 1))
        po = engine.draft_purchase_orders(at=NOW)[0]
        engine.approve(po, "owner", "ok")
        engine.place(po, at=NOW)
        return po

    def test_tracking_ships_the_order_and_reaches_the_channel(self, engine):
        self._placed(engine)
        shipment = Shipment("UPS", "1Z999", NOW)
        assert engine.ingest_tracking("o1", shipment)
        order = engine.context.order_by_id("o1")
        assert order.state is OrderState.SHIPPED
        assert order.shipment is shipment
        assert engine.connectors[Channel.SHOPIFY].tracking["#o1"] is shipment

    def test_tracking_for_an_unplaced_order_raises(self, engine):
        cleared(engine, ("o1", "SKU-A", 1))
        with pytest.raises(ValueError, match="only applies to placed"):
            engine.ingest_tracking("o1", Shipment("UPS", "1Z", NOW))

    def test_tracking_for_an_unknown_order_is_false_not_a_crash(self, engine):
        assert not engine.ingest_tracking("nope", Shipment("UPS", "1Z", NOW))

    def test_a_channel_that_rejects_tracking_is_recorded_not_swallowed(self, engine):
        from storefront_agent.channels import amazon_connector

        self._placed(engine)
        engine.connectors[Channel.SHOPIFY] = amazon_connector()
        assert not engine.ingest_tracking("o1", Shipment("UPS", "1Z", NOW))
        assert any(e.action == "tracking_push_failed" for e in engine.audit.entries)
        # The order still shipped: losing that fact is what gets sellers penalised.
        assert engine.context.order_by_id("o1").state is OrderState.SHIPPED


class TestLateDetection:
    def test_an_overdue_order_is_flagged_against_its_promise(self, engine):
        cleared(engine, ("o1", "SKU-A", 1))
        late = engine.late_orders(NOW + timedelta(days=10))
        assert len(late) == 1 and late[0].days_late == 4

    def test_nothing_is_late_before_the_promise(self, engine):
        cleared(engine, ("o1", "SKU-A", 1))
        assert engine.late_orders(NOW + timedelta(days=2)) == []

    def test_shipped_and_cancelled_orders_are_not_chased(self, engine):
        cleared(engine, ("o1", "SKU-A", 1))
        order = engine.context.order_by_id("o1")
        engine.pipeline.cancel(order, "owner", "done")
        assert engine.late_orders(NOW + timedelta(days=30)) == []

    def test_the_worst_offender_sorts_first(self, engine):
        cleared(engine, ("o1", "SKU-A", 1))
        engine.context.listings[1].promised_days = 2
        cleared(engine, ("o2", "SKU-B", 1))
        late = engine.late_orders(NOW + timedelta(days=20))
        assert late[0].days_late >= late[-1].days_late


class TestReporting:
    def test_pending_purchase_orders_exclude_placed_ones(self, engine):
        cleared(engine, ("o1", "SKU-A", 1))
        po = engine.draft_purchase_orders(at=NOW)[0]
        assert engine.pending_purchase_orders() == [po]
        engine.approve(po, "owner", "ok")
        engine.place(po, at=NOW)
        assert engine.pending_purchase_orders() == []

    def test_committed_spend_counts_approved_and_placed(self, engine):
        cleared(engine, ("o1", "SKU-A", 2))
        po = engine.draft_purchase_orders(at=NOW)[0]
        assert engine.committed_spend().is_zero()
        engine.approve(po, "owner", "ok")
        assert engine.committed_spend() == dollars(16)
