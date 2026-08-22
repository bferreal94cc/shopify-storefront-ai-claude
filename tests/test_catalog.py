"""Listing copy, and the fee-aware pricing that protects the margin floor."""

from __future__ import annotations

import pytest

from conftest import dollars, make_candidate, make_supplier
from storefront_agent.catalog import CatalogBuilder, CopyWriter, PricingEngine
from storefront_agent.domain import Channel, ListingState, Money, Storefront
from storefront_agent.policy import AutomationPolicy
from storefront_agent.providers import StubProvider

SHOPIFY = Storefront(Channel.SHOPIFY, "Shop", fee_rate=0.029, fee_fixed=Money(30))
AMAZON = Storefront(Channel.AMAZON, "Amazon", fee_rate=0.15, fee_fixed=Money(0))
EBAY = Storefront(Channel.EBAY, "eBay", fee_rate=0.1325, fee_fixed=Money(30))


class TestPricing:
    @pytest.mark.parametrize("storefront", [SHOPIFY, AMAZON, EBAY])
    @pytest.mark.parametrize("target", [0.20, 0.30, 0.45])
    def test_the_solved_price_actually_clears_the_target_after_fees(self, storefront, target):
        engine = PricingEngine()
        landed = dollars(8)
        price = engine.price_for(landed, storefront, target)
        assert price is not None
        assert engine.net_margin_rate(price, landed, storefront) >= target

    def test_a_higher_fee_channel_is_priced_higher_for_the_same_margin(self):
        engine = PricingEngine()
        landed = dollars(8)
        cheap = engine.price_for(landed, SHOPIFY, 0.25)
        dear = engine.price_for(landed, AMAZON, 0.25)
        assert dear > cheap

    def test_an_unreachable_target_returns_none_rather_than_a_bad_price(self):
        punitive = Storefront(Channel.AMAZON, "X", fee_rate=0.80, fee_fixed=Money(0))
        assert PricingEngine().price_for(dollars(8), punitive, 0.30) is None

    def test_a_target_equal_to_the_remaining_share_is_unreachable(self):
        half = Storefront(Channel.AMAZON, "X", fee_rate=0.50, fee_fixed=Money(0))
        assert PricingEngine().price_for(dollars(8), half, 0.50) is None

    def test_an_exact_cent_requirement_is_not_rounded_past(self):
        # Zero fees and a zero margin make the requirement exactly the cost, so
        # the cheapest clearing price IS the cost. Adding a cent unconditionally
        # overshot it, and charm rounding turned that cent into a dollar.
        free = Storefront(Channel.SHOPIFY, "Free", fee_rate=0.0, fee_fixed=Money(0))
        assert PricingEngine(charm_endings=False).price_for(
            dollars(10.99), free, 0.0
        ).cents == 1099
        assert PricingEngine(charm_endings=True).price_for(
            dollars(10.99), free, 0.0
        ).cents == 1099

    def test_a_fractional_requirement_still_rounds_up(self):
        free = Storefront(Channel.SHOPIFY, "Free", fee_rate=0.0, fee_fixed=Money(0))
        # 1000 / 0.995 = 1005.02..., so the cheapest whole cent is 1006.
        assert PricingEngine(charm_endings=False).price_for(
            dollars(10), free, 0.005
        ).cents == 1006

    def test_charm_rounding_always_rounds_up(self):
        assert PricingEngine._charm(Money(1050)).cents == 1099
        assert PricingEngine._charm(Money(1100)).cents == 1199
        assert PricingEngine._charm(Money(1099)).cents == 1099

    def test_charm_rounding_can_be_switched_off(self):
        plain = PricingEngine(charm_endings=False).price_for(dollars(8), SHOPIFY, 0.25)
        assert plain is not None and plain.cents % 100 != 99

    def test_net_proceeds_deduct_the_channel_cut(self):
        assert PricingEngine().net_proceeds(dollars(100), SHOPIFY) == dollars(96.80)


class TestCopyWriter:
    def test_the_fallback_produces_usable_copy_with_no_model(self):
        copy = CopyWriter(StubProvider()).write(make_candidate(), Channel.SHOPIFY)
        assert copy.title and len(copy.bullets) == 4 and copy.description
        assert copy.source == "fallback"

    def test_a_model_response_is_used_when_it_has_enough_lines(self):
        provider = StubProvider(
            default="A Great Lamp\nBullet one\nBullet two\nBullet three\nBullet four\nA description."
        )
        copy = CopyWriter(provider).write(make_candidate(), Channel.SHOPIFY)
        assert copy.title == "A Great Lamp"
        assert copy.source == "stub"

    @pytest.mark.parametrize(
        "channel,limit", [(Channel.SHOPIFY, 70), (Channel.AMAZON, 200), (Channel.EBAY, 80)]
    )
    def test_titles_respect_each_channel_limit(self, channel, limit):
        provider = StubProvider(default="X" * 500 + "\nb1\nb2\nb3\nb4\ndesc")
        copy = CopyWriter(provider).write(make_candidate(), channel)
        assert len(copy.title) <= limit


class TestCatalogBuilder:
    def test_one_listing_per_storefront(self):
        listings = CatalogBuilder().build_all(make_candidate(), [SHOPIFY, AMAZON, EBAY])
        assert [l.channel for l in listings] == [Channel.SHOPIFY, Channel.AMAZON, Channel.EBAY]

    def test_skus_are_unique_per_channel(self):
        listings = CatalogBuilder().build_all(make_candidate(), [SHOPIFY, AMAZON, EBAY])
        assert len({l.sku for l in listings}) == 3

    def test_a_blocked_candidate_still_returns_a_listing_with_the_reason(self):
        candidate = make_candidate(supplier=make_supplier(blind=False))
        listing = CatalogBuilder().build(candidate, AMAZON)
        assert listing.state is ListingState.BLOCKED
        assert "seller of record" in listing.blocked_reason
        assert listing.quantity == 0

    def test_the_supplier_and_promise_are_carried_onto_the_listing(self):
        candidate = make_candidate(supplier=make_supplier("sup-x", lead_time_days=3), transit_days=5)
        listing = CatalogBuilder().build(candidate, SHOPIFY)
        assert listing.supplier_id == "sup-x"
        assert listing.promised_days == 8

    def test_an_unreachable_margin_blocks_with_an_explanation(self):
        punitive = Storefront(Channel.AMAZON, "X", fee_rate=0.90, fee_fixed=Money(0))
        listing = CatalogBuilder().build(make_candidate(), punitive)
        assert listing.state is ListingState.BLOCKED
        assert "unreachable" in listing.blocked_reason

    def test_a_clean_candidate_is_a_draft_with_stock(self):
        listing = CatalogBuilder().build(make_candidate(), SHOPIFY, quantity=40)
        assert listing.state is ListingState.DRAFT
        assert listing.quantity == 40

    def test_the_margin_floor_from_policy_is_the_one_applied(self):
        builder = CatalogBuilder(policy=AutomationPolicy(margin_floor=0.5))
        listing = builder.build(make_candidate(), SHOPIFY)
        net = builder.pricing.net_margin_rate(listing.price, listing.landed_cost, SHOPIFY)
        assert net >= 0.5
