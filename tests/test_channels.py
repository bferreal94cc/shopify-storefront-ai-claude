"""Every adapter implements one contract; the in-memory one implements it fully."""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import NOW, make_listing, make_order
from storefront_agent.channels import (
    ChannelConnector,
    ChannelError,
    InMemoryChannel,
    ShopifyConnector,
    amazon_connector,
    connector_for,
    ebay_connector,
)
from storefront_agent.domain import Channel, ListingState, Shipment
from storefront_agent.security import SecretRef, StaticResolver

CONTRACT = ("publish_listing", "update_inventory", "fetch_orders", "acknowledge_order", "submit_tracking")


class TestContract:
    @pytest.mark.parametrize(
        "connector",
        [InMemoryChannel(), ShopifyConnector(), amazon_connector(), ebay_connector()],
    )
    def test_every_adapter_implements_every_verb(self, connector):
        assert isinstance(connector, ChannelConnector)
        for verb in CONTRACT:
            assert callable(getattr(connector, verb))


class TestInMemoryChannel:
    def test_publishing_assigns_an_id_and_stocks_inventory(self):
        channel = InMemoryChannel(channel=Channel.EBAY)
        listing = make_listing(quantity=25)
        external = channel.publish_listing(listing)
        assert external.startswith("ebay-")
        assert listing.state is ListingState.PUBLISHED
        assert channel.inventory[listing.sku] == 25

    def test_a_blocked_listing_is_refused(self):
        channel = InMemoryChannel()
        listing = make_listing()
        listing.state = ListingState.BLOCKED
        with pytest.raises(ChannelError, match="blocked listing"):
            channel.publish_listing(listing)

    def test_inventory_updates_only_apply_to_published_skus(self):
        channel = InMemoryChannel()
        assert not channel.update_inventory("NOPE", 5)
        listing = make_listing()
        channel.publish_listing(listing)
        assert channel.update_inventory(listing.sku, 12)
        assert listing.quantity == 12

    def test_inventory_never_goes_negative(self):
        channel = InMemoryChannel()
        listing = make_listing()
        channel.publish_listing(listing)
        channel.update_inventory(listing.sku, -5)
        assert channel.inventory[listing.sku] == 0

    def test_fetching_drains_the_inbox_so_orders_arrive_once(self):
        channel = InMemoryChannel()
        channel.seed_order(make_order("o1"))
        assert len(channel.fetch_orders()) == 1
        assert channel.fetch_orders() == []

    def test_fetching_since_a_later_time_returns_nothing(self):
        channel = InMemoryChannel()
        channel.seed_order(make_order("o1", at=NOW))
        assert channel.fetch_orders(since=NOW + timedelta(hours=1)) == []

    def test_tracking_and_acknowledgement_are_recorded(self):
        channel = InMemoryChannel()
        order = make_order("o1")
        assert channel.acknowledge_order(order)
        shipment = Shipment("UPS", "1Z999", NOW)
        assert channel.submit_tracking(order, shipment)
        assert channel.tracking[order.external_id] is shipment


class TestLiveAdapters:
    def test_the_default_connector_is_never_live(self):
        for channel in Channel:
            connector = connector_for(channel)
            assert isinstance(connector, InMemoryChannel)
            assert not connector.is_live()

    def test_shopify_is_not_live_without_a_domain_and_token(self):
        assert not ShopifyConnector().is_live()
        assert not ShopifyConnector(shop_domain="x.myshopify.com").is_live()

    def test_shopify_is_live_once_both_are_supplied(self):
        connector = ShopifyConnector(
            shop_domain="x.myshopify.com",
            token_ref=SecretRef("TOK"),
            resolver=StaticResolver({"TOK": "shpat_x"}),
        )
        assert connector.is_live()

    def test_shopify_endpoint_pins_an_api_version(self):
        connector = ShopifyConnector(shop_domain="x.myshopify.com")
        assert connector.endpoint.startswith("https://x.myshopify.com/admin/api/")
        assert connector.endpoint.endswith("/graphql.json")

    def test_shopify_refuses_to_call_without_a_domain(self):
        with pytest.raises(ChannelError, match="shop_domain"):
            ShopifyConnector().execute("query { shop { name } }")

    def test_shopify_surfaces_user_errors_rather_than_reporting_success(self):
        with pytest.raises(ChannelError, match="rejected fulfillmentCreate"):
            ShopifyConnector.raise_user_errors(
                {"userErrors": [{"field": "trackingInfo", "message": "is invalid"}]},
                "fulfillmentCreate",
            )

    def test_shopify_accepts_an_empty_user_errors_array(self):
        ShopifyConnector.raise_user_errors({"userErrors": []}, "fulfillmentCreate")

    def test_shopify_rate_limiter_eventually_refuses(self):
        # Drain on the real monotonic clock: the limiter refills against it, so
        # a fake timestamp here would just look like a long idle period.
        connector = ShopifyConnector(shop_domain="x.myshopify.com")
        for _ in range(connector.limiter.capacity):
            assert connector.limiter.allow()
        with pytest.raises(ChannelError, match="rate limit"):
            connector.execute("query { shop { name } }")

    def test_shopify_never_reaches_the_network_without_a_token(self):
        # The limiter is checked first, so this proves the secret is resolved
        # before any request is built -- and that a missing one fails loudly.
        from storefront_agent.security import MissingSecret

        connector = ShopifyConnector(shop_domain="x.myshopify.com")
        with pytest.raises(MissingSecret, match="never be committed"):
            connector.execute("query { shop { name } }")

    @pytest.mark.parametrize("factory", [amazon_connector, ebay_connector])
    def test_unwired_adapters_explain_what_is_missing(self, factory):
        connector = factory()
        with pytest.raises(NotImplementedError) as excinfo:
            connector.publish_listing(make_listing())
        assert "InMemoryChannel" in str(excinfo.value)

    def test_amazon_guidance_mentions_the_dropshipping_policy(self):
        with pytest.raises(NotImplementedError, match="seller of record"):
            amazon_connector().fetch_orders()

    def test_going_live_is_explicit_per_channel(self):
        assert isinstance(connector_for(Channel.SHOPIFY, live=True), ShopifyConnector)
        assert not isinstance(connector_for(Channel.AMAZON, live=True), InMemoryChannel)
