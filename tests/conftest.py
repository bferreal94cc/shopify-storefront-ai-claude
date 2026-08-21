"""Small builders so each test states only what it actually cares about."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from storefront_agent.channels import InMemoryChannel
from storefront_agent.domain import (
    Address,
    Channel,
    Customer,
    Listing,
    Money,
    Order,
    OrderLine,
    Product,
    SourcedCandidate,
    Storefront,
    StorefrontContext,
    Supplier,
)
from storefront_agent.orchestrator import Orchestrator
from storefront_agent.policy import AutomationPolicy
from storefront_agent.providers import StubProvider
from storefront_agent.research import SeedCatalogSource

NOW = datetime(2026, 8, 21, 9, 0)


def dollars(amount) -> Money:
    return Money.from_dollars(amount)


def make_supplier(
    supplier_id: str = "sup-1",
    *,
    blind: bool = True,
    wholesale: bool = True,
    rating: float = 4.5,
    lead_time_days: int = 2,
) -> Supplier:
    return Supplier(
        id=supplier_id,
        name=supplier_id.upper(),
        lead_time_days=lead_time_days,
        ships_blind=blind,
        is_wholesale=wholesale,
        rating=rating,
    )


def make_candidate(
    product_id: str = "p-1",
    *,
    title: str = "LED Desk Lamp",
    category: str = "lighting",
    supplier: Supplier | None = None,
    unit_cost=6.20,
    ship_cost=1.80,
    transit_days: int = 4,
    demand: float = 0.7,
    competition: float = 0.4,
) -> SourcedCandidate:
    return SourcedCandidate(
        product=Product(product_id, title, category),
        supplier=supplier or make_supplier(),
        unit_cost=dollars(unit_cost),
        ship_cost=dollars(ship_cost),
        transit_days=transit_days,
        demand_signal=demand,
        competition=competition,
    )


def make_listing(
    sku: str = "SKU-1",
    *,
    channel: Channel = Channel.SHOPIFY,
    price=24.99,
    cost=8.00,
    quantity: int = 25,
    supplier_id: str = "sup-1",
    promised_days: int = 6,
) -> Listing:
    return Listing(
        sku=sku,
        channel=channel,
        title=f"Product {sku}",
        description="A description.",
        price=dollars(price),
        landed_cost=dollars(cost),
        quantity=quantity,
        bullets=("A useful benefit", "Another benefit"),
        supplier_id=supplier_id,
        promised_days=promised_days,
    )


def make_order(
    order_id: str = "ord-1",
    *,
    channel: Channel = Channel.SHOPIFY,
    sku: str = "SKU-1",
    quantity: int = 1,
    price=24.99,
    cost=8.00,
    at: datetime | None = None,
    email: str = "dana@example.com",
    name: str = "Dana Whitfield",
    ship_name: str | None = None,
    country: str = "US",
    line1: str = "18 Harper Lane",
    city: str = "Austin",
    postal: str = "78701",
    prior_orders: int = 4,
) -> Order:
    return Order(
        id=order_id,
        channel=channel,
        external_id=f"#{order_id}",
        placed_at=at or NOW,
        lines=(OrderLine(sku, quantity, dollars(price), dollars(cost)),),
        ship_to=Address(ship_name or name, line1, city, postal, country),
        customer=Customer(f"cus-{order_id}", email, name, prior_orders),
    )


def make_context(
    listings: list[Listing] | None = None,
    channels: tuple[Channel, ...] = (Channel.SHOPIFY,),
) -> StorefrontContext:
    return StorefrontContext(
        storefronts=[Storefront(c, c.label) for c in channels],
        suppliers=[make_supplier()],
        listings=list(listings or []),
    )


@pytest.fixture
def context() -> StorefrontContext:
    """One Shopify storefront with one well-stocked listing."""
    return make_context([make_listing()])


@pytest.fixture
def policy() -> AutomationPolicy:
    return AutomationPolicy()


@pytest.fixture
def agent() -> Orchestrator:
    """A wired orchestrator over a two-item catalogue and in-memory channels."""
    return Orchestrator(
        context=StorefrontContext(),
        policy=AutomationPolicy(),
        provider=StubProvider(),
        sources=[
            SeedCatalogSource(
                [
                    make_candidate("p-1", title="LED Desk Lamp"),
                    make_candidate(
                        "p-2", title="Desk Cable Organiser", category="office",
                        unit_cost=2.10, ship_cost=1.10,
                    ),
                ]
            )
        ],
        connectors={c: InMemoryChannel(channel=c) for c in Channel},
    )


@pytest.fixture
def later():
    """Helper producing offsets from NOW."""

    def _later(**kwargs) -> datetime:
        return NOW + timedelta(**kwargs)

    return _later
