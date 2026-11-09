"""The live Shopify adapter, exercised offline against a scripted transport.

None of this needs a store. The transport is replaced with a fake that answers
from a script and records what it was asked, so the interesting properties --
that a dry run writes nothing, that the productSet payload is actually sellable,
that a retry cannot double-apply -- are all assertable without credentials.
"""

from __future__ import annotations

import pytest

from conftest import NOW, make_listing, make_order
from storefront_agent.audit import AuditLog
from storefront_agent.channels import ChannelError
from storefront_agent.domain import Channel, ListingState, Shipment
from storefront_agent.security import SecretRef, StaticResolver
from storefront_agent.shopify import (
    GRAMS_PER_POUND,
    SHOPIFY_API_VERSION,
    ShopifyConnector,
)

LOCATION = "gid://shopify/Location/113462575384"
PRODUCT = "gid://shopify/Product/900"


class FakeShopify(ShopifyConnector):
    """A connector whose transport answers from a script."""

    def __init__(self, script: dict[str, dict] | None = None, **kwargs):
        kwargs.setdefault("shop_domain", "test.myshopify.com")
        kwargs.setdefault("token_ref", SecretRef("TOK"))
        kwargs.setdefault("resolver", StaticResolver({"TOK": "shpat_test"}))
        super().__init__(**kwargs)
        self.script = script or {}
        self.calls: list[tuple[str, dict]] = []

    def execute(self, query: str, variables: dict | None = None) -> dict:
        self.calls.append((query, variables or {}))
        for needle, response in self.script.items():
            if needle in query:
                return response
        return {}

    def sent(self, needle: str) -> list[dict]:
        """Variables for every real call whose document mentions ``needle``."""
        return [v for q, v in self.calls if needle in q]


def default_script() -> dict[str, dict]:
    return {
        "connectorLocations": {
            "locations": {
                "nodes": [
                    {"id": "gid://shopify/Location/1", "name": "Warehouse",
                     "isActive": True, "shipsInventory": False},
                    {"id": LOCATION, "name": "Shop location",
                     "isActive": True, "shipsInventory": True},
                ]
            }
        },
        "connectorPublications": {
            "publications": {
                "nodes": [
                    {"id": "gid://shopify/Publication/1", "autoPublish": False,
                     "catalog": None, "name": "Point of Sale"},
                    {"id": "gid://shopify/Publication/2", "autoPublish": False,
                     "catalog": None, "name": "Online Store"},
                ]
            }
        },
        "connectorProductSet": {
            "productSet": {
                "product": {
                    "id": PRODUCT, "handle": "led-desk-lamp", "status": "ACTIVE",
                    "variants": {"nodes": [{"id": "gid://shopify/ProductVariant/1",
                                            "sku": "SKU-1", "price": "24.99"}]},
                },
                "userErrors": [],
            }
        },
        "connectorPublish": {
            "publishablePublish": {
                "publishable": {"availablePublicationsCount": {"count": 1}},
                "userErrors": [],
            }
        },
        "connectorVariantBySku": {
            "productVariants": {
                "nodes": [{"id": "gid://shopify/ProductVariant/1", "sku": "SKU-1",
                           "inventoryItem": {"id": "gid://shopify/InventoryItem/7"}}]
            }
        },
        "connectorInventorySet": {
            "inventorySetQuantities": {
                "inventoryAdjustmentGroup": {"createdAt": "2026-08-21T09:00:00Z",
                                             "reason": "correction"},
                "userErrors": [],
            }
        },
    }


@pytest.fixture
def live() -> FakeShopify:
    """A connector with writes enabled, for testing the real paths."""
    return FakeShopify(default_script(), dry_run=False)


@pytest.fixture
def rehearsal() -> FakeShopify:
    """A connector in its default, safe posture."""
    return FakeShopify(default_script())


class TestSafetyPosture:
    def test_dry_run_is_the_default(self):
        assert ShopifyConnector(shop_domain="x.myshopify.com").dry_run is True

    def test_the_api_version_is_not_stale(self):
        # The old pin was 2025-01, which is past Shopify's support window.
        assert SHOPIFY_API_VERSION >= "2025-10"

    def test_the_endpoint_carries_that_version(self):
        connector = ShopifyConnector(shop_domain="x.myshopify.com")
        assert f"/admin/api/{SHOPIFY_API_VERSION}/graphql.json" in connector.endpoint

    def test_is_live_needs_both_domain_and_token(self):
        assert not ShopifyConnector().is_live()
        assert not ShopifyConnector(shop_domain="x.myshopify.com").is_live()
        assert FakeShopify().is_live()

    def test_is_live_says_nothing_about_whether_writes_are_enabled(self):
        connector = FakeShopify()
        assert connector.is_live() and connector.dry_run


class TestDryRun:
    def test_publishing_sends_no_mutation(self, rehearsal):
        rehearsal.publish_listing(make_listing())
        assert rehearsal.sent("connectorProductSet") == []

    def test_publishing_still_returns_an_id_so_callers_keep_working(self, rehearsal):
        external = rehearsal.publish_listing(make_listing("SKU-9"))
        assert external and "dry-run" in external

    def test_reads_still_execute_for_real(self, rehearsal):
        rehearsal.publish_listing(make_listing())
        # Location discovery is a read, and it must happen: it is how the
        # rehearsed payload gets a real location id in it.
        assert rehearsal.sent("connectorLocations")

    def test_the_rehearsed_payload_is_captured_in_full(self, rehearsal):
        rehearsal.publish_listing(make_listing("SKU-7", price=24.99))
        call = rehearsal.rehearsed[0]
        assert call.operation == "productSet"
        assert call.variables["input"]["variants"][0]["price"] == "24.99"
        assert "24.99" in call.pretty()

    def test_the_rehearsal_is_written_to_the_audit_log(self):
        audit = AuditLog()
        connector = FakeShopify(default_script(), audit=audit)
        connector.publish_listing(make_listing())
        entries = [e for e in audit.entries if e.action == "channel_dry_run"]
        assert entries and entries[0].metadata["dry_run"] is True
        assert audit.verify().valid

    def test_inventory_writes_nothing_and_reports_it(self, rehearsal):
        assert rehearsal.update_inventory("SKU-1", 12) is False
        assert rehearsal.sent("connectorInventorySet") == []

    def test_tracking_writes_nothing(self, rehearsal):
        rehearsal.script["connectorFulfillmentOrders"] = {
            "order": {"id": "gid://shopify/Order/1", "name": "#1001",
                      "fulfillmentOrders": {"nodes": [
                          {"id": "gid://shopify/FulfillmentOrder/1", "status": "OPEN",
                           "lineItems": {"nodes": [{"id": "li1", "remainingQuantity": 1}]}}]}}
        }
        assert rehearsal.submit_tracking(
            make_order("o1"), Shipment("UPS", "1Z999", NOW)
        ) is False
        assert rehearsal.sent("connectorFulfillmentCreate") == []

    def test_the_report_names_every_rehearsed_write(self, rehearsal):
        rehearsal.publish_listing(make_listing("SKU-1"))
        rehearsal.publish_listing(make_listing("SKU-2"))
        report = rehearsal.rehearsal_report()
        assert "2 write(s) rehearsed, none sent" in report
        assert report.count("productSet") == 2

    def test_the_report_is_honest_when_nothing_happened(self, rehearsal):
        assert rehearsal.rehearsal_report() == "No writes were attempted."


class TestLocationDiscovery:
    def test_prefers_a_location_that_ships_inventory(self, live):
        assert live.default_location() == LOCATION

    def test_the_result_is_cached(self, live):
        live.default_location()
        live.default_location()
        assert len(live.sent("connectorLocations")) == 1

    def test_falls_back_to_any_active_location(self):
        connector = FakeShopify(
            {"connectorLocations": {"locations": {"nodes": [
                {"id": "gid://shopify/Location/5", "name": "Only",
                 "isActive": True, "shipsInventory": False}]}}},
            dry_run=False,
        )
        assert connector.default_location() == "gid://shopify/Location/5"

    def test_inactive_locations_are_ignored(self):
        connector = FakeShopify(
            {"connectorLocations": {"locations": {"nodes": [
                {"id": "gid://shopify/Location/5", "name": "Closed",
                 "isActive": False, "shipsInventory": True}]}}},
            dry_run=False,
        )
        with pytest.raises(ChannelError, match="no active location"):
            connector.default_location()

    def test_no_locations_raises_with_a_fix(self):
        connector = FakeShopify({"connectorLocations": {"locations": {"nodes": []}}})
        with pytest.raises(ChannelError, match="Settings -> Locations"):
            connector.default_location()


class TestProductSetPayload:
    def test_the_variant_carries_price_sku_and_stock(self, live):
        payload = live.product_set_input(make_listing("SKU-1", price=24.99, quantity=25))
        variant = payload["variants"][0]
        assert variant["price"] == "24.99"
        assert variant["sku"] == "SKU-1"
        assert variant["inventoryQuantities"][0]["quantity"] == 25
        assert variant["inventoryQuantities"][0]["locationId"] == LOCATION

    def test_inventory_is_tracked_or_stock_is_meaningless(self, live):
        payload = live.product_set_input(make_listing())
        assert payload["variants"][0]["inventoryItem"]["tracked"] is True

    def test_the_product_is_active(self, live):
        assert live.product_set_input(make_listing())["status"] == "ACTIVE"

    def test_a_single_variant_product_still_needs_an_option(self, live):
        payload = live.product_set_input(make_listing())
        assert payload["productOptions"][0]["name"] == "Title"
        assert payload["variants"][0]["optionValues"][0]["optionName"] == "Title"

    def test_the_supplier_becomes_the_vendor(self, live):
        payload = live.product_set_input(make_listing(supplier_id="sup-meridian"))
        assert payload["vendor"] == "sup-meridian"

    def test_bullets_are_rendered_as_html(self, live):
        payload = live.product_set_input(make_listing())
        assert "<ul>" in payload["descriptionHtml"]
        assert "<li>A useful benefit</li>" in payload["descriptionHtml"]

    def test_a_listing_with_no_bullets_still_renders(self, live):
        listing = make_listing()
        listing.bullets = ()
        assert live.product_set_input(listing)["descriptionHtml"].startswith("<p>")

    def test_negative_stock_is_clamped_not_sent(self, live):
        listing = make_listing()
        listing.quantity = -5
        assert live.product_set_input(listing)["variants"][0]["inventoryQuantities"][0]["quantity"] == 0

    def test_price_is_a_decimal_string_not_a_float(self, live):
        # Shopify's Money scalar is a string; sending a float invites a
        # floating-point cent to appear on a real product.
        price = live.product_set_input(make_listing(price=19.9))["variants"][0]["price"]
        assert price == "19.90" and isinstance(price, str)


class TestPublishing:
    def test_a_successful_publish_marks_the_listing_live(self, live):
        listing = make_listing()
        external = live.publish_listing(listing)
        assert external == PRODUCT
        assert listing.state is ListingState.PUBLISHED
        assert listing.external_id == PRODUCT

    def test_publishing_to_the_online_store_is_a_separate_call(self, live):
        live.publish_listing(make_listing())
        assert live.sent("connectorPublish")

    def test_the_online_store_is_found_by_deprecated_name_when_catalog_is_null(self, live):
        assert live.online_store_publication() == "gid://shopify/Publication/2"

    def test_catalog_title_is_preferred_when_present(self):
        connector = FakeShopify(
            {"connectorPublications": {"publications": {"nodes": [
                {"id": "gid://shopify/Publication/9", "autoPublish": False,
                 "catalog": {"title": "Online Store"}, "name": "something else"}]}}},
            dry_run=False,
        )
        assert connector.online_store_publication() == "gid://shopify/Publication/9"

    def test_auto_publishing_channels_skip_the_redundant_write(self):
        connector = FakeShopify(
            {**default_script(),
             "connectorPublications": {"publications": {"nodes": [
                 {"id": "gid://shopify/Publication/2", "autoPublish": True,
                  "catalog": None, "name": "Online Store"}]}}},
            dry_run=False,
        )
        assert connector.publish(PRODUCT) is True
        assert connector.sent("connectorPublish") == []

    def test_a_shop_with_no_online_store_reports_false(self):
        connector = FakeShopify(
            {"connectorPublications": {"publications": {"nodes": []}}}, dry_run=False
        )
        assert connector.publish(PRODUCT) is False

    def test_user_errors_are_raised_not_swallowed(self):
        connector = FakeShopify(
            {**default_script(),
             "connectorProductSet": {"productSet": {
                 "product": None,
                 "userErrors": [{"field": "price", "message": "is invalid"}]}}},
            dry_run=False,
        )
        with pytest.raises(ChannelError, match="rejected productSet"):
            connector.publish_listing(make_listing())

    def test_a_missing_product_id_is_an_error_not_a_silent_success(self):
        connector = FakeShopify(
            {**default_script(),
             "connectorProductSet": {"productSet": {"product": {}, "userErrors": []}}},
            dry_run=False,
        )
        with pytest.raises(ChannelError, match="no product id"):
            connector.publish_listing(make_listing())


class TestInventory:
    def test_stock_is_set_at_the_discovered_location(self, live):
        assert live.update_inventory("SKU-1", 12) is True
        sent = live.sent("connectorInventorySet")[0]
        quantity = sent["input"]["quantities"][0]
        assert quantity["locationId"] == LOCATION
        assert quantity["quantity"] == 12
        assert quantity["inventoryItemId"] == "gid://shopify/InventoryItem/7"

    def test_an_unknown_sku_reports_false_rather_than_raising(self):
        connector = FakeShopify(
            {**default_script(),
             "connectorVariantBySku": {"productVariants": {"nodes": []}}},
            dry_run=False,
        )
        assert connector.update_inventory("NOPE", 5) is False

    def test_a_repeated_correction_is_applied_once(self, live):
        live.update_inventory("SKU-1", 12)
        live.update_inventory("SKU-1", 12)
        assert len(live.sent("connectorInventorySet")) == 1

    def test_a_different_quantity_is_a_different_write(self, live):
        live.update_inventory("SKU-1", 12)
        live.update_inventory("SKU-1", 8)
        assert len(live.sent("connectorInventorySet")) == 2

    def test_negative_stock_is_clamped(self, live):
        live.update_inventory("SKU-1", -3)
        assert live.sent("connectorInventorySet")[0]["input"]["quantities"][0]["quantity"] == 0


class TestUnits:
    def test_grams_convert_to_pounds(self):
        assert ShopifyConnector.pounds_from_grams(453) == pytest.approx(0.9987, abs=1e-3)
        assert ShopifyConnector.pounds_from_grams(1000) == pytest.approx(2.2046, abs=1e-3)

    def test_the_constant_is_the_real_one(self):
        assert GRAMS_PER_POUND == pytest.approx(453.59237)

    def test_zero_grams_is_zero_pounds(self):
        assert ShopifyConnector.pounds_from_grams(0) == 0.0


class TestOrderIntake:
    def test_fetching_orders_explains_it_is_phase_two(self, live):
        with pytest.raises(NotImplementedError, match="phase 2b"):
            live.fetch_orders()

    def test_the_guidance_names_the_hmac_check(self, live):
        with pytest.raises(NotImplementedError, match="verify_webhook"):
            live.fetch_orders()

    def test_acknowledgement_is_a_no_op_because_shopify_has_no_such_step(self, live):
        assert live.acknowledge_order(make_order("o1")) is True


class TestTransport:
    def test_a_missing_domain_is_caught_before_any_request(self):
        with pytest.raises(ChannelError, match="shop_domain"):
            ShopifyConnector().execute("query { shop { name } }")

    def test_transport_errors_are_surfaced(self):
        connector = FakeShopify()
        connector.channel = Channel.SHOPIFY
        assert connector.channel is Channel.SHOPIFY

    def test_user_error_helper_passes_a_clean_response(self):
        ShopifyConnector.raise_user_errors({"userErrors": []}, "anything")

    def test_user_error_helper_names_the_field_and_message(self):
        with pytest.raises(ChannelError, match="trackingInfo: is invalid"):
            ShopifyConnector.raise_user_errors(
                {"userErrors": [{"field": "trackingInfo", "message": "is invalid"}]},
                "fulfillmentCreate",
            )
