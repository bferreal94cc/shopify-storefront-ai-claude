"""The state machine is the safety rail; illegal moves must raise."""

from __future__ import annotations

import pytest

from conftest import NOW, make_context, make_listing, make_order
from storefront_agent.domain import Address, OrderState, Shipment
from storefront_agent.orders import (
    LEGAL_TRANSITIONS,
    IllegalTransition,
    OrderPipeline,
    validate_order,
)
from storefront_agent.policy import AutomationPolicy


@pytest.fixture
def pipeline():
    return OrderPipeline(make_context([make_listing(quantity=25)]))


class TestTransitionTable:
    def test_every_state_is_covered(self):
        assert set(LEGAL_TRANSITIONS) == set(OrderState)

    def test_terminal_states_lead_nowhere(self):
        assert LEGAL_TRANSITIONS[OrderState.DELIVERED] == frozenset()
        assert LEGAL_TRANSITIONS[OrderState.CANCELLED] == frozenset()

    def test_a_placed_order_cannot_be_held_because_the_money_is_spent(self):
        assert OrderState.HELD not in LEGAL_TRANSITIONS[OrderState.PLACED]

    def test_a_shipped_order_cannot_be_cancelled(self):
        assert LEGAL_TRANSITIONS[OrderState.SHIPPED] == frozenset({OrderState.DELIVERED})

    def test_a_held_order_can_return_to_the_pipeline_or_be_cancelled(self):
        assert LEGAL_TRANSITIONS[OrderState.HELD] == frozenset(
            {OrderState.VALIDATED, OrderState.RISK_CLEARED, OrderState.CANCELLED}
        )


class TestValidation:
    def test_a_good_order_has_no_issues(self, pipeline):
        assert validate_order(make_order(), pipeline.context) == []

    def test_an_unlisted_sku_is_caught(self, pipeline):
        issues = validate_order(make_order(sku="NOPE"), pipeline.context)
        assert any("not listed" in i.detail for i in issues)

    def test_overselling_is_caught(self, pipeline):
        issues = validate_order(make_order(quantity=400), pipeline.context)
        assert any("available" in i.detail for i in issues)

    @pytest.mark.parametrize(
        "kwargs,fragment",
        [
            ({"line1": ""}, "street"),
            ({"city": ""}, "city"),
            ({"postal": ""}, "postal"),
            ({"country": "USA"}, "2-letter"),
        ],
    )
    def test_incomplete_addresses_are_caught(self, pipeline, kwargs, fragment):
        issues = validate_order(make_order(**kwargs), pipeline.context)
        assert any(fragment in i.detail for i in issues)

    def test_an_order_with_no_lines_is_caught(self, pipeline):
        order = make_order()
        order.lines = ()
        assert any(i.field == "lines" for i in validate_order(order, pipeline.context))


class TestIntake:
    def test_a_clean_order_reaches_risk_cleared(self, pipeline):
        assert pipeline.intake([make_order()]) == {"risk_cleared": 1}

    def test_an_oversell_is_held_with_the_reason_attached(self, pipeline):
        pipeline.intake([make_order("o1", quantity=400)])
        order = pipeline.context.order_by_id("o1")
        assert order.state is OrderState.HELD
        assert "available" in order.hold_reason

    def test_a_risky_order_is_held(self, pipeline):
        pipeline.intake(
            [make_order("o1", country="NG", line1="Freight Forward Depot",
                        name="Ann Lee", ship_name="Bob Kay")]
        )
        assert pipeline.context.order_by_id("o1").state is OrderState.HELD

    def test_receiving_the_same_order_twice_does_not_duplicate_it(self, pipeline):
        order = make_order("o1")
        pipeline.receive(order)
        pipeline.receive(order)
        assert len(pipeline.context.orders) == 1

    def test_the_hold_threshold_comes_from_policy(self):
        context = make_context([make_listing()])
        pipeline = OrderPipeline(context, AutomationPolicy(risk_hold_threshold=0.1))
        pipeline.intake([make_order("o1", country="NG")])
        assert pipeline.context.order_by_id("o1").state is OrderState.HELD


class TestTransitions:
    def test_the_happy_path_runs_to_delivered(self, pipeline):
        pipeline.intake([make_order("o1")])
        order = pipeline.context.order_by_id("o1")
        pipeline.attach_purchase_order(order, "PO-1")
        pipeline.mark_po_approved(order, "owner", "approved")
        pipeline.mark_placed(order, "SUP-1")
        pipeline.mark_shipped(order, Shipment("UPS", "1Z", NOW))
        pipeline.mark_delivered(order)
        assert order.state is OrderState.DELIVERED
        assert order.purchase_order_id == "PO-1"

    def test_skipping_a_step_raises_and_names_the_legal_moves(self, pipeline):
        pipeline.intake([make_order("o1")])
        order = pipeline.context.order_by_id("o1")
        with pytest.raises(IllegalTransition, match="legal next states are"):
            pipeline.mark_shipped(order, Shipment("UPS", "1Z", NOW))

    def test_a_terminal_order_cannot_move(self, pipeline):
        pipeline.intake([make_order("o1")])
        order = pipeline.context.order_by_id("o1")
        pipeline.cancel(order, "owner", "changed their mind")
        with pytest.raises(IllegalTransition, match="terminal"):
            pipeline.cancel(order, "owner", "again")

    def test_releasing_a_held_order_returns_it_to_the_pipeline(self, pipeline):
        pipeline.intake([make_order("o1", quantity=400)])
        order = pipeline.context.order_by_id("o1")
        pipeline.release(order, "owner", "restocked")
        assert order.state is OrderState.RISK_CLEARED
        assert order.hold_reason == ""


class TestAuditing:
    def test_every_transition_is_recorded_with_its_reasoning(self, pipeline):
        pipeline.intake([make_order("o1")])
        actions = [e.action for e in pipeline.audit.entries]
        assert "order_received" in actions and "order_risk_cleared" in actions
        assert all(e.reasoning for e in pipeline.audit.entries)

    def test_the_chain_verifies_after_a_full_run(self, pipeline):
        pipeline.intake([make_order("o1"), make_order("o2", quantity=400)])
        assert pipeline.audit.verify().valid

    def test_entries_record_where_the_order_came_from_and_went(self, pipeline):
        pipeline.intake([make_order("o1")])
        cleared = [e for e in pipeline.audit.entries if e.action == "order_risk_cleared"][0]
        assert cleared.metadata["from"] == "validated"
        assert cleared.metadata["to"] == "risk_cleared"


class TestQueries:
    def test_the_queues_partition_the_orders(self, pipeline):
        pipeline.intake([make_order("o1"), make_order("o2", quantity=400)])
        assert [o.id for o in pipeline.awaiting_purchase()] == ["o1"]
        assert [o.id for o in pipeline.held()] == ["o2"]

    def test_in_flight_covers_the_purchasing_states(self, pipeline):
        pipeline.intake([make_order("o1")])
        order = pipeline.context.order_by_id("o1")
        pipeline.attach_purchase_order(order, "PO-1")
        assert [o.id for o in pipeline.in_flight()] == ["o1"]
