"""The autonomy dial: what the system may do alone, and what waits for you.

This is the single most important module in the package. Everything else can
act; this decides whether it is allowed to act *unattended*. Turn the dial one
way and the owner approves every purchase order; turn it the other and only the
expensive, thin-margin or non-compliant ones surface at check-in.

Three verdicts, and only three:

``ALLOW``    proceed unattended, and write why to the audit log.
``APPROVE``  hold for the owner's next check-in, with the reasoning attached.
``BLOCK``    refuse outright. A blocked action never reaches the owner as a
             choice, because approving it would breach a marketplace policy or
             a hard spend ceiling.

**Marketplace compliance is encoded here, not left to the operator's memory.**
Amazon's drop shipping policy requires you to be the seller of record and
forbids shipping with another retailer's packing slip; eBay permits
wholesale-supplier dropshipping but restricts retail arbitrage. Those rules are
:class:`ChannelRules` below, applied *before* a listing can publish. They change
-- re-check the current policy text before going live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from enum import Enum

from .domain import Channel, Money, PurchaseOrder, SourcedCandidate


class Verdict(str, Enum):
    ALLOW = "allow"
    APPROVE = "approve"
    BLOCK = "block"


@dataclass(frozen=True)
class PolicyDecision:
    """What policy said, and which rule said it."""

    verdict: Verdict
    rule: str
    reason: str

    @property
    def is_allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW

    @property
    def needs_owner(self) -> bool:
        return self.verdict is Verdict.APPROVE

    @property
    def is_blocked(self) -> bool:
        return self.verdict is Verdict.BLOCK

    def describe(self) -> str:
        return f"{self.verdict.value.upper()} [{self.rule}] {self.reason}"


def allow(rule: str, reason: str) -> PolicyDecision:
    return PolicyDecision(Verdict.ALLOW, rule, reason)


def needs_approval(rule: str, reason: str) -> PolicyDecision:
    return PolicyDecision(Verdict.APPROVE, rule, reason)


def block(rule: str, reason: str) -> PolicyDecision:
    return PolicyDecision(Verdict.BLOCK, rule, reason)


class AutonomyLevel(str, Enum):
    """How much rope the system gets.

    The level does not change *what* the rules are, only the thresholds they
    fire at, so tightening the dial can never unblock a compliance rule.
    """

    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"

    @property
    def po_cap_multiplier(self) -> float:
        return {"conservative": 0.25, "balanced": 1.0, "aggressive": 3.0}[self.value]


# -- per-channel compliance ------------------------------------------------


@dataclass(frozen=True)
class ChannelRules:
    """Marketplace rules that decide whether we may list at all."""

    channel: Channel
    restricted_categories: frozenset[str] = frozenset()
    requires_blind_shipping: bool = False
    requires_wholesale_supplier: bool = False
    max_handling_days: int = 30
    min_supplier_rating: float = 0.0

    def evaluate(self, candidate: SourcedCandidate) -> PolicyDecision:
        """Whether this candidate may be listed on this channel."""
        category = candidate.product.category.strip().lower()
        if category in {c.lower() for c in self.restricted_categories}:
            return block(
                f"{self.channel.value}.restricted_category",
                f"{self.channel.label} restricts the '{candidate.product.category}' category.",
            )
        if self.requires_blind_shipping and not candidate.supplier.ships_blind:
            return block(
                f"{self.channel.value}.seller_of_record",
                f"{candidate.supplier.name} will not ship blind, so the packing slip would "
                f"carry their branding. {self.channel.label} requires you to be the seller "
                "of record.",
            )
        if self.requires_wholesale_supplier and not candidate.supplier.is_wholesale:
            return block(
                f"{self.channel.value}.retail_arbitrage",
                f"{candidate.supplier.name} is a retail source, not a wholesaler. "
                f"{self.channel.label} does not permit retail-arbitrage dropshipping.",
            )
        if candidate.days_to_door > self.max_handling_days:
            return block(
                f"{self.channel.value}.handling_time",
                f"{candidate.days_to_door} days to the door exceeds the "
                f"{self.max_handling_days}-day limit on {self.channel.label}.",
            )
        if candidate.supplier.rating < self.min_supplier_rating:
            return needs_approval(
                f"{self.channel.value}.supplier_rating",
                f"{candidate.supplier.name} is rated {candidate.supplier.rating:.1f}, "
                f"below the {self.min_supplier_rating:.1f} bar for {self.channel.label}.",
            )
        return allow(f"{self.channel.value}.eligible", "Meets channel requirements.")


def default_channel_rules() -> dict[Channel, ChannelRules]:
    """Marketplace rules as published at time of writing. Re-verify before launch."""
    return {
        Channel.SHOPIFY: ChannelRules(
            channel=Channel.SHOPIFY,
            restricted_categories=frozenset({"weapons", "tobacco", "prescription"}),
            max_handling_days=30,
        ),
        Channel.AMAZON: ChannelRules(
            channel=Channel.AMAZON,
            restricted_categories=frozenset(
                {"weapons", "tobacco", "prescription", "hazmat", "supplements"}
            ),
            requires_blind_shipping=True,
            requires_wholesale_supplier=True,
            max_handling_days=14,
            min_supplier_rating=4.0,
        ),
        Channel.EBAY: ChannelRules(
            channel=Channel.EBAY,
            restricted_categories=frozenset({"weapons", "tobacco", "prescription", "hazmat"}),
            requires_wholesale_supplier=True,
            max_handling_days=21,
            min_supplier_rating=3.5,
        ),
    }


# -- the dial --------------------------------------------------------------


@dataclass
class AutomationPolicy:
    """Every threshold that separates 'act now' from 'ask the owner'."""

    autonomy: AutonomyLevel = AutonomyLevel.BALANCED
    margin_floor: float = 0.25
    po_auto_approve_cap: Money = Money(15_000)
    po_hard_cap: Money = Money(200_000)
    daily_spend_cap: Money = Money(100_000)
    ad_auto_approve_cap: Money = Money(5_000)
    max_new_listings_per_run: int = 25
    risk_hold_threshold: float = 0.6
    social_requires_approval: bool = True
    supplier_blocklist: frozenset[str] = frozenset()
    check_in_windows: tuple[time, ...] = (time(9, 0), time(17, 0))
    channel_rules: dict[Channel, ChannelRules] = field(default_factory=default_channel_rules)
    _spent_today: Money = field(default=Money(0), init=False)
    _spend_date: str = field(default="", init=False)

    # -- effective thresholds ---------------------------------------------

    @property
    def effective_po_cap(self) -> Money:
        """The auto-approve ceiling after the autonomy dial is applied."""
        return self.po_auto_approve_cap * self.autonomy.po_cap_multiplier

    def rules_for(self, channel: Channel) -> ChannelRules:
        return self.channel_rules.get(channel, ChannelRules(channel=channel))

    # -- spend tracking ----------------------------------------------------

    def record_spend(self, amount: Money, at: datetime | None = None) -> None:
        """Add to today's running total, rolling over at midnight."""
        stamp = (at or datetime.now()).date().isoformat()
        if stamp != self._spend_date:
            self._spend_date = stamp
            self._spent_today = Money.zero(amount.currency)
        self._spent_today = self._spent_today + amount

    def spent_today(self, at: datetime | None = None) -> Money:
        stamp = (at or datetime.now()).date().isoformat()
        return self._spent_today if stamp == self._spend_date else Money.zero()

    def remaining_today(self, at: datetime | None = None) -> Money:
        return self.daily_spend_cap - self.spent_today(at)

    # -- decisions ---------------------------------------------------------

    def evaluate_candidate(
        self, candidate: SourcedCandidate, channel: Channel, sell_price: Money
    ) -> PolicyDecision:
        """Whether a sourced product may be listed on a channel."""
        if candidate.supplier.id in self.supplier_blocklist:
            return block(
                "supplier_blocklist",
                f"{candidate.supplier.name} is on the blocklist.",
            )
        compliance = self.rules_for(channel).evaluate(candidate)
        if not compliance.is_allowed:
            return compliance
        margin = (sell_price - candidate.landed_cost).ratio_to(sell_price)
        if margin < 0:
            return block(
                "negative_margin",
                f"{sell_price} sell price is below {candidate.landed_cost} landed cost.",
            )
        if margin < self.margin_floor:
            return needs_approval(
                "margin_floor",
                f"{margin:.0%} margin is under the {self.margin_floor:.0%} floor.",
            )
        return allow("candidate_eligible", f"{margin:.0%} margin, compliant on {channel.label}.")

    def evaluate_purchase_order(
        self, po: PurchaseOrder, at: datetime | None = None
    ) -> PolicyDecision:
        """Whether a purchase order may be placed without the owner.

        The hard cap and the daily cap block rather than escalate. A single
        runaway order and a runaway day are the two failure modes that turn a
        bug into a bank balance, so neither is something the owner can click
        past in a hurry at 9am.
        """
        total = po.total
        if po.supplier_id in self.supplier_blocklist:
            return block("supplier_blocklist", f"{po.supplier_id} is on the blocklist.")
        if total.cents <= 0:
            return block("empty_po", "Purchase order has no positive value.")
        if total > self.po_hard_cap:
            return block(
                "po_hard_cap",
                f"{total} exceeds the {self.po_hard_cap} hard ceiling for a single order.",
            )
        remaining = self.remaining_today(at)
        if total > remaining:
            return block(
                "daily_spend_cap",
                f"{total} would breach today's remaining {remaining} of "
                f"{self.daily_spend_cap} budget.",
            )
        cap = self.effective_po_cap
        if total > cap:
            return needs_approval(
                "po_auto_approve_cap",
                f"{total} is over the {cap} auto-approve limit "
                f"({self.autonomy.value} autonomy).",
            )
        return allow("po_within_limits", f"{total} is within the {cap} auto-approve limit.")

    def evaluate_ad_spend(self, amount: Money, platform: str) -> PolicyDecision:
        if amount.cents <= 0:
            return block("empty_budget", "Ad budget must be positive.")
        if amount > self.ad_auto_approve_cap:
            return needs_approval(
                "ad_auto_approve_cap",
                f"{amount} on {platform} is over the {self.ad_auto_approve_cap} limit.",
            )
        return allow("ad_within_limits", f"{amount} on {platform} is within limits.")

    def evaluate_social_post(self, platform: str) -> PolicyDecision:
        """Social posts default to owner approval.

        Bulk automated posting breaches most platforms' terms and gets accounts
        banned, so the default is a queue the owner clears, not a firehose.
        """
        if self.social_requires_approval:
            return needs_approval(
                "social_requires_approval",
                f"Posts to {platform} are queued for your approval.",
            )
        return allow("social_auto", f"Auto-posting to {platform} is enabled.")

    def should_hold_order(self, risk_score: float) -> bool:
        return risk_score >= self.risk_hold_threshold

    # -- check-in scheduling ----------------------------------------------

    def next_check_in(self, after: datetime) -> datetime:
        """When the owner is next expected to look at the queue."""
        for window in sorted(self.check_in_windows):
            candidate = after.replace(
                hour=window.hour, minute=window.minute, second=0, microsecond=0
            )
            if candidate > after:
                return candidate
        first = sorted(self.check_in_windows)[0]
        today_at_first = after.replace(
            hour=first.hour, minute=first.minute, second=0, microsecond=0
        )
        return today_at_first + timedelta(days=1)

    @classmethod
    def conservative(cls) -> "AutomationPolicy":
        return cls(autonomy=AutonomyLevel.CONSERVATIVE, margin_floor=0.35)

    @classmethod
    def aggressive(cls) -> "AutomationPolicy":
        return cls(
            autonomy=AutonomyLevel.AGGRESSIVE,
            margin_floor=0.15,
            social_requires_approval=False,
        )

    def summary(self) -> str:
        return (
            f"{self.autonomy.value} autonomy | auto-approve POs to {self.effective_po_cap} | "
            f"margin floor {self.margin_floor:.0%} | daily cap {self.daily_spend_cap} | "
            f"check-ins {', '.join(w.strftime('%H:%M') for w in sorted(self.check_in_windows))}"
        )
