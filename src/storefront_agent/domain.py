"""Core vocabulary: money, products, listings, orders, purchase orders.

Everything above this module speaks these types. Two decisions here shape the
rest of the system:

* **Money is integer minor units.** A dropship margin is often a few percent;
  binary floating point loses cents and the loss compounds across fees, refunds
  and multi-currency. :class:`Money` never converts to ``float`` internally.
* **State lives on the aggregate, transitions live in the engines.** An
  :class:`Order` knows its :class:`OrderState` but will not move itself; only
  ``orders.py`` may transition it, so every change passes one audited chokepoint.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum


class Channel(str, Enum):
    """A storefront we sell through."""

    SHOPIFY = "shopify"
    AMAZON = "amazon"
    EBAY = "ebay"

    @property
    def label(self) -> str:
        return {"shopify": "Shopify", "amazon": "Amazon", "ebay": "eBay"}[self.value]


# -- money -----------------------------------------------------------------


@dataclass(frozen=True, order=True)
class Money:
    """An exact amount, held as integer minor units (cents).

    Arithmetic between different currencies raises rather than guessing at a
    conversion rate -- a silently wrong exchange rate is worse than a crash.
    """

    cents: int
    currency: str = "USD"

    def __post_init__(self) -> None:
        if not isinstance(self.cents, int):
            raise TypeError(f"Money.cents must be int minor units, got {type(self.cents).__name__}")
        if not self.currency or len(self.currency) != 3:
            raise ValueError(f"currency must be a 3-letter code, got {self.currency!r}")

    @classmethod
    def zero(cls, currency: str = "USD") -> "Money":
        return cls(0, currency)

    @classmethod
    def from_dollars(cls, amount: float | int | str, currency: str = "USD") -> "Money":
        """Build from a major-unit amount, rounding half-up to the cent."""
        from decimal import ROUND_HALF_UP, Decimal

        quantised = (Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return cls(int(quantised), currency)

    def _check(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise ValueError(f"cannot combine {self.currency} with {other.currency}")

    def __add__(self, other: "Money") -> "Money":
        self._check(other)
        return Money(self.cents + other.cents, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._check(other)
        return Money(self.cents - other.cents, self.currency)

    def __mul__(self, factor: int | float) -> "Money":
        """Scale, rounding half-up. Used for quantities and fee rates."""
        from decimal import ROUND_HALF_UP, Decimal

        scaled = (Decimal(self.cents) * Decimal(str(factor))).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        return Money(int(scaled), self.currency)

    __rmul__ = __mul__

    def __neg__(self) -> "Money":
        return Money(-self.cents, self.currency)

    @property
    def dollars(self) -> float:
        """Major units. Presentation only -- never feed this back into maths."""
        return self.cents / 100

    def is_zero(self) -> bool:
        return self.cents == 0

    def ratio_to(self, other: "Money") -> float:
        """``self / other`` as a float, or 0.0 when ``other`` is zero."""
        self._check(other)
        return self.cents / other.cents if other.cents else 0.0

    def __str__(self) -> str:
        sign = "-" if self.cents < 0 else ""
        whole, part = divmod(abs(self.cents), 100)
        symbol = {"USD": "$", "GBP": "£", "EUR": "€"}.get(self.currency, f"{self.currency} ")
        return f"{sign}{symbol}{whole:,}.{part:02d}"


# -- supply side -----------------------------------------------------------


@dataclass(frozen=True)
class Supplier:
    """Where stock actually comes from.

    ``ships_blind`` matters more than it looks: Amazon's drop shipping policy
    requires you to be the seller of record on the packing slip. A supplier who
    cannot ship blind disqualifies the product on that channel, and
    ``policy.py`` enforces exactly that.
    """

    id: str
    name: str
    country: str = "US"
    lead_time_days: int = 3
    ships_blind: bool = True
    is_wholesale: bool = True
    api_enabled: bool = False
    rating: float = 4.5

    def total_days_to_door(self, transit_days: int) -> int:
        return self.lead_time_days + transit_days


@dataclass(frozen=True)
class Product:
    """A physical thing, independent of who sells it or where."""

    id: str
    title: str
    category: str
    brand: str = "generic"
    attributes: dict[str, str] = field(default_factory=dict)
    weight_grams: int = 500

    def describe(self) -> str:
        return f"{self.title} ({self.category})"


@dataclass(frozen=True)
class SourcedCandidate:
    """A product from a specific supplier at a specific cost, with signals.

    This is what ``research.py`` produces and ``policy.py`` filters. Demand and
    competition are normalised 0-1 so sources with different native scales stay
    comparable.
    """

    product: Product
    supplier: Supplier
    unit_cost: Money
    ship_cost: Money
    transit_days: int = 7
    moq: int = 1
    demand_signal: float = 0.5
    competition: float = 0.5
    source_name: str = "seed"

    @property
    def landed_cost(self) -> Money:
        """What one unit actually costs us, delivered."""
        return self.unit_cost + self.ship_cost

    @property
    def days_to_door(self) -> int:
        return self.supplier.total_days_to_door(self.transit_days)

    def key(self) -> tuple[str, str]:
        """Identity for de-duplicating the same product across sources."""
        return (self.product.title.strip().lower(), self.supplier.id)


# -- sell side -------------------------------------------------------------


class ListingState(str, Enum):
    DRAFT = "draft"
    BLOCKED = "blocked"
    PUBLISHED = "published"
    PAUSED = "paused"


@dataclass
class Listing:
    """One product offered on one channel."""

    sku: str
    channel: Channel
    title: str
    description: str
    price: Money
    landed_cost: Money
    quantity: int = 0
    bullets: tuple[str, ...] = ()
    supplier_id: str = ""
    promised_days: int = 7
    state: ListingState = ListingState.DRAFT
    external_id: str | None = None
    blocked_reason: str = ""

    @property
    def unit_margin(self) -> Money:
        """Gross margin before channel fees. ``catalog.py`` nets fees off price."""
        return self.price - self.landed_cost

    @property
    def margin_rate(self) -> float:
        return self.unit_margin.ratio_to(self.price)

    def describe(self) -> str:
        return (
            f"{self.channel.label} {self.sku}: {self.title[:48]} @ {self.price} "
            f"({self.margin_rate:.0%} margin)"
        )


@dataclass(frozen=True)
class Address:
    name: str
    line1: str
    city: str
    postal_code: str
    country: str = "US"
    region: str = ""

    def one_line(self) -> str:
        parts = [self.line1, self.city, self.region, self.postal_code, self.country]
        return ", ".join(p for p in parts if p)


@dataclass(frozen=True)
class Customer:
    id: str
    email: str
    name: str = ""
    prior_orders: int = 0


@dataclass(frozen=True)
class OrderLine:
    sku: str
    quantity: int
    unit_price: Money
    unit_cost: Money

    def __post_init__(self) -> None:
        if self.quantity < 1:
            raise ValueError(f"order line quantity must be positive, got {self.quantity}")

    @property
    def revenue(self) -> Money:
        return self.unit_price * self.quantity

    @property
    def cost(self) -> Money:
        return self.unit_cost * self.quantity


class OrderState(str, Enum):
    """Where an order sits in the pipeline.

    ``HELD`` is the risk branch and ``CANCELLED`` the terminal exit; both are
    reachable from most live states. ``orders.py`` owns the legal transitions.
    """

    RECEIVED = "received"
    VALIDATED = "validated"
    RISK_CLEARED = "risk_cleared"
    PO_DRAFTED = "po_drafted"
    PO_APPROVED = "po_approved"
    PLACED = "placed"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    HELD = "held"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (OrderState.DELIVERED, OrderState.CANCELLED)


@dataclass
class Order:
    """A customer order landed from one of the channels."""

    id: str
    channel: Channel
    external_id: str
    placed_at: datetime
    lines: tuple[OrderLine, ...]
    ship_to: Address
    customer: Customer
    shipping_paid: Money = Money(0)
    state: OrderState = OrderState.RECEIVED
    hold_reason: str = ""
    risk_score: float = 0.0
    purchase_order_id: str | None = None
    shipment: "Shipment | None" = None

    @property
    def revenue(self) -> Money:
        total = self.shipping_paid
        for line in self.lines:
            total = total + line.revenue
        return total

    @property
    def cost(self) -> Money:
        total = Money.zero(self.revenue.currency)
        for line in self.lines:
            total = total + line.cost
        return total

    @property
    def margin(self) -> Money:
        return self.revenue - self.cost

    @property
    def units(self) -> int:
        return sum(line.quantity for line in self.lines)

    def describe(self) -> str:
        return (
            f"{self.channel.label} {self.external_id}: {self.units} unit(s), "
            f"{self.revenue} rev / {self.margin} margin [{self.state.value}]"
        )


# -- fulfilment ------------------------------------------------------------


class POState(str, Enum):
    DRAFT = "draft"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    PLACED = "placed"
    REJECTED = "rejected"


@dataclass(frozen=True)
class POLine:
    sku: str
    quantity: int
    unit_cost: Money
    order_id: str

    @property
    def cost(self) -> Money:
        return self.unit_cost * self.quantity


@dataclass
class PurchaseOrder:
    """What we buy from a supplier to fulfil one or more customer orders.

    Batched per supplier: three orders for the same supplier in one check-in
    window become one PO, which is both cheaper and fewer things for the owner
    to look at.
    """

    id: str
    supplier_id: str
    lines: tuple[POLine, ...]
    created_at: datetime
    state: POState = POState.DRAFT
    approved_by: str = ""
    rejection_reason: str = ""
    external_ref: str = ""

    @property
    def total(self) -> Money:
        total = Money.zero()
        for line in self.lines:
            total = total + line.cost
        return total

    @property
    def order_ids(self) -> tuple[str, ...]:
        seen: list[str] = []
        for line in self.lines:
            if line.order_id not in seen:
                seen.append(line.order_id)
        return tuple(seen)

    def describe(self) -> str:
        return (
            f"{self.id}: {self.total} to {self.supplier_id} "
            f"covering {len(self.order_ids)} order(s) [{self.state.value}]"
        )


@dataclass(frozen=True)
class Shipment:
    carrier: str
    tracking_number: str
    shipped_at: datetime
    tracking_url: str = ""
    delivered_at: datetime | None = None

    def describe(self) -> str:
        return f"{self.carrier} {self.tracking_number}"


# -- the world -------------------------------------------------------------


@dataclass(frozen=True)
class Storefront:
    """One connected shop."""

    channel: Channel
    name: str
    currency: str = "USD"
    fee_rate: float = 0.029
    fee_fixed: Money = Money(30)

    def fee_on(self, price: Money) -> Money:
        """Channel's cut of a sale at ``price``."""
        return price * self.fee_rate + self.fee_fixed

    @classmethod
    def for_channel(cls, channel: Channel) -> "Storefront":
        """A storefront with fees roughly matching the marketplace's published rates.

        The spread is the point: a ~15% Amazon referral fee against ~2.9% + 30c
        on Shopify Payments is why the same product cannot carry one price
        across channels. ``catalog.py`` solves each price against these.
        """
        if channel is Channel.AMAZON:
            return cls(channel, "Amazon", fee_rate=0.15, fee_fixed=Money(0))
        if channel is Channel.EBAY:
            return cls(channel, "eBay", fee_rate=0.1325, fee_fixed=Money(30))
        return cls(channel, "Shopify", fee_rate=0.029, fee_fixed=Money(30))


@dataclass
class StorefrontContext:
    """Everything the agents operate on."""

    storefronts: list[Storefront] = field(default_factory=list)
    suppliers: list[Supplier] = field(default_factory=list)
    listings: list[Listing] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    purchase_orders: list[PurchaseOrder] = field(default_factory=list)

    def storefront_for(self, channel: Channel) -> Storefront | None:
        return next((s for s in self.storefronts if s.channel is channel), None)

    def supplier_by_id(self, supplier_id: str) -> Supplier | None:
        return next((s for s in self.suppliers if s.id == supplier_id), None)

    def order_by_id(self, order_id: str) -> Order | None:
        return next((o for o in self.orders if o.id == order_id), None)

    def po_by_id(self, po_id: str) -> PurchaseOrder | None:
        return next((p for p in self.purchase_orders if p.id == po_id), None)

    def listing_by_sku(self, sku: str, channel: Channel | None = None) -> Listing | None:
        for listing in self.listings:
            if listing.sku == sku and (channel is None or listing.channel is channel):
                return listing
        return None

    def orders_in(self, *states: OrderState) -> list[Order]:
        wanted = set(states)
        return [o for o in self.orders if o.state in wanted]

    def channels(self) -> tuple[Channel, ...]:
        return tuple(s.channel for s in self.storefronts)


__all__ = [
    "Address", "Channel", "Customer", "Listing", "ListingState", "Money", "Order",
    "OrderLine", "OrderState", "POLine", "POState", "Product", "PurchaseOrder",
    "Shipment", "SourcedCandidate", "Storefront", "StorefrontContext", "Supplier",
    "replace",
]
