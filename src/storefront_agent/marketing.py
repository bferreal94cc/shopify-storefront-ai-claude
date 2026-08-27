"""Ad creative and campaign budgeting.

Generates ad variants per platform and wraps them in a campaign with a paced
budget. Two things here are load-bearing.

**Claims discipline.** Ad copy is where an automated system gets its owner into
real trouble -- an invented certification or a health claim is a regulator's
problem, not a marketing one. The system prompt forbids inventing claims, and
:func:`screen_claims` re-checks the generated text against a prohibited-claim
list before anything is queued. Generation is not trusted to police itself.

**Pacing, not just capping.** A daily cap alone still lets a campaign burn a
month's budget in three days at the wrong CPM. :class:`BudgetPacer` compares
spend against elapsed time and reports whether the campaign is ahead or behind,
so the check-in digest can say "this is 40% ahead of pace" rather than just
"this spent money".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

from .domain import Listing, Money
from .policy import AutomationPolicy, PolicyDecision
from .providers import LLMProvider, StubProvider


class AdPlatform(str, Enum):
    META = "meta"
    GOOGLE = "google"
    TIKTOK = "tiktok"

    @property
    def label(self) -> str:
        return {"meta": "Meta", "google": "Google Ads", "tiktok": "TikTok"}[self.value]

    @property
    def headline_limit(self) -> int:
        return {"meta": 40, "google": 30, "tiktok": 40}[self.value]

    @property
    def body_limit(self) -> int:
        return {"meta": 125, "google": 90, "tiktok": 100}[self.value]


class CampaignState(str, Enum):
    DRAFT = "draft"
    AWAITING_APPROVAL = "awaiting_approval"
    ACTIVE = "active"
    PAUSED = "paused"
    REJECTED = "rejected"


# Phrases that turn an ad into a compliance problem. Not exhaustive -- a
# deliberate floor, not a substitute for review of a regulated category.
_PROHIBITED_CLAIMS: tuple[tuple[str, str], ...] = (
    (r"\bFDA[- ]approved\b", "implies regulatory approval"),
    (r"\bclinically proven\b", "medical efficacy claim"),
    (r"\bcures?\b|\btreats?\b|\bheals?\b", "medical treatment claim"),
    (r"\b100%\s+(guaranteed|effective|safe)\b", "absolute guarantee"),
    (r"\bbest (?:in the world|on earth)\b", "unsubstantiated superlative"),
    (r"\block(?:s)? in.{0,20}\bprofit\b|\bguaranteed (?:income|returns?)\b", "financial guarantee"),
)

_AD_SYSTEM = (
    "You write short direct-response ad copy. Return plain text lines only. "
    "Never invent certifications, medical or health claims, guarantees, "
    "endorsements, awards, or statistics. Describe only what you were told."
)


@dataclass(frozen=True)
class ClaimIssue:
    phrase: str
    reason: str

    def describe(self) -> str:
        return f"{self.phrase!r}: {self.reason}"


def screen_claims(text: str) -> list[ClaimIssue]:
    """Prohibited claims found in ``text``."""
    issues: list[ClaimIssue] = []
    for pattern, reason in _PROHIBITED_CLAIMS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            issues.append(ClaimIssue(match.group(0), reason))
    return issues


@dataclass(frozen=True)
class AdCreative:
    """One ad variant."""

    variant: str
    headline: str
    body: str
    call_to_action: str = "Shop now"
    source: str = "fallback"
    issues: tuple[ClaimIssue, ...] = ()

    @property
    def is_clean(self) -> bool:
        return not self.issues

    def describe(self) -> str:
        flag = "" if self.is_clean else f" [{len(self.issues)} claim issue(s)]"
        return f"{self.variant}: {self.headline} / {self.body[:60]}{flag}"


@dataclass
class Campaign:
    """A paced spend against one listing on one platform."""

    id: str
    sku: str
    platform: AdPlatform
    daily_budget: Money
    duration_days: int
    creatives: tuple[AdCreative, ...] = ()
    state: CampaignState = CampaignState.DRAFT
    spent: Money = field(default_factory=Money.zero)
    started_on: date | None = None

    @property
    def total_budget(self) -> Money:
        return self.daily_budget * self.duration_days

    @property
    def clean_creatives(self) -> tuple[AdCreative, ...]:
        return tuple(c for c in self.creatives if c.is_clean)

    def describe(self) -> str:
        return (
            f"{self.id} {self.platform.label} {self.sku}: {self.daily_budget}/day "
            f"x{self.duration_days}d = {self.total_budget} [{self.state.value}]"
        )


@dataclass
class BudgetPacer:
    """Is this campaign spending faster than its own calendar?"""

    tolerance: float = 0.15

    def expected_spend(self, campaign: Campaign, today: date) -> Money:
        if campaign.started_on is None:
            return Money.zero()
        elapsed = min(campaign.duration_days, max(0, (today - campaign.started_on).days))
        return campaign.daily_budget * elapsed

    def pace_ratio(self, campaign: Campaign, today: date) -> float:
        """``actual / expected``. Above 1.0 means overspending."""
        expected = self.expected_spend(campaign, today)
        if expected.is_zero():
            return 0.0 if campaign.spent.is_zero() else float("inf")
        return campaign.spent.ratio_to(expected)

    def verdict(self, campaign: Campaign, today: date) -> str:
        ratio = self.pace_ratio(campaign, today)
        if ratio == float("inf"):
            return "spending before the campaign start date"
        if ratio == 0.0:
            return "no spend yet"
        if ratio > 1 + self.tolerance:
            return f"{ratio - 1:.0%} ahead of pace"
        if ratio < 1 - self.tolerance:
            return f"{1 - ratio:.0%} behind pace"
        return "on pace"


@dataclass
class MarketingAgent:
    """Writes ad variants and assembles campaigns."""

    provider: LLMProvider = field(default_factory=StubProvider)
    policy: AutomationPolicy = field(default_factory=AutomationPolicy)
    pacer: BudgetPacer = field(default_factory=BudgetPacer)
    _counter: int = field(default=0, init=False)

    def write_creative(
        self, listing: Listing, platform: AdPlatform, variant: str = "A"
    ) -> AdCreative:
        """One ad variant for one listing, screened for prohibited claims."""
        prompt = (
            f"Product: {listing.title}\n"
            f"Price: {listing.price}\n"
            f"Key points: {'; '.join(listing.bullets) or 'none supplied'}\n"
            f"Platform: {platform.label}\n\n"
            f"Line 1: a headline of at most {platform.headline_limit} characters.\n"
            f"Line 2: body copy of at most {platform.body_limit} characters.\n"
            "Line 3: a two-or-three word call to action."
        )
        response = self.provider.complete(prompt, system=_AD_SYSTEM, temperature=0.7)
        lines = response.as_lines()
        if len(lines) >= 3:
            headline, body, cta = lines[0], lines[1], lines[2]
            source = self.provider.name
        else:
            headline, body, cta = self._fallback(listing, platform, variant)
            source = "fallback"
        headline = headline[: platform.headline_limit]
        body = body[: platform.body_limit]
        issues = screen_claims(f"{headline} {body}")
        return AdCreative(
            variant=variant,
            headline=headline,
            body=body,
            call_to_action=cta[:20],
            source=source,
            issues=tuple(issues),
        )

    def _fallback(
        self, listing: Listing, platform: AdPlatform, variant: str
    ) -> tuple[str, str, str]:
        angle = listing.bullets[0] if listing.bullets else listing.title
        if variant == "B":
            headline = f"{listing.title} — {listing.price}"
            body = f"{angle}. Free returns."
        else:
            headline = listing.title
            body = f"{angle}. From {listing.price}."
        return headline, body, "Shop now"

    def build_campaign(
        self,
        listing: Listing,
        platform: AdPlatform,
        daily_budget: Money,
        duration_days: int = 7,
        variants: tuple[str, ...] = ("A", "B"),
    ) -> tuple[Campaign, PolicyDecision]:
        """Draft a campaign and ask policy whether it may run unattended."""
        if duration_days < 1:
            raise ValueError(f"duration must be at least 1 day, got {duration_days}")
        self._counter += 1
        creatives = tuple(self.write_creative(listing, platform, v) for v in variants)
        campaign = Campaign(
            id=f"CMP-{self._counter:04d}",
            sku=listing.sku,
            platform=platform,
            daily_budget=daily_budget,
            duration_days=duration_days,
            creatives=creatives,
        )
        total = campaign.total_budget
        decision = self.policy.evaluate_ad_spend(total, platform.label)
        if not campaign.clean_creatives:
            from .policy import block

            decision = block(
                "ad_claims",
                "every variant tripped the prohibited-claim screen: "
                + "; ".join(i.describe() for c in creatives for i in c.issues),
            )
        campaign.state = (
            CampaignState.ACTIVE
            if decision.is_allowed
            else CampaignState.REJECTED
            if decision.is_blocked
            else CampaignState.AWAITING_APPROVAL
        )
        if decision.is_allowed:
            campaign.started_on = date.today()
        return campaign, decision
