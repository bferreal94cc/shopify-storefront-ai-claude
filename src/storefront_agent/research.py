"""Product research: find things worth selling, and rank them honestly.

A :class:`ProductSource` is any place candidates come from -- a supplier API, a
CSV export, a scraped catalogue. Sources are deliberately dumb: they return
candidates and nothing else. All judgement lives in :class:`ProductResearchAgent`,
so adding a source never changes how products are ranked.

The scoring is a weighted sum of five components, each normalised to 0-1 and
each able to explain itself. A single opaque "score: 0.72" is useless to an
owner deciding what to stock, so :class:`CandidateScore` keeps the breakdown.

One deliberate omission: there is no demand *oracle* here. Real demand signal
comes from a paid keyword or marketplace-analytics API. Sources supply whatever
signal they have and the agent treats it as one input among five rather than
pretending it is ground truth.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .domain import Money, Product, SourcedCandidate, Supplier


class ProductSource(ABC):
    """Somewhere candidates come from."""

    source_name: str = "abstract"

    @abstractmethod
    def search(self, niche: str, limit: int = 20) -> list[SourcedCandidate]:
        """Candidates matching ``niche``, best-effort and unranked."""

    def is_available(self) -> bool:
        """Whether this source can currently be queried."""
        return True


@dataclass
class SeedCatalogSource(ProductSource):
    """Reference source backed by an in-memory catalogue.

    This is what makes the whole system runnable with no accounts: the demo, the
    tests and a first-run dry run all pull from here.
    """

    catalogue: list[SourcedCandidate] = field(default_factory=list)
    source_name: str = "seed"

    def search(self, niche: str, limit: int = 20) -> list[SourcedCandidate]:
        terms = _terms(niche)
        if not terms:
            return list(self.catalogue[:limit])
        scored: list[tuple[int, SourcedCandidate]] = []
        for candidate in self.catalogue:
            haystack = (
                f"{candidate.product.title} {candidate.product.category} "
                f"{candidate.product.brand} {' '.join(candidate.product.attributes.values())}"
            ).lower()
            hits = sum(1 for term in terms if term in haystack)
            if hits:
                scored.append((hits, candidate))
        scored.sort(key=lambda pair: -pair[0])
        return [candidate for _hits, candidate in scored[:limit]]


class PlaywrightSource(ProductSource):
    """Scrapes a supplier catalogue that has no API, via Playwright.

    The sibling ``playwright-mcp`` server in this repository drives the browser.
    Left as a port on purpose: scraping terms differ per supplier, and pointing
    this at a site that forbids it is the operator's decision to make knowingly,
    not a default this package ships turned on.

    Implement :meth:`fetch_rows` against a supplier you are permitted to scrape;
    the parsing and normalising below is then reusable.
    """

    source_name = "playwright"

    def __init__(self, catalogue_url: str = "", enabled: bool = False) -> None:
        self.catalogue_url = catalogue_url
        self.enabled = enabled

    def is_available(self) -> bool:
        return self.enabled and bool(self.catalogue_url)

    def fetch_rows(self, niche: str, limit: int) -> list[dict[str, str]]:
        """Return raw scraped rows. Override with a real implementation."""
        raise NotImplementedError(
            "PlaywrightSource needs a supplier-specific fetch_rows(). Drive the "
            "playwright-mcp server in this repo, and confirm the supplier's terms "
            "permit automated access before enabling it."
        )

    def search(self, niche: str, limit: int = 20) -> list[SourcedCandidate]:
        if not self.is_available():
            return []
        return [self._to_candidate(row) for row in self.fetch_rows(niche, limit)]

    def _to_candidate(self, row: dict[str, str]) -> SourcedCandidate:
        supplier = Supplier(
            id=row.get("supplier_id", "scraped"),
            name=row.get("supplier_name", "Scraped supplier"),
            country=row.get("country", "CN"),
            lead_time_days=int(row.get("lead_time_days", 5)),
            ships_blind=row.get("ships_blind", "false").lower() == "true",
            is_wholesale=row.get("is_wholesale", "true").lower() == "true",
        )
        product = Product(
            id=row.get("id", "scraped-item"),
            title=row.get("title", "Untitled"),
            category=row.get("category", "uncategorised"),
        )
        return SourcedCandidate(
            product=product,
            supplier=supplier,
            unit_cost=Money.from_dollars(row.get("unit_cost", "0")),
            ship_cost=Money.from_dollars(row.get("ship_cost", "0")),
            transit_days=int(row.get("transit_days", 10)),
            source_name=self.source_name,
        )


# -- scoring ---------------------------------------------------------------


@dataclass(frozen=True)
class ScoreWeights:
    """How much each component counts. Must sum to something positive."""

    margin: float = 0.35
    demand: float = 0.25
    competition: float = 0.15
    speed: float = 0.15
    supplier: float = 0.10

    def total(self) -> float:
        return self.margin + self.demand + self.competition + self.speed + self.supplier


@dataclass(frozen=True)
class CandidateScore:
    """A ranked candidate with its reasoning intact."""

    candidate: SourcedCandidate
    total: float
    components: dict[str, float]
    projected_price: Money
    projected_margin_rate: float

    def describe(self) -> str:
        parts = ", ".join(f"{k} {v:.2f}" for k, v in sorted(self.components.items()))
        return (
            f"{self.total:.2f} {self.candidate.product.title[:40]} "
            f"@ {self.projected_price} ({self.projected_margin_rate:.0%} margin) [{parts}]"
        )


@dataclass
class ProductResearchAgent:
    """Searches every source, de-duplicates, scores and ranks."""

    sources: list[ProductSource] = field(default_factory=list)
    weights: ScoreWeights = field(default_factory=ScoreWeights)
    target_markup: float = 3.0
    max_acceptable_days: int = 21

    def gather(self, niche: str, limit_per_source: int = 20) -> list[SourcedCandidate]:
        """Every candidate from every available source, de-duplicated.

        When two sources offer the same product from the same supplier, the
        cheaper landed cost wins -- that is the one we would actually buy.
        """
        best: dict[tuple[str, str], SourcedCandidate] = {}
        for source in self.sources:
            if not source.is_available():
                continue
            for candidate in source.search(niche, limit_per_source):
                key = candidate.key()
                incumbent = best.get(key)
                if incumbent is None or candidate.landed_cost < incumbent.landed_cost:
                    best[key] = candidate
        return list(best.values())

    def project_price(self, candidate: SourcedCandidate) -> Money:
        """A first-pass sell price. ``catalog.py`` refines this per channel."""
        raw = candidate.landed_cost * self.target_markup
        return _charm_price(raw)

    def score(self, candidate: SourcedCandidate) -> CandidateScore:
        """Score one candidate across all five components."""
        price = self.project_price(candidate)
        margin_rate = (price - candidate.landed_cost).ratio_to(price)

        components = {
            "margin": _clamp(margin_rate / 0.6),
            "demand": _clamp(candidate.demand_signal),
            "competition": _clamp(1.0 - candidate.competition),
            "speed": _clamp(1.0 - candidate.days_to_door / max(1, self.max_acceptable_days)),
            "supplier": _clamp(candidate.supplier.rating / 5.0),
        }
        weighted = (
            components["margin"] * self.weights.margin
            + components["demand"] * self.weights.demand
            + components["competition"] * self.weights.competition
            + components["speed"] * self.weights.speed
            + components["supplier"] * self.weights.supplier
        )
        divisor = self.weights.total() or 1.0
        return CandidateScore(
            candidate=candidate,
            total=round(weighted / divisor, 3),
            components={k: round(v, 3) for k, v in components.items()},
            projected_price=price,
            projected_margin_rate=round(margin_rate, 3),
        )

    def research(self, niche: str, top_n: int = 10) -> list[CandidateScore]:
        """The whole pipeline: gather, score, rank, truncate."""
        scored = [self.score(c) for c in self.gather(niche)]
        scored.sort(key=lambda s: (-s.total, s.candidate.product.title))
        return scored[:top_n]


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _charm_price(raw: Money) -> Money:
    """Round to a .99 price point at or above ``raw``.

    Retail convention, and it removes a whole class of pennies-off pricing bugs
    when the same product is priced on three channels with different fees.
    """
    whole = raw.cents // 100
    if raw.cents % 100 > 99:  # pragma: no cover - unreachable, kept for intent
        whole += 1
    return Money(whole * 100 + 99, raw.currency)


def _terms(niche: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", niche.lower()) if len(t) > 2]
