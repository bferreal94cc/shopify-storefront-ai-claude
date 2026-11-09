"""Policy decides what runs unattended. These are the boundaries."""

from __future__ import annotations

from datetime import datetime, time

import pytest

from conftest import dollars, make_candidate, make_supplier
from storefront_agent.domain import Channel, Money, POLine, POState, PurchaseOrder
from storefront_agent.policy import (
    AutomationPolicy,
    AutonomyLevel,
    ChannelRules,
    Verdict,
    default_channel_rules,
)

AT = datetime(2026, 8, 21, 10, 0)


def make_po(total_dollars, supplier_id="sup-1", po_id="PO-1") -> PurchaseOrder:
    return PurchaseOrder(
        id=po_id,
        supplier_id=supplier_id,
        lines=(POLine("SKU", 1, dollars(total_dollars), "o1"),),
        created_at=AT,
        state=POState.DRAFT,
    )


class TestChannelCompliance:
    def test_amazon_blocks_a_supplier_that_will_not_ship_blind(self):
        candidate = make_candidate(supplier=make_supplier(blind=False))
        decision = AutomationPolicy().evaluate_candidate(
            candidate, Channel.AMAZON, dollars(24.99)
        )
        assert decision.verdict is Verdict.BLOCK
        assert decision.rule == "amazon.seller_of_record"
        assert "seller of record" in decision.reason

    def test_ebay_blocks_retail_arbitrage(self):
        candidate = make_candidate(supplier=make_supplier(wholesale=False))
        decision = AutomationPolicy().evaluate_candidate(
            candidate, Channel.EBAY, dollars(24.99)
        )
        assert decision.verdict is Verdict.BLOCK
        assert decision.rule == "ebay.retail_arbitrage"

    def test_shopify_allows_what_the_marketplaces_forbid(self):
        candidate = make_candidate(supplier=make_supplier(blind=False, wholesale=False))
        decision = AutomationPolicy().evaluate_candidate(
            candidate, Channel.SHOPIFY, dollars(24.99)
        )
        assert decision.is_allowed

    def test_slow_supplier_fails_amazon_handling_window(self):
        slow = make_supplier(lead_time_days=6)
        candidate = make_candidate(supplier=slow, transit_days=20)
        decision = AutomationPolicy().evaluate_candidate(
            candidate, Channel.AMAZON, dollars(49.99)
        )
        assert decision.verdict is Verdict.BLOCK
        assert decision.rule == "amazon.handling_time"

    def test_restricted_category_is_blocked_everywhere(self):
        candidate = make_candidate(category="weapons")
        for channel in Channel:
            decision = AutomationPolicy().evaluate_candidate(
                candidate, channel, dollars(99.99)
            )
            assert decision.verdict is Verdict.BLOCK

    def test_low_rated_supplier_escalates_rather_than_blocking(self):
        candidate = make_candidate(supplier=make_supplier(rating=3.9))
        decision = AutomationPolicy().evaluate_candidate(
            candidate, Channel.AMAZON, dollars(24.99)
        )
        assert decision.needs_owner
        assert decision.rule == "amazon.supplier_rating"

    def test_default_rules_cover_every_channel(self):
        assert set(default_channel_rules()) == set(Channel)

    def test_unknown_channel_gets_permissive_empty_rules(self):
        rules = ChannelRules(channel=Channel.SHOPIFY)
        assert rules.evaluate(make_candidate()).is_allowed


class TestMargin:
    def test_negative_margin_is_blocked_not_escalated(self):
        candidate = make_candidate(unit_cost=20, ship_cost=10)
        decision = AutomationPolicy().evaluate_candidate(
            candidate, Channel.SHOPIFY, dollars(9.99)
        )
        assert decision.verdict is Verdict.BLOCK
        assert decision.rule == "negative_margin"

    def test_thin_margin_escalates_to_the_owner(self):
        candidate = make_candidate(unit_cost=8, ship_cost=1)
        decision = AutomationPolicy().evaluate_candidate(
            candidate, Channel.SHOPIFY, dollars(10.50)
        )
        assert decision.needs_owner
        assert decision.rule == "margin_floor"

    def test_margin_exactly_at_the_floor_is_allowed(self):
        policy = AutomationPolicy(margin_floor=0.5)
        candidate = make_candidate(unit_cost=5, ship_cost=0)
        assert policy.evaluate_candidate(candidate, Channel.SHOPIFY, dollars(10)).is_allowed


class TestPurchaseOrders:
    def test_within_cap_runs_unattended(self):
        assert AutomationPolicy().evaluate_purchase_order(make_po(100), AT).is_allowed

    def test_over_cap_escalates(self):
        decision = AutomationPolicy().evaluate_purchase_order(make_po(420), AT)
        assert decision.needs_owner
        assert decision.rule == "po_auto_approve_cap"

    def test_hard_cap_blocks_and_cannot_be_clicked_past(self):
        decision = AutomationPolicy().evaluate_purchase_order(make_po(5000), AT)
        assert decision.verdict is Verdict.BLOCK
        assert decision.rule == "po_hard_cap"

    def test_empty_po_is_blocked(self):
        assert AutomationPolicy().evaluate_purchase_order(make_po(0), AT).is_blocked

    def test_blocklisted_supplier_is_blocked(self):
        policy = AutomationPolicy(supplier_blocklist=frozenset({"sup-bad"}))
        decision = policy.evaluate_purchase_order(make_po(50, supplier_id="sup-bad"), AT)
        assert decision.rule == "supplier_blocklist"

    def test_conservative_autonomy_lowers_the_auto_approve_ceiling(self):
        conservative = AutomationPolicy(autonomy=AutonomyLevel.CONSERVATIVE)
        assert conservative.effective_po_cap == dollars(37.50)
        assert conservative.evaluate_purchase_order(make_po(100), AT).needs_owner

    def test_aggressive_autonomy_raises_it(self):
        aggressive = AutomationPolicy(autonomy=AutonomyLevel.AGGRESSIVE)
        assert aggressive.evaluate_purchase_order(make_po(400), AT).is_allowed

    def test_autonomy_cannot_unblock_a_compliance_rule(self):
        candidate = make_candidate(supplier=make_supplier(blind=False))
        decision = AutomationPolicy.aggressive().evaluate_candidate(
            candidate, Channel.AMAZON, dollars(99.99)
        )
        assert decision.is_blocked


class TestDailySpend:
    def test_spend_accumulates_and_blocks_at_the_cap(self):
        policy = AutomationPolicy(daily_spend_cap=dollars(300))
        policy.record_spend(dollars(250), AT)
        assert policy.spent_today(AT) == dollars(250)
        assert policy.remaining_today(AT) == dollars(50)
        decision = policy.evaluate_purchase_order(make_po(100), AT)
        assert decision.rule == "daily_spend_cap"

    def test_spend_resets_the_next_day(self):
        policy = AutomationPolicy()
        policy.record_spend(dollars(250), AT)
        tomorrow = AT.replace(day=22)
        assert policy.spent_today(tomorrow) == Money.zero()


class TestCheckInWindows:
    def test_next_window_later_the_same_day(self):
        assert AutomationPolicy().next_check_in(
            datetime(2026, 8, 21, 10, 30)
        ) == datetime(2026, 8, 21, 17, 0)

    def test_after_the_last_window_rolls_to_tomorrow(self):
        assert AutomationPolicy().next_check_in(
            datetime(2026, 8, 21, 22, 0)
        ) == datetime(2026, 8, 22, 9, 0)

    @pytest.mark.parametrize(
        "moment,expected",
        [
            (datetime(2026, 8, 31, 23, 30), datetime(2026, 9, 1, 9, 0)),
            (datetime(2026, 12, 31, 18, 0), datetime(2027, 1, 1, 9, 0)),
            (datetime(2028, 2, 28, 20, 0), datetime(2028, 2, 29, 9, 0)),
        ],
    )
    def test_rolls_over_month_year_and_leap_day(self, moment, expected):
        assert AutomationPolicy().next_check_in(moment) == expected

    def test_custom_windows_are_honoured(self):
        policy = AutomationPolicy(check_in_windows=(time(7, 30), time(12, 0), time(20, 0)))
        assert policy.next_check_in(datetime(2026, 8, 21, 8, 0)) == datetime(2026, 8, 21, 12, 0)


class TestOtherDecisions:
    def test_social_defaults_to_requiring_approval(self):
        assert AutomationPolicy().evaluate_social_post("Instagram").needs_owner

    def test_aggressive_policy_allows_auto_posting(self):
        assert AutomationPolicy.aggressive().evaluate_social_post("X").is_allowed

    def test_ad_spend_over_cap_escalates(self):
        assert AutomationPolicy().evaluate_ad_spend(dollars(500), "Meta").needs_owner

    def test_zero_ad_budget_is_blocked(self):
        assert AutomationPolicy().evaluate_ad_spend(Money.zero(), "Meta").is_blocked

    def test_risk_threshold_drives_holds(self):
        policy = AutomationPolicy(risk_hold_threshold=0.5)
        assert policy.should_hold_order(0.5)
        assert not policy.should_hold_order(0.49)

    def test_summary_mentions_the_dial_and_the_windows(self):
        summary = AutomationPolicy().summary()
        assert "balanced" in summary and "09:00" in summary and "17:00" in summary
