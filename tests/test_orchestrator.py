"""End to end: prompt in, parcels out, with the owner touching it twice."""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import NOW, dollars
from storefront_agent.approvals import ApprovalKind, Decision
from storefront_agent.domain import Channel, ListingState, OrderState
from storefront_agent.nlu import RunConfig
from storefront_agent.orchestrator import Orchestrator
from storefront_agent.scenarios import (
    build_catalogue,
    build_orchestrator,
    build_suppliers,
    seed_orders,
    seed_tracking,
)
from storefront_agent.sequence import StorefrontSequence
from storefront_agent.providers import StubProvider

PROMPT = (
    "start selling desk accessories on shopify, amazon and ebay, "
    "budget $2000, balanced, 25% margin, 5 products"
)


@pytest.fixture
def demo():
    agent = build_orchestrator(provider=StubProvider())
    return agent, StorefrontSequence(agent)


class TestConfiguration:
    def test_applying_a_config_creates_storefronts_and_connectors(self):
        agent = Orchestrator()
        agent.apply_config(RunConfig(niche="lamps", channels=(Channel.SHOPIFY, Channel.EBAY)))
        assert set(agent.context.channels()) == {Channel.SHOPIFY, Channel.EBAY}
        assert set(agent.connectors) >= {Channel.SHOPIFY, Channel.EBAY}

    def test_channel_fee_rates_differ_by_marketplace(self):
        agent = Orchestrator()
        agent.apply_config(RunConfig(niche="x", channels=tuple(Channel)))
        rates = {sf.channel: sf.fee_rate for sf in agent.context.storefronts}
        assert rates[Channel.AMAZON] > rates[Channel.EBAY] > rates[Channel.SHOPIFY]

    def test_applying_a_config_twice_does_not_duplicate_storefronts(self):
        agent = Orchestrator()
        config = RunConfig(niche="x", channels=(Channel.SHOPIFY,))
        agent.apply_config(config)
        agent.apply_config(config)
        assert len(agent.context.storefronts) == 1


class TestNaturalLanguage:
    def test_check_in_returns_a_digest(self, demo):
        agent, _ = demo
        result = agent.handle("check in")
        assert result.ok and "digest" in result.data

    def test_status_summarises_the_business(self, demo):
        agent, _ = demo
        assert "listing(s)" in agent.handle("what's the status?").summary

    def test_pause_is_acknowledged(self, demo):
        agent, _ = demo
        assert agent.handle("pause everything").ok

    def test_an_unclear_instruction_asks_for_a_rephrase(self, demo):
        agent, _ = demo
        result = agent.handle("the weather is nice")
        assert not result.ok and "check in" in result.summary

    def test_every_instruction_is_audited(self, demo):
        agent, _ = demo
        agent.handle("check in")
        assert any(e.action == "request_received" for e in agent.audit.entries)


class TestListingStage:
    def test_a_compliant_product_lists_on_every_channel(self, demo):
        agent, sequence = demo
        state = sequence.start(PROMPT, at=NOW)
        sequence.run(state, at=NOW)
        lamp = [l for l in agent.context.listings if "Lamp" in l.title]
        assert {l.channel for l in lamp} == set(Channel)

    def test_a_slow_supplier_is_blocked_on_amazon_only(self, demo):
        agent, sequence = demo
        state = sequence.start(PROMPT, at=NOW)
        sequence.run(state, at=NOW)
        blocked = [l for l in agent.context.listings if l.state is ListingState.BLOCKED]
        assert blocked
        assert all(l.channel is Channel.AMAZON for l in blocked)
        assert all("handling" in l.blocked_reason.lower() or "limit" in l.blocked_reason
                   for l in blocked)

    def test_the_same_product_is_priced_differently_per_channel(self, demo):
        agent, sequence = demo
        state = sequence.start(PROMPT, at=NOW)
        sequence.run(state, at=NOW)
        lamp = {l.channel: l.price for l in agent.context.listings if "Lamp" in l.title}
        assert lamp[Channel.AMAZON] > lamp[Channel.SHOPIFY]

    def test_published_listings_reach_their_connector(self, demo):
        agent, sequence = demo
        state = sequence.start(PROMPT, at=NOW)
        sequence.run(state, at=NOW)
        live = [l for l in agent.context.listings if l.state is ListingState.PUBLISHED]
        assert live and all(l.external_id for l in live)


class TestFullLoop:
    def _trade(self, agent, sequence):
        """Launch, clear the gate, take orders, clear the second gate, ship."""
        state = sequence.start(PROMPT, at=NOW)
        sequence.run(state, at=NOW)
        agent.approve_all(at=NOW)
        sequence.resume(state, at=NOW)
        seed_orders(agent, at=NOW)
        later = NOW + timedelta(hours=1)
        sequence.run(state, at=later)
        for item in list(agent.approvals.pending()):
            agent.approvals.decide(item.id, Decision.APPROVE, "owner", "ok", at=later)
            agent.act_on(item.id, later)
        much_later = later + timedelta(days=2)
        sequence.resume(state, at=much_later)
        seed_tracking(agent, at=much_later)
        sequence.run(state, at=much_later)
        return state

    def test_orders_reach_shipped(self, demo):
        agent, sequence = demo
        self._trade(agent, sequence)
        shipped = [o for o in agent.context.orders if o.state is OrderState.SHIPPED]
        assert shipped
        assert all(o.shipment is not None for o in shipped)

    def test_tracking_is_pushed_back_to_the_originating_channel(self, demo):
        agent, sequence = demo
        self._trade(agent, sequence)
        for order in agent.context.orders:
            if order.state is OrderState.SHIPPED:
                assert order.external_id in agent.connectors[order.channel].tracking

    def test_oversold_and_risky_orders_are_held_not_shipped(self, demo):
        agent, sequence = demo
        state = sequence.start(PROMPT, at=NOW)
        sequence.run(state, at=NOW)
        agent.approve_all(at=NOW)
        sequence.resume(state, at=NOW)
        seed_orders(agent, at=NOW)
        sequence.run(state, at=NOW + timedelta(hours=1))
        held = [o for o in agent.context.orders if o.state is OrderState.HELD]
        assert len(held) >= 2
        assert any("available" in o.hold_reason for o in held)
        assert any("freight_forwarder" in o.hold_reason for o in held)

    def test_the_audit_chain_survives_a_full_trading_cycle(self, demo):
        agent, sequence = demo
        self._trade(agent, sequence)
        assert len(agent.audit.entries) > 50
        assert agent.audit.verify().valid

    def test_no_customer_email_reaches_the_audit_log(self, demo):
        agent, sequence = demo
        self._trade(agent, sequence)
        blob = agent.audit.export_jsonl()
        assert "@example.com" not in blob

    def test_the_owner_only_had_to_act_twice(self, demo):
        agent, sequence = demo
        self._trade(agent, sequence)
        check_ins = [e for e in agent.audit.entries if e.action == "check_in"]
        assert len(check_ins) <= 4  # two rounds, each digest plus its report stage


class TestPurchaseOrders:
    def test_a_po_within_the_cap_is_placed_without_the_owner(self, demo):
        agent, sequence = demo
        agent.policy.po_auto_approve_cap = dollars(10_000)
        state = sequence.start(PROMPT, at=NOW)
        sequence.run(state, at=NOW)
        agent.approve_all(at=NOW)
        sequence.resume(state, at=NOW)
        seed_orders(agent, at=NOW)
        sequence.run(state, at=NOW + timedelta(hours=1))
        placed = [o for o in agent.context.orders if o.state is OrderState.PLACED]
        assert placed
        assert not [i for i in agent.approvals.pending()
                    if i.kind is ApprovalKind.PURCHASE_ORDER]

    def test_a_po_over_the_cap_waits_for_the_owner(self, demo):
        agent, sequence = demo
        state = sequence.start(PROMPT, at=NOW)
        sequence.run(state, at=NOW)
        agent.approve_all(at=NOW)
        sequence.resume(state, at=NOW)
        seed_orders(agent, at=NOW)
        sequence.run(state, at=NOW + timedelta(hours=1))
        waiting = [i for i in agent.approvals.pending()
                   if i.kind is ApprovalKind.PURCHASE_ORDER]
        assert waiting and waiting[0].amount > agent.policy.effective_po_cap

    def test_rejecting_a_po_cancels_its_orders(self, demo):
        agent, sequence = demo
        state = sequence.start(PROMPT, at=NOW)
        sequence.run(state, at=NOW)
        agent.approve_all(at=NOW)
        sequence.resume(state, at=NOW)
        seed_orders(agent, at=NOW)
        later = NOW + timedelta(hours=1)
        sequence.run(state, at=later)
        po_item = [i for i in agent.approvals.pending()
                   if i.kind is ApprovalKind.PURCHASE_ORDER][0]
        agent.approvals.decide(po_item.id, Decision.REJECT, "owner", "too dear", at=later)
        agent.act_on(po_item.id, later)
        po = agent.context.po_by_id(po_item.subject_id)
        assert all(
            agent.context.order_by_id(oid).state is OrderState.CANCELLED
            for oid in po.order_ids
        )


class TestScenarioData:
    def test_the_catalogue_covers_every_compliance_branch(self):
        suppliers = build_suppliers()
        assert not suppliers["retail"].ships_blind
        assert not suppliers["retail"].is_wholesale
        assert suppliers["wholesale"].ships_blind and suppliers["wholesale"].is_wholesale

    def test_every_catalogue_entry_is_priced_above_zero(self):
        assert all(c.landed_cost.cents > 0 for c in build_catalogue())

    def test_seeding_orders_before_listings_does_nothing(self):
        assert seed_orders(build_orchestrator(provider=StubProvider()), at=NOW) == 0
