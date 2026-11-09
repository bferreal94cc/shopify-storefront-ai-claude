"""Storefront connectors: one port, three marketplaces.

Every channel exposes the same five verbs -- publish a listing, adjust
inventory, pull new orders, acknowledge an order, submit tracking. Above this
module nothing knows which marketplace it is talking to, so adding a fourth
channel is a new subclass and nothing else.

:class:`InMemoryChannel` is the reference adapter. It is not a mock: it
implements the full contract, holds real state, and is what the demo and the
tests run against. That is what lets the whole system be exercised end to end
with no accounts and no credentials.

The live Shopify adapter lives in :mod:`storefront_agent.shopify` -- it is the
one that talks to a real storefront and it needs the room. It is still reachable
as ``channels.ShopifyConnector`` for anything that imported it from here.

Amazon and eBay ship as ports with the same surface: their auth flows (LWA and
OAuth respectively) are substantial enough that stubbing them convincingly would
be worse than leaving an honest ``NotImplementedError``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from .domain import Channel, Listing, ListingState, Order, Shipment


class ChannelError(RuntimeError):
    """A marketplace rejected a request or could not be reached."""


class ChannelConnector(ABC):
    """The contract every storefront adapter implements."""

    channel: Channel

    @abstractmethod
    def publish_listing(self, listing: Listing) -> str:
        """Create or update the listing. Returns the marketplace's id."""

    @abstractmethod
    def update_inventory(self, sku: str, quantity: int) -> bool:
        """Set available quantity. Returns whether it took effect."""

    @abstractmethod
    def fetch_orders(self, since: datetime | None = None) -> list[Order]:
        """New orders placed after ``since``."""

    @abstractmethod
    def acknowledge_order(self, order: Order) -> bool:
        """Tell the marketplace we have accepted the order."""

    @abstractmethod
    def submit_tracking(self, order: Order, shipment: Shipment) -> bool:
        """Mark the order shipped and attach tracking."""

    def is_live(self) -> bool:
        """Whether this adapter reaches a real marketplace."""
        return False


# -- reference adapter -----------------------------------------------------


@dataclass
class InMemoryChannel(ChannelConnector):
    """Full-contract adapter backed by dictionaries.

    Records everything it was asked to do so tests and the demo can assert on
    real behaviour rather than on call counts.
    """

    channel: Channel = Channel.SHOPIFY
    published: dict[str, Listing] = field(default_factory=dict)
    inventory: dict[str, int] = field(default_factory=dict)
    inbox: list[Order] = field(default_factory=list)
    acknowledged: list[str] = field(default_factory=list)
    tracking: dict[str, Shipment] = field(default_factory=dict)
    _counter: int = field(default=0, init=False)

    def publish_listing(self, listing: Listing) -> str:
        if listing.state is ListingState.BLOCKED:
            raise ChannelError(f"refusing to publish blocked listing {listing.sku}")
        self._counter += 1
        external_id = f"{self.channel.value}-{self._counter:05d}"
        listing.external_id = external_id
        listing.state = ListingState.PUBLISHED
        self.published[listing.sku] = listing
        self.inventory[listing.sku] = listing.quantity
        return external_id

    def update_inventory(self, sku: str, quantity: int) -> bool:
        if sku not in self.published:
            return False
        self.inventory[sku] = max(0, quantity)
        self.published[sku].quantity = self.inventory[sku]
        return True

    def fetch_orders(self, since: datetime | None = None) -> list[Order]:
        ready = [o for o in self.inbox if since is None or o.placed_at > since]
        self.inbox = [o for o in self.inbox if o not in ready]
        return ready

    def acknowledge_order(self, order: Order) -> bool:
        self.acknowledged.append(order.external_id)
        return True

    def submit_tracking(self, order: Order, shipment: Shipment) -> bool:
        self.tracking[order.external_id] = shipment
        return True

    def seed_order(self, order: Order) -> None:
        """Drop an order into the inbox as though a customer had placed it."""
        self.inbox.append(order)


# -- Amazon and eBay -------------------------------------------------------


@dataclass
class _UnwiredConnector(ChannelConnector):
    """Shared body for adapters that need an auth flow before they can work."""

    channel: Channel = Channel.AMAZON
    service: str = "the marketplace API"
    guidance: str = ""

    def _unwired(self, verb: str) -> "NotImplementedError":
        return NotImplementedError(
            f"{self.channel.label} {verb} is not wired up. {self.guidance} "
            "Until then this channel runs against InMemoryChannel, which "
            "implements the full contract."
        )

    def publish_listing(self, listing: Listing) -> str:
        raise self._unwired("listing publication")

    def update_inventory(self, sku: str, quantity: int) -> bool:
        raise self._unwired("inventory sync")

    def fetch_orders(self, since: datetime | None = None) -> list[Order]:
        raise self._unwired("order polling")

    def acknowledge_order(self, order: Order) -> bool:
        raise self._unwired("order acknowledgement")

    def submit_tracking(self, order: Order, shipment: Shipment) -> bool:
        raise self._unwired("tracking submission")


def amazon_connector() -> _UnwiredConnector:
    """Amazon Selling Partner API port."""
    return _UnwiredConnector(
        channel=Channel.AMAZON,
        service="SP-API",
        guidance=(
            "SP-API needs Login with Amazon: exchange a refresh token for an "
            "access token, then use the Orders and Feeds APIs. Listings go via "
            "the JSON_LISTINGS_FEED, shipping confirmation via "
            "POST_ORDER_FULFILLMENT_DATA. Note that Amazon's drop shipping "
            "policy requires you to be the seller of record on every packing "
            "slip -- policy.py blocks non-compliant candidates before they "
            "reach this connector."
        ),
    )


def ebay_connector() -> _UnwiredConnector:
    """eBay Sell API port."""
    return _UnwiredConnector(
        channel=Channel.EBAY,
        service="Sell API",
        guidance=(
            "eBay needs an OAuth user token. Listings go through the Inventory "
            "API (createOrReplaceInventoryItem, then publishOffer); orders and "
            "shipping come from the Fulfillment API "
            "(getOrders, createShippingFulfillment)."
        ),
    )


def connector_for(channel: Channel, live: bool = False, **kwargs) -> ChannelConnector:
    """Pick an adapter for ``channel``.

    Defaults to the in-memory adapter. Going live is an explicit, per-channel
    decision -- there is no environment variable that silently promotes a demo
    run into one that spends money.
    """
    if not live:
        return InMemoryChannel(channel=channel)
    if channel is Channel.SHOPIFY:
        # Imported here rather than at module scope: shopify.py depends on this
        # module for the port, so a top-level import would be circular.
        from .shopify import ShopifyConnector

        return ShopifyConnector(**kwargs)
    if channel is Channel.AMAZON:
        return amazon_connector()
    return ebay_connector()


def __getattr__(name: str):
    """Keep ``channels.ShopifyConnector`` working after the move.

    The adapter lives in :mod:`storefront_agent.shopify` now. Resolving it
    lazily here means existing imports keep working without reintroducing the
    circular dependency a real re-export would create.
    """
    if name == "ShopifyConnector":
        from .shopify import ShopifyConnector

        return ShopifyConnector
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
