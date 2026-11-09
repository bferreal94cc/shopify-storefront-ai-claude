"""Seed data: a small business that exercises every branch.

The catalogue is chosen so a demo run hits the interesting paths rather than
the happy one only. It contains a compliant wholesaler, a retail source that
will not ship blind (blocked on Amazon and eBay, fine on Shopify), a slow
supplier that fails Amazon's handling-time limit, and a thin-margin item that
lands in the approval queue instead of going live.

The orders do the same job: one clean, one that oversells stock, one that trips
the fraud screen, and one large enough to push its purchase order over the
auto-approve cap. A run over this data produces a check-in digest with
something in every section.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .channels import InMemoryChannel
from .domain import (
    Address,
    Channel,
    Customer,
    Money,
    Order,
    OrderLine,
    Product,
    Shipment,
    SourcedCandidate,
    StorefrontContext,
    Supplier,
)
from .orchestrator import Orchestrator
from .policy import AutomationPolicy
from .providers import LLMProvider, provider_from_env
from .research import SeedCatalogSource

DEMO_START = datetime(2026, 8, 21, 9, 0)


def build_suppliers() -> dict[str, Supplier]:
    return {
        "wholesale": Supplier(
            id="sup-wholesale",
            name="Meridian Wholesale",
            country="US",
            lead_time_days=2,
            ships_blind=True,
            is_wholesale=True,
            rating=4.6,
        ),
        "retail": Supplier(
            id="sup-retail",
            name="BigBox Retail",
            country="US",
            lead_time_days=1,
            ships_blind=False,   # blocked on Amazon: packing slip carries their brand
            is_wholesale=False,  # blocked on eBay: retail arbitrage
            rating=4.9,
        ),
        "slow": Supplier(
            id="sup-slow",
            name="Overseas Direct",
            country="CN",
            lead_time_days=6,
            ships_blind=True,
            is_wholesale=True,
            rating=4.1,
        ),
    }


def build_catalogue() -> list[SourcedCandidate]:
    """A desk-accessories catalogue with one item per interesting branch."""
    suppliers = build_suppliers()
    return [
        SourcedCandidate(
            product=Product(
                "p-lamp", "LED Desk Lamp", "lighting", "Lumen",
                {"finish": "matte black", "power": "USB-C"},
            ),
            supplier=suppliers["wholesale"],
            unit_cost=Money.from_dollars(6.20),
            ship_cost=Money.from_dollars(1.80),
            transit_days=4,
            demand_signal=0.82,
            competition=0.35,
        ),
        SourcedCandidate(
            product=Product(
                "p-organiser", "Desk Cable Organiser", "office", "Tidy",
                {"material": "silicone"},
            ),
            supplier=suppliers["wholesale"],
            unit_cost=Money.from_dollars(2.10),
            ship_cost=Money.from_dollars(1.10),
            transit_days=4,
            demand_signal=0.61,
            competition=0.58,
        ),
        SourcedCandidate(
            product=Product(
                "p-monitor", "Monitor Riser Shelf", "office", "Elevate",
                {"material": "bamboo"},
            ),
            supplier=suppliers["retail"],  # Shopify only
            unit_cost=Money.from_dollars(14.00),
            ship_cost=Money.from_dollars(4.50),
            transit_days=3,
            demand_signal=0.74,
            competition=0.44,
        ),
        SourcedCandidate(
            product=Product(
                "p-ringlight", "Ring Light with Stand", "lighting", "Halo",
                {"diameter": "10 inch"},
            ),
            supplier=suppliers["slow"],  # 26 days: over Amazon's handling limit
            unit_cost=Money.from_dollars(11.00),
            ship_cost=Money.from_dollars(4.00),
            transit_days=20,
            demand_signal=0.90,
            competition=0.81,
        ),
        SourcedCandidate(
            product=Product(
                "p-mousepad", "Large Desk Mat", "office", "Flat",
                {"size": "900x400mm"},
            ),
            supplier=suppliers["slow"],
            unit_cost=Money.from_dollars(9.50),
            ship_cost=Money.from_dollars(3.20),
            transit_days=9,
            demand_signal=0.55,
            competition=0.72,
        ),
    ]


def build_orchestrator(
    provider: LLMProvider | None = None,
    policy: AutomationPolicy | None = None,
) -> Orchestrator:
    """A fully wired orchestrator over the seed catalogue and in-memory channels."""
    return Orchestrator(
        context=StorefrontContext(),
        policy=policy or AutomationPolicy(),
        provider=provider or provider_from_env(),
        sources=[SeedCatalogSource(build_catalogue())],
        connectors={c: InMemoryChannel(channel=c) for c in Channel},
    )


def seed_orders(agent: Orchestrator, at: datetime | None = None) -> int:
    """Drop customer orders into the channel inboxes. Returns how many.

    Called after listings publish, because an order for an unlisted SKU is a
    validation failure rather than a useful test.
    """
    moment = at or DEMO_START
    live = [l for l in agent.context.listings if l.state.value == "published"]
    if not live:
        return 0
    by_channel: dict[Channel, list] = {}
    for listing in live:
        by_channel.setdefault(listing.channel, []).append(listing)

    seeded = 0
    for index, (channel, listings) in enumerate(sorted(by_channel.items(), key=lambda kv: kv[0].value)):
        connector = agent.connectors.get(channel)
        if connector is None or not isinstance(connector, InMemoryChannel):
            continue
        first = listings[0]
        for order in _orders_for(channel, first, listings, moment, index):
            connector.seed_order(order)
            seeded += 1
    return seeded


def _orders_for(channel, first, listings, moment, index):
    """Four orders per channel, each exercising a different branch."""
    stamp = f"{channel.value[:3]}{index}"
    normal = Order(
        id=f"ord-{stamp}-1",
        channel=channel,
        external_id=f"#{1000 + index * 10 + 1}",
        placed_at=moment + timedelta(minutes=5),
        lines=(OrderLine(first.sku, 1, first.price, first.landed_cost),),
        ship_to=Address("Dana Whitfield", "18 Harper Lane", "Austin", "78701", "US", "TX"),
        customer=Customer(f"cus-{stamp}-1", "dana.whitfield@example.com", "Dana Whitfield", 4),
        shipping_paid=Money.from_dollars(4.95),
    )
    # Quantity far above stock: held at validation.
    oversell = Order(
        id=f"ord-{stamp}-2",
        channel=channel,
        external_id=f"#{1000 + index * 10 + 2}",
        placed_at=moment + timedelta(minutes=12),
        lines=(OrderLine(first.sku, 400, first.price, first.landed_cost),),
        ship_to=Address("Priya Raman", "9 Kestrel Way", "Denver", "80202", "US", "CO"),
        customer=Customer(f"cus-{stamp}-2", "priya.raman@example.com", "Priya Raman", 1),
    )
    # Freight forwarder, mismatched name, brand-new account: held on risk.
    risky = Order(
        id=f"ord-{stamp}-3",
        channel=channel,
        external_id=f"#{1000 + index * 10 + 3}",
        placed_at=moment + timedelta(minutes=20),
        lines=(OrderLine(first.sku, 3, first.price, first.landed_cost),),
        ship_to=Address("Unknown Recipient", "Freight Forward Depot 14", "Lagos", "100001", "NG"),
        customer=Customer(f"cus-{stamp}-3", "quickbuy9982@example.com", "Marcus Webb", 0),
    )
    orders = [normal, oversell, risky]
    # A bulk order big enough to push its PO past the auto-approve cap.
    if len(listings) > 1:
        second = listings[1]
        orders.append(
            Order(
                id=f"ord-{stamp}-4",
                channel=channel,
                external_id=f"#{1000 + index * 10 + 4}",
                placed_at=moment + timedelta(minutes=28),
                lines=(
                    OrderLine(first.sku, 6, first.price, first.landed_cost),
                    OrderLine(second.sku, 9, second.price, second.landed_cost),
                ),
                ship_to=Address("Owen Castellanos", "402 Mill Street", "Portland", "97201", "US", "OR"),
                customer=Customer(f"cus-{stamp}-4", "owen.c@example.com", "Owen Castellanos", 11),
                shipping_paid=Money.from_dollars(9.95),
            )
        )
    return orders


def seed_tracking(agent: Orchestrator, at: datetime | None = None) -> int:
    """Supplier tracking for everything currently placed."""
    moment = at or DEMO_START
    queued = 0
    for order in agent.context.orders:
        if order.state.value != "placed":
            continue
        queued += 1
        agent.receive_tracking(
            order.id,
            Shipment(
                carrier="UPS",
                tracking_number=f"1Z{order.id.replace('-', '').upper()[:12]}",
                shipped_at=moment,
                tracking_url="https://www.ups.com/track",
            ),
        )
    return queued
