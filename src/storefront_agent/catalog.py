"""Turning a sourced candidate into listings, one per channel.

Two jobs, and the second one is where the money is.

**Copy.** Titles, bullets and descriptions per channel, because the same product
needs a keyword-dense title on Amazon and a brand-voice one on Shopify. The LLM
writes it; a deterministic fallback covers the no-API-key case so nothing in the
pipeline depends on a model being reachable.

**Price.** The margin floor has to hold *after* the channel takes its cut, and
the cuts differ enormously -- roughly 2.9% + 30c on Shopify Payments against a
~15% referral fee on Amazon. Pricing at a flat markup means a product that
clears 30% on Shopify quietly clears 17% on Amazon. :class:`PricingEngine`
solves for the price that hits the target margin net of fees, per channel, so
the same product is priced differently in each storefront on purpose.
"""

from __future__ import annotations

import math

from dataclasses import dataclass, field

from .domain import (
    Channel,
    Listing,
    ListingState,
    Money,
    SourcedCandidate,
    Storefront,
)
from .policy import AutomationPolicy
from .providers import LLMProvider, StubProvider

# Channel-specific title limits. Over-long titles are silently truncated by the
# marketplace, usually mid-word and usually somewhere embarrassing.
_TITLE_LIMITS: dict[Channel, int] = {
    Channel.SHOPIFY: 70,
    Channel.AMAZON: 200,
    Channel.EBAY: 80,
}

_COPY_SYSTEM = (
    "You write ecommerce listing copy. Return plain text only. Be concrete and "
    "specific about what the product does. Never invent certifications, brand "
    "affiliations, medical claims, or performance numbers you were not given."
)


@dataclass(frozen=True)
class ListingCopy:
    """Channel-shaped words for one product."""

    title: str
    bullets: tuple[str, ...]
    description: str
    source: str = "fallback"


class CopyWriter:
    """Drafts listing copy, with a deterministic fallback.

    The fallback is not a placeholder -- it produces usable copy from the
    product's own attributes, so a run with no API key still yields listings a
    human would recognise rather than lorem ipsum.
    """

    def __init__(self, provider: LLMProvider | None = None) -> None:
        self.provider = provider or StubProvider()

    def write(self, candidate: SourcedCandidate, channel: Channel) -> ListingCopy:
        limit = _TITLE_LIMITS.get(channel, 80)
        prompt = (
            f"Product: {candidate.product.title}\n"
            f"Category: {candidate.product.category}\n"
            f"Attributes: {candidate.product.attributes or 'none supplied'}\n"
            f"Marketplace: {channel.label}\n"
            f"Ships in {candidate.days_to_door} days.\n\n"
            f"Write a listing. Line 1: a title of at most {limit} characters. "
            "Lines 2-5: four benefit bullets. Remaining lines: a two-sentence "
            "description."
        )
        response = self.provider.complete(prompt, system=_COPY_SYSTEM, temperature=0.4)
        lines = response.as_lines()
        if len(lines) >= 6:
            return ListingCopy(
                title=lines[0][:limit],
                bullets=tuple(lines[1:5]),
                description=" ".join(lines[5:]),
                source=self.provider.name,
            )
        return self._fallback(candidate, channel, limit)

    def _fallback(
        self, candidate: SourcedCandidate, channel: Channel, limit: int
    ) -> ListingCopy:
        product = candidate.product
        descriptors = [v for v in product.attributes.values() if v][:2]
        qualifier = " ".join(descriptors)
        title = f"{product.title} {qualifier}".strip()[:limit]
        bullets = (
            f"{product.category.title()} essential, ready to use out of the box",
            f"Ships in about {candidate.days_to_door} days",
            f"Supplied by {candidate.supplier.name}, rated {candidate.supplier.rating:.1f}/5",
            "Covered by our standard returns policy",
        )
        description = (
            f"{product.title} is a {product.category} item"
            f"{' featuring ' + qualifier if qualifier else ''}. "
            f"Dispatched within {candidate.supplier.lead_time_days} days of ordering."
        )
        return ListingCopy(title=title, bullets=bullets, description=description)


# -- pricing ---------------------------------------------------------------


def _ceil_cents(required: float) -> int:
    """Smallest whole cent at or above ``required``, tolerating float dust.

    The margin solve is a float division, so a requirement that is exactly
    1099 cents can arrive as 1098.9999999999 or 1099.0000000001. A bare
    ``math.ceil`` turns the second into 1100 -- a cent of drift that charm
    rounding then amplifies into a whole dollar (10.99 becomes 11.99). The
    epsilon absorbs the dust without ever rounding a genuine fraction down.
    """
    return math.ceil(required - 1e-9)


@dataclass
class PricingEngine:
    """Solves for a sell price that hits a target margin net of channel fees.

    For a channel taking rate ``r`` plus fixed fee ``f`` on a sale at ``p``,
    what lands in the bank is ``p(1 - r) - f``. Requiring the net margin over
    landed cost ``c`` to be at least fraction ``m`` of the sale price gives::

        p(1 - r) - f - c >= m·p
        p >= (f + c) / (1 - r - m)

    When ``1 - r - m`` is zero or negative the target is unreachable on that
    channel at any price, which is a real answer and not an error -- a 25% net
    margin cannot coexist with an 80% referral fee.
    """

    target_margin: float = 0.30
    charm_endings: bool = True

    def price_for(
        self, landed_cost: Money, storefront: Storefront, target_margin: float | None = None
    ) -> Money | None:
        """Cheapest price meeting the target, or ``None`` if unreachable."""
        margin = self.target_margin if target_margin is None else target_margin
        denominator = 1.0 - storefront.fee_rate - margin
        if denominator <= 0:
            return None
        required = (storefront.fee_fixed + landed_cost).cents / denominator
        price = Money(_ceil_cents(required), landed_cost.currency)
        return self._charm(price) if self.charm_endings else price

    def net_proceeds(self, price: Money, storefront: Storefront) -> Money:
        """What actually reaches the bank after the channel's cut."""
        return price - storefront.fee_on(price)

    def net_margin_rate(
        self, price: Money, landed_cost: Money, storefront: Storefront
    ) -> float:
        """Realised margin as a fraction of the sale price, after fees."""
        return (self.net_proceeds(price, storefront) - landed_cost).ratio_to(price)

    @staticmethod
    def _charm(price: Money) -> Money:
        """Round *up* to the next .99, never down -- rounding down eats margin."""
        whole, remainder = divmod(price.cents, 100)
        if remainder > 99:  # pragma: no cover - divmod by 100 caps remainder at 99
            whole += 1
        elif remainder == 99:
            return price
        return Money(whole * 100 + 99, price.currency)


# -- assembly --------------------------------------------------------------


@dataclass
class CatalogBuilder:
    """Builds channel listings from candidates, applying policy as it goes."""

    policy: AutomationPolicy = field(default_factory=AutomationPolicy)
    pricing: PricingEngine = field(default_factory=PricingEngine)
    writer: CopyWriter = field(default_factory=CopyWriter)
    _sku_counter: int = field(default=0, init=False)

    def next_sku(self, candidate: SourcedCandidate, channel: Channel) -> str:
        self._sku_counter += 1
        stem = "".join(ch for ch in candidate.product.id if ch.isalnum()).upper()[:8]
        return f"{stem}-{channel.value[:3].upper()}-{self._sku_counter:04d}"

    def build(
        self,
        candidate: SourcedCandidate,
        storefront: Storefront,
        quantity: int = 25,
    ) -> Listing:
        """One listing for one candidate on one storefront.

        A listing that policy blocks is still returned -- in ``BLOCKED`` state
        with the reason attached -- because the owner needs to see *what was
        rejected and why*, not just a shorter list than they expected.
        """
        channel = storefront.channel
        landed = candidate.landed_cost
        price = self.pricing.price_for(landed, storefront, self.policy.margin_floor)
        copy = self.writer.write(candidate, channel)
        sku = self.next_sku(candidate, channel)

        if price is None:
            return Listing(
                sku=sku,
                channel=channel,
                title=copy.title,
                description=copy.description,
                bullets=copy.bullets,
                price=landed,
                landed_cost=landed,
                quantity=0,
                supplier_id=candidate.supplier.id,
                promised_days=candidate.days_to_door,
                state=ListingState.BLOCKED,
                blocked_reason=(
                    f"A {self.policy.margin_floor:.0%} net margin is unreachable on "
                    f"{channel.label} at a {storefront.fee_rate:.1%} fee rate."
                ),
            )

        decision = self.policy.evaluate_candidate(candidate, channel, price)
        listing = Listing(
            sku=sku,
            channel=channel,
            title=copy.title,
            description=copy.description,
            bullets=copy.bullets,
            price=price,
            landed_cost=landed,
            quantity=quantity if not decision.is_blocked else 0,
            supplier_id=candidate.supplier.id,
            promised_days=candidate.days_to_door,
            state=ListingState.BLOCKED if decision.is_blocked else ListingState.DRAFT,
            blocked_reason=decision.reason if decision.is_blocked else "",
        )
        return listing

    def build_all(
        self,
        candidate: SourcedCandidate,
        storefronts: list[Storefront],
        quantity: int = 25,
    ) -> list[Listing]:
        """One listing per storefront, priced independently."""
        return [self.build(candidate, sf, quantity) for sf in storefronts]
