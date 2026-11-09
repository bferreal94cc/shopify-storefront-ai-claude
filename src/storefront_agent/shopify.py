"""The live Shopify adapter, over the GraphQL Admin API.

Split out of ``channels.py`` because this is the one adapter that talks to a
real storefront, and it earns the room. It speaks HTTP through ``urllib`` from
the standard library, so the package still has no third-party dependencies.

**Every write goes through :meth:`ShopifyConnector.mutate`, and ``dry_run``
defaults to True.** In dry-run the mutation is recorded -- document, variables
and all -- and a synthetic id comes back, but nothing leaves the process. Reads
always execute for real, because reading is how you find out what a write would
do. Turning writes on is an explicit constructor argument, which is the same
discipline as ``connector_for(live=False)``: nothing in the environment can
silently promote a rehearsal into a run that changes a live store.

Three details about Shopify that are not obvious from outside, and that each
cost a debugging session to discover:

* **Creating a sellable product is not ``productCreate``.** That makes a product
  with a default variant at $0.00, no SKU and no stock -- live, visible, and
  unsellable. :meth:`publish_listing` uses ``productSet``, which Shopify
  documents for syncing an external source, and which sets the variant, price,
  SKU and inventory in one call.
* **``status: ACTIVE`` does not make a product visible.** Publishing to the
  Online Store publication is a separate step, handled by :meth:`publish`.
* **Fulfilment is two calls.** Read the order's ``fulfillmentOrders`` -- the
  unit of fulfilment work, one per assigned location -- then
  ``fulfillmentCreate`` with ``lineItemsByFulfillmentOrder`` and ``trackingInfo``.
  Later corrections use ``fulfillmentTrackingInfoUpdate``. Since API version
  2024-10 this only works for merchant-managed locations or a fulfilment
  service you own; an order routed to somebody else's service needs
  ``fulfillmentOrderSubmitFulfillmentRequest`` instead.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .audit import AuditLog
from .channels import ChannelConnector, ChannelError
from .domain import Channel, Listing, ListingState, Order, Shipment
from .security import (
    IdempotencyGuard,
    RateLimiter,
    SecretRef,
    SecretResolver,
    idempotency_key,
)
from .shopify_gql import (
    FULFILLMENT_CREATE_MUTATION,
    FULFILLMENT_ORDERS_QUERY,
    INVENTORY_SET_MUTATION,
    LOCATIONS_QUERY,
    PRODUCT_SET_MUTATION,
    PUBLICATIONS_QUERY,
    PUBLISHABLE_PUBLISH_MUTATION,
    VARIANT_BY_SKU_QUERY,
)

# Shopify supports roughly a year of quarterly versions. Pinning a stale one is
# a slow-motion outage: it keeps working until abruptly it does not.
SHOPIFY_API_VERSION = "2026-04"

GRAMS_PER_POUND = 453.59237


@dataclass
class DryRunCall:
    """A write that was rehearsed rather than sent."""

    operation: str
    variables: dict[str, Any]
    synthetic_id: str
    at: datetime = field(default_factory=datetime.now)

    def describe(self) -> str:
        return f"{self.operation} -> {self.synthetic_id} (not sent)"

    def pretty(self) -> str:
        """The full payload, for the owner to read before going live."""
        return f"{self.operation}\n{json.dumps(self.variables, indent=2, default=str)}"


@dataclass
class ShopifyConnector(ChannelConnector):
    """Live Shopify adapter. Writes are rehearsed unless ``dry_run`` is False."""

    shop_domain: str = ""
    channel: Channel = Channel.SHOPIFY
    token_ref: SecretRef = field(default_factory=lambda: SecretRef("SHOPIFY_ACCESS_TOKEN"))
    resolver: SecretResolver = field(default_factory=SecretResolver)
    limiter: RateLimiter = field(
        default_factory=lambda: RateLimiter(capacity=4, refill_per_second=2.0)
    )
    guard: IdempotencyGuard = field(default_factory=IdempotencyGuard)
    audit: AuditLog | None = None
    timeout: float = 20.0
    dry_run: bool = True
    rehearsed: list[DryRunCall] = field(default_factory=list)
    _location_id: str = field(default="", init=False)
    _publication_id: str = field(default="", init=False)
    _auto_publishes: bool = field(default=False, init=False)
    _counter: int = field(default=0, init=False)

    # -- plumbing ----------------------------------------------------------

    def is_live(self) -> bool:
        """Whether this adapter can reach a real shop. Says nothing about writes."""
        return bool(self.shop_domain) and self.resolver.has(self.token_ref)

    @property
    def endpoint(self) -> str:
        return f"https://{self.shop_domain}/admin/api/{SHOPIFY_API_VERSION}/graphql.json"

    def execute(self, query: str, variables: dict | None = None) -> dict:
        """POST a GraphQL document and return ``data``, raising on any error.

        GraphQL answers 200 for business-logic failures, so both the transport
        ``errors`` array and every mutation's ``userErrors`` have to be checked
        explicitly. Treating a 200 as success is the classic way to believe a
        fulfilment succeeded when it did not.

        This is the raw path and does not consult ``dry_run`` -- reads come
        through here directly, writes come through :meth:`mutate`.
        """
        if not self.shop_domain:
            raise ChannelError("ShopifyConnector needs shop_domain, e.g. my-shop.myshopify.com")
        if not self.limiter.allow():
            raise ChannelError(
                f"Shopify rate limit reached; retry in {self.limiter.wait_time():.1f}s"
            )
        body = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Shopify-Access-Token": self.resolver.resolve(self.token_ref),
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # pragma: no cover - needs a live shop
            raise ChannelError(f"Shopify returned HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:  # pragma: no cover - needs a network
            raise ChannelError(f"could not reach Shopify: {exc.reason}") from exc

        if payload.get("errors"):
            raise ChannelError(f"Shopify GraphQL errors: {payload['errors']}")
        return payload.get("data") or {}

    def mutate(
        self, query: str, variables: dict, operation: str, synthetic: str = ""
    ) -> dict | None:
        """The single chokepoint for every write.

        Returns ``None`` when the call was rehearsed rather than sent, which
        callers must handle -- an unchecked ``None`` is a loud failure rather
        than a silent write.
        """
        if self.dry_run:
            self._counter += 1
            fake = synthetic or f"gid://shopify/DryRun/{self._counter:05d}"
            call = DryRunCall(operation=operation, variables=variables, synthetic_id=fake)
            self.rehearsed.append(call)
            if self.audit is not None:
                self.audit.record(
                    actor="shopify",
                    action="channel_dry_run",
                    subject=operation,
                    reasoning=f"rehearsed {operation}; nothing was sent to {self.shop_domain}",
                    metadata={"synthetic_id": fake, "dry_run": True},
                )
            return None
        return self.execute(query, variables)

    @staticmethod
    def raise_user_errors(node: dict, action: str) -> None:
        errors = node.get("userErrors") or []
        if errors:
            detail = "; ".join(f"{e.get('field')}: {e.get('message')}" for e in errors)
            raise ChannelError(f"Shopify rejected {action}: {detail}")

    # -- discovery ---------------------------------------------------------

    def default_location(self) -> str:
        """The location that holds stock, discovered rather than configured.

        Prefers one that actually ships inventory. Cached, because it does not
        change between calls and every inventory write needs it.
        """
        if self._location_id:
            return self._location_id
        nodes = (self.execute(LOCATIONS_QUERY).get("locations") or {}).get("nodes") or []
        active = [n for n in nodes if n.get("isActive")]
        if not active:
            raise ChannelError(
                f"{self.shop_domain} has no active location, so inventory cannot be set. "
                "Add one under Settings -> Locations."
            )
        shipping = [n for n in active if n.get("shipsInventory")]
        chosen = (shipping or active)[0]
        self._location_id = chosen["id"]
        return self._location_id

    @staticmethod
    def _publication_title(node: dict) -> str:
        """A publication's human name, from whichever field the shop populates."""
        catalog = node.get("catalog") or {}
        return catalog.get("title") or node.get("name") or ""

    def online_store_publication(self) -> str | None:
        """The Online Store publication id, or ``None`` if the shop has none."""
        if self._publication_id:
            return self._publication_id
        nodes = (self.execute(PUBLICATIONS_QUERY).get("publications") or {}).get("nodes") or []
        match = next(
            (n for n in nodes if self._publication_title(n) == "Online Store"), None
        )
        if match is None:
            return None
        self._publication_id = match["id"]
        self._auto_publishes = bool(match.get("autoPublish"))
        return self._publication_id

    # -- listings ----------------------------------------------------------

    def product_set_input(self, listing: Listing) -> dict[str, Any]:
        """Build the ``ProductSetInput`` for a listing.

        Pulled out so the payload can be inspected and asserted on without a
        shop -- which is what makes the dry run reviewable and the tests
        hermetic.
        """
        variant: dict[str, Any] = {
            "optionValues": [{"optionName": "Title", "name": "Default Title"}],
            "sku": listing.sku,
            "price": f"{listing.price.dollars:.2f}",
            "inventoryItem": {"tracked": True},
            "inventoryQuantities": [
                {
                    "locationId": self.default_location(),
                    "name": "available",
                    "quantity": max(0, listing.quantity),
                }
            ],
        }
        payload: dict[str, Any] = {
            "title": listing.title,
            "descriptionHtml": self._description_html(listing),
            "status": "ACTIVE",
            "productOptions": [{"name": "Title", "values": [{"name": "Default Title"}]}],
            "variants": [variant],
        }
        # Vendor is the supplier, which is what you want to filter and report on
        # when a supplier goes bad. ``productType`` is deliberately omitted:
        # Listing does not carry a category, and inventing one would put a wrong
        # value on a live product rather than leave the field empty.
        if listing.supplier_id:
            payload["vendor"] = listing.supplier_id
        return payload

    @staticmethod
    def _description_html(listing: Listing) -> str:
        """Description plus bullets, as the HTML Shopify expects."""
        if not listing.bullets:
            return f"<p>{listing.description}</p>"
        items = "".join(f"<li>{b}</li>" for b in listing.bullets)
        return f"<p>{listing.description}</p><ul>{items}</ul>"

    @staticmethod
    def pounds_from_grams(grams: int) -> float:
        """Shopify stores weight in the shop's unit; ours is grams."""
        return round(grams / GRAMS_PER_POUND, 4)

    def publish_listing(self, listing: Listing) -> str:
        """Create a *sellable* product: variant, price, SKU and stock in one call."""
        variables = {"input": self.product_set_input(listing)}
        data = self.mutate(
            PRODUCT_SET_MUTATION, variables, "productSet", synthetic=f"dry-run:{listing.sku}"
        )
        if data is None:
            listing.external_id = self.rehearsed[-1].synthetic_id
            return listing.external_id

        node = data.get("productSet") or {}
        self.raise_user_errors(node, "productSet")
        product = node.get("product") or {}
        external_id = product.get("id", "")
        if not external_id:
            raise ChannelError(f"productSet returned no product id for {listing.sku}")
        listing.external_id = external_id
        listing.state = ListingState.PUBLISHED
        self.publish(external_id)
        return external_id

    def publish(self, product_id: str) -> bool:
        """Make a product visible on the Online Store.

        ``status: ACTIVE`` is necessary but not sufficient -- a product that is
        not published to a sales channel is active and invisible.
        """
        publication_id = self.online_store_publication()
        if publication_id is None:
            return False
        if self._auto_publishes:
            return True  # the channel publishes on create; a second write is noise
        data = self.mutate(
            PUBLISHABLE_PUBLISH_MUTATION,
            {"id": product_id, "input": [{"publicationId": publication_id}]},
            "publishablePublish",
        )
        if data is None:
            return False
        self.raise_user_errors(data.get("publishablePublish") or {}, "publishablePublish")
        return True

    # -- inventory ---------------------------------------------------------

    def inventory_item_for(self, sku: str) -> str | None:
        """Resolve a SKU to its inventory item id."""
        nodes = (
            self.execute(VARIANT_BY_SKU_QUERY, {"query": f"sku:{sku}"}).get("productVariants")
            or {}
        ).get("nodes") or []
        if not nodes:
            return None
        return ((nodes[0] or {}).get("inventoryItem") or {}).get("id")

    def update_inventory(self, sku: str, quantity: int) -> bool:
        """Set available stock for a SKU at the discovered location.

        Guarded for idempotency on the exact (sku, quantity) pair, so a retried
        webhook cannot apply the same correction twice.
        """
        key = idempotency_key("shopify.inventory", sku, quantity)
        if self.guard.seen(key):
            return bool(self.guard.result_for(key))

        item_id = self.inventory_item_for(sku)
        if item_id is None:
            return False
        variables = {
            "input": {
                "name": "available",
                "reason": "correction",
                "ignoreCompareQuantity": True,
                "quantities": [
                    {
                        "inventoryItemId": item_id,
                        "locationId": self.default_location(),
                        "quantity": max(0, quantity),
                    }
                ],
            }
        }
        data = self.mutate(INVENTORY_SET_MUTATION, variables, "inventorySetQuantities")
        if data is None:
            return False
        self.raise_user_errors(data.get("inventorySetQuantities") or {}, "inventorySetQuantities")
        self.guard.claim(key)
        self.guard.record(key, True)
        return True

    # -- orders ------------------------------------------------------------

    def fetch_orders(self, since: datetime | None = None) -> list[Order]:  # pragma: no cover
        raise NotImplementedError(
            "Order intake is phase 2b: poll orders(query: 'created_at:>...') and map "
            "the payload onto domain.Order, with an orders/create webhook whose HMAC "
            "is checked by security.verify_webhook before the body is believed. "
            "Until then this channel runs against InMemoryChannel."
        )

    def acknowledge_order(self, order: Order) -> bool:
        return True  # Shopify has no separate acknowledgement step.

    def submit_tracking(self, order: Order, shipment: Shipment) -> bool:
        """Fulfil the order and attach tracking, exactly once per shipment."""
        key = idempotency_key("shopify.fulfil", order.external_id, shipment.tracking_number)
        if self.guard.seen(key):
            return bool(self.guard.result_for(key))

        data = self.execute(FULFILLMENT_ORDERS_QUERY, {"id": order.external_id})
        nodes = (((data.get("order") or {}).get("fulfillmentOrders") or {}).get("nodes")) or []
        open_orders = [n for n in nodes if n.get("status") in ("OPEN", "IN_PROGRESS")]
        if not open_orders:
            raise ChannelError(f"no open fulfillment orders on {order.external_id}")

        line_items = [
            {
                "fulfillmentOrderId": node["id"],
                "fulfillmentOrderLineItems": [
                    {"id": li["id"], "quantity": li["remainingQuantity"]}
                    for li in (node.get("lineItems") or {}).get("nodes", [])
                    if li.get("remainingQuantity", 0) > 0
                ],
            }
            for node in open_orders
        ]
        variables = {
            "fulfillment": {
                "lineItemsByFulfillmentOrder": line_items,
                "notifyCustomer": True,
                "trackingInfo": {
                    "company": shipment.carrier,
                    "number": shipment.tracking_number,
                    "url": shipment.tracking_url,
                },
            }
        }
        result = self.mutate(FULFILLMENT_CREATE_MUTATION, variables, "fulfillmentCreate")
        if result is None:
            return False
        node = result.get("fulfillmentCreate") or {}
        self.raise_user_errors(node, "fulfillmentCreate")
        succeeded = (node.get("fulfillment") or {}).get("status") == "SUCCESS"
        self.guard.claim(key)
        self.guard.record(key, succeeded)
        return succeeded

    # -- reporting ---------------------------------------------------------

    def rehearsal_report(self) -> str:
        """Everything a dry run would have sent, for the owner to read."""
        if not self.rehearsed:
            return "No writes were attempted."
        lines = [f"{len(self.rehearsed)} write(s) rehearsed, none sent:"]
        lines.extend(f"  {call.describe()}" for call in self.rehearsed)
        return "\n".join(lines)
