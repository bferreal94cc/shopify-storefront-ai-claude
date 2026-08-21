"""Fraud scoring must explain itself and hold only what is worth holding."""

from __future__ import annotations

from datetime import timedelta

from conftest import NOW, make_order
from storefront_agent.risk import RiskEngine, RiskSignal


def names(assessment) -> set[str]:
    return {s.name for s in assessment.signals}


class TestSignals:
    def test_an_ordinary_repeat_order_scores_zero(self):
        assessment = RiskEngine().assess(make_order())
        assert assessment.score == 0.0
        assert assessment.band == "low"
        assert not assessment.should_hold
        assert "nothing flagged" in assessment.summary()

    def test_a_large_first_order_flags(self):
        order = make_order(quantity=20, price=40, prior_orders=0)
        assert "first_order_high_value" in names(RiskEngine().assess(order))

    def test_a_returning_customer_does_not_flag_on_value(self):
        order = make_order(quantity=20, price=40, prior_orders=9)
        assert "first_order_high_value" not in names(RiskEngine().assess(order))

    def test_a_freight_forwarder_destination_flags(self):
        order = make_order(line1="Freight Forward Depot 14")
        assert "freight_forwarder" in names(RiskEngine().assess(order))

    def test_an_elevated_risk_destination_flags(self):
        assert "elevated_risk_destination" in names(
            RiskEngine().assess(make_order(country="NG"))
        )

    def test_a_mismatched_shipping_name_flags(self):
        order = make_order(name="Dana Whitfield", ship_name="Marcus Webb")
        assert "name_mismatch" in names(RiskEngine().assess(order))

    def test_initials_and_middle_names_do_not_flag(self):
        order = make_order(name="Dana A. Whitfield", ship_name="D. Whitfield")
        assert "name_mismatch" not in names(RiskEngine().assess(order))

    def test_bulk_quantity_on_a_consumer_order_flags(self):
        assert "bulk_quantity" in names(RiskEngine().assess(make_order(quantity=12)))

    def test_a_loss_making_order_flags_as_a_pricing_error(self):
        order = make_order(price=5.00, cost=30.00)
        assert "negative_margin" in names(RiskEngine().assess(order))


class TestVelocity:
    def test_repeated_orders_from_one_email_flag(self):
        engine = RiskEngine(velocity_limit=3)
        for index in range(3):
            engine.assess(
                make_order(f"o{index}", email="same@example.com", at=NOW + timedelta(minutes=index))
            )
        latest = engine.assess(make_order("o9", email="same@example.com", at=NOW + timedelta(minutes=5)))
        assert "order_velocity" in names(latest)

    def test_orders_outside_the_window_do_not_count(self):
        engine = RiskEngine(velocity_limit=2, velocity_window=timedelta(hours=1))
        engine.assess(make_order("o1", email="same@example.com", at=NOW))
        engine.assess(make_order("o2", email="same@example.com", at=NOW))
        much_later = engine.assess(
            make_order("o3", email="same@example.com", at=NOW + timedelta(days=2))
        )
        assert "order_velocity" not in names(much_later)


class TestScoring:
    def test_signals_stack_into_a_hold(self):
        order = make_order(
            country="NG", line1="Freight Forward Depot", name="Ann Lee", ship_name="Bob Kay"
        )
        assessment = RiskEngine().assess(order)
        assert assessment.should_hold
        assert assessment.band == "elevated"

    def test_the_score_is_capped_at_one(self):
        order = make_order(
            quantity=30, price=100, prior_orders=0, country="NG",
            line1="Freight Forward Depot", name="Ann Lee", ship_name="Bob Kay",
        )
        assert RiskEngine().assess(order).score <= 1.0

    def test_signals_are_ranked_heaviest_first(self):
        order = make_order(country="NG", line1="Freight Forward Depot")
        weights = [s.weight for s in RiskEngine().assess(order).signals]
        assert weights == sorted(weights, reverse=True)

    def test_the_threshold_is_configurable(self):
        order = make_order(country="NG")
        assert not RiskEngine(threshold=0.5).assess(order).should_hold
        assert RiskEngine(threshold=0.15).assess(order).should_hold

    def test_explanations_are_human_readable(self):
        explanation = RiskEngine().assess(make_order(country="NG")).explain()
        assert explanation and "elevated_risk_destination" in explanation[0]

    def test_bands_map_to_scores(self):
        assert RiskSignal("x", 0.9, "d").weight == 0.9
        low = RiskEngine().assess(make_order())
        assert low.band == "low"
