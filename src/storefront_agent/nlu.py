"""Turning the owner's sentence into a run configuration.

The whole system starts from one prompt, so this module has to be forgiving
about phrasing and strict about consequences. Two rules follow from that.

**Regex first, model second.** Deterministic patterns extract channels, budgets,
margins and autonomy. The LLM is only consulted for what the patterns missed,
and only for the niche -- the free-text part. A model is not in the path of
"how much money may this spend", because a hallucinated budget is a withdrawal.

**Nothing dangerous defaults to permissive.** An unparsed budget is not
"unlimited", it is the conservative default. An unrecognised autonomy word is
not "aggressive". :class:`RunConfig` starts safe and the prompt can only move it
within bounds :meth:`RunConfig.validate` accepts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from .domain import Channel, Money
from .policy import AutomationPolicy, AutonomyLevel
from .providers import LLMProvider, StubProvider


class Intent(str, Enum):
    START_RUN = "start_run"
    CHECK_IN = "check_in"
    RESEARCH = "research"
    STATUS = "status"
    APPROVE_ALL = "approve_all"
    PAUSE = "pause"
    UNKNOWN = "unknown"


_CHANNEL_WORDS: dict[str, Channel] = {
    "shopify": Channel.SHOPIFY,
    "amazon": Channel.AMAZON,
    "ebay": Channel.EBAY,
    "e-bay": Channel.EBAY,
}

_INTENT_PATTERNS: tuple[tuple[Intent, re.Pattern[str]], ...] = (
    (Intent.CHECK_IN, re.compile(r"\b(check[- ]?in|what needs me|my queue|approvals?)\b", re.I)),
    (Intent.APPROVE_ALL, re.compile(r"\bapprove (?:them )?all\b|\bapprove everything\b", re.I)),
    (Intent.PAUSE, re.compile(r"\b(pause|stop|halt|freeze)\b", re.I)),
    (Intent.STATUS, re.compile(r"\b(status|how are we doing|dashboard|report)\b", re.I)),
    (Intent.RESEARCH, re.compile(r"\b(research|find|source)\b.*\bproducts?\b", re.I)),
    (Intent.START_RUN, re.compile(r"\b(start|launch|kick ?off|begin|run)\b", re.I)),
)

# "$500", "500 dollars", "$1.5k", "1,200"
_MONEY = re.compile(
    r"(?:\$\s?|\bbudget\s+(?:of\s+)?|\bcap\s+(?:of\s+)?)([\d,]+(?:\.\d+)?)\s*([km])?\b"
    r"|([\d,]+(?:\.\d+)?)\s*([km])?\s*dollars\b",
    re.I,
)
_PERCENT = re.compile(r"([\d.]+)\s*%")
_COUNT = re.compile(r"\b(\d{1,3})\s+(?:products?|items?|skus?|listings?)\b", re.I)
_NICHE = re.compile(
    r"\b(?:sell(?:ing)?|sourc(?:e|ing)|research(?:ing)?|find(?:ing)?|stock(?:ing)?|"
    r"niche(?:\s+is)?)\s+"
    r"(?:(?:me|some|new|more)\s+)?"
    # "research products for X" and "find items in X" both mean the niche is X.
    r"(?:products?|items?|skus?)?\s*(?:in|for|around)?\s*"
    r"(.+?)"
    r"(?=\s+(?:on|across|to|for|with|at|under|budget|and\s+(?:list|post))\b|[,.;]|$)",
    re.I,
)


@dataclass
class RunConfig:
    """What a run is allowed to do. Starts safe."""

    niche: str = ""
    channels: tuple[Channel, ...] = ()
    budget: Money | None = None
    margin_floor: float = 0.25
    autonomy: AutonomyLevel = AutonomyLevel.CONSERVATIVE
    max_listings: int = 10

    def validate(self) -> list[str]:
        """Complaints about this configuration, empty when it is usable."""
        problems: list[str] = []
        if not self.niche.strip():
            problems.append("no product niche given -- say what to sell")
        if not self.channels:
            problems.append("no storefront named -- say shopify, amazon or ebay")
        if not 0.0 <= self.margin_floor < 1.0:
            problems.append(f"margin floor {self.margin_floor} is not between 0 and 1")
        if self.max_listings < 1:
            problems.append(f"max listings must be positive, got {self.max_listings}")
        if self.budget is not None and self.budget.cents <= 0:
            problems.append("budget must be positive")
        return problems

    def to_policy(self, base: AutomationPolicy | None = None) -> AutomationPolicy:
        """Fold this configuration into an automation policy."""
        policy = base or AutomationPolicy()
        policy.autonomy = self.autonomy
        policy.margin_floor = self.margin_floor
        policy.max_new_listings_per_run = self.max_listings
        if self.budget is not None:
            policy.daily_spend_cap = self.budget
            # A single PO should never be able to consume the whole day's budget.
            policy.po_hard_cap = self.budget
        return policy

    def describe(self) -> str:
        channels = ", ".join(c.label for c in self.channels) or "no channels"
        budget = f", budget {self.budget}" if self.budget else ""
        return (
            f"'{self.niche}' on {channels}{budget}, {self.margin_floor:.0%} margin floor, "
            f"{self.autonomy.value} autonomy, up to {self.max_listings} listings"
        )


@dataclass
class ParsedPrompt:
    """What the owner asked for."""

    intent: Intent
    config: RunConfig = field(default_factory=RunConfig)
    confidence: float = 0.0
    source: str = "pattern"
    raw_text: str = ""

    @property
    def is_actionable(self) -> bool:
        return self.intent is not Intent.UNKNOWN and self.confidence >= 0.4


def parse_money(text: str) -> Money | None:
    """First money amount in ``text``, honouring k/m suffixes."""
    match = _MONEY.search(text)
    if not match:
        return None
    raw = match.group(1) or match.group(3)
    suffix = (match.group(2) or match.group(4) or "").lower()
    if raw is None:
        return None
    amount = float(raw.replace(",", ""))
    amount *= {"k": 1_000, "m": 1_000_000}.get(suffix, 1)
    return Money.from_dollars(amount)


def parse_channels(text: str) -> tuple[Channel, ...]:
    """Channels named in ``text``, in a stable order, de-duplicated."""
    found: list[Channel] = []
    lowered = text.lower()
    for word, channel in _CHANNEL_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", lowered) and channel not in found:
            found.append(channel)
    if re.search(r"\b(all|every)\b.{0,20}\b(store|storefront|channel|marketplace)s?\b", lowered):
        return (Channel.SHOPIFY, Channel.AMAZON, Channel.EBAY)
    return tuple(found)


def parse_autonomy(text: str) -> AutonomyLevel | None:
    lowered = text.lower()
    if re.search(r"\b(aggressive|hands[- ]off|full auto|autonomous)\b", lowered):
        return AutonomyLevel.AGGRESSIVE
    if re.search(r"\b(conservative|cautious|careful|ask me)\b", lowered):
        return AutonomyLevel.CONSERVATIVE
    if re.search(r"\bbalanced\b", lowered):
        return AutonomyLevel.BALANCED
    return None


def parse_niche(text: str) -> str:
    match = _NICHE.search(text)
    if not match:
        return ""
    niche = match.group(1).strip(" .,;")
    # Strip a trailing channel name the greedy capture may have swallowed.
    for word in _CHANNEL_WORDS:
        niche = re.sub(rf"\s*\b(?:on|to|across)?\s*{re.escape(word)}\b\s*$", "", niche, flags=re.I)
    return niche.strip(" .,;")


class PromptParser:
    """Deterministic extraction. No model involved."""

    def parse(self, text: str) -> ParsedPrompt:
        intent = Intent.UNKNOWN
        for candidate, pattern in _INTENT_PATTERNS:
            if pattern.search(text):
                intent = candidate
                break

        config = RunConfig()
        signals = 0

        channels = parse_channels(text)
        if channels:
            config.channels = channels
            signals += 1

        budget = parse_money(text)
        if budget is not None:
            config.budget = budget
            signals += 1

        percent = _PERCENT.search(text)
        if percent:
            config.margin_floor = float(percent.group(1)) / 100
            signals += 1

        autonomy = parse_autonomy(text)
        if autonomy is not None:
            config.autonomy = autonomy
            signals += 1

        count = _COUNT.search(text)
        if count:
            config.max_listings = int(count.group(1))
            signals += 1

        niche = parse_niche(text)
        if niche:
            config.niche = niche
            signals += 1

        confidence = 0.0 if intent is Intent.UNKNOWN else min(1.0, 0.45 + 0.1 * signals)
        return ParsedPrompt(
            intent=intent, config=config, confidence=round(confidence, 2), raw_text=text
        )


class NLUPipeline:
    """Patterns first; the model only fills a missing niche."""

    def __init__(
        self, parser: PromptParser | None = None, provider: LLMProvider | None = None
    ) -> None:
        self.parser = parser or PromptParser()
        self.provider = provider or StubProvider()

    def understand(self, text: str) -> ParsedPrompt:
        parsed = self.parser.parse(text)
        if parsed.intent is not Intent.START_RUN or parsed.config.niche:
            return parsed
        niche = self._ask_for_niche(text)
        if niche:
            parsed.config.niche = niche
            parsed.source = f"pattern+{self.provider.name}"
            parsed.confidence = min(1.0, parsed.confidence + 0.1)
        return parsed

    def _ask_for_niche(self, text: str) -> str:
        """Ask the model for the product category only.

        Scope is deliberately narrow: the model never sees a question whose
        answer could widen what the run may spend or where it may publish.
        """
        response = self.provider.complete(
            f"Message: {text!r}\n\n"
            "What product category does this describe? Answer with a short noun "
            "phrase and nothing else. If there is no product category, answer NONE.",
            system="You extract a single product category. Answer in under six words.",
        )
        answer = response.text.strip().strip('".')
        if not answer or answer.upper().startswith("NONE") or len(answer) > 60:
            return ""
        # Reject anything that is not plainly a noun phrase: a stub's "{}", a
        # JSON fragment, or a model declining in punctuation.
        if not re.search(r"[a-z]", answer, re.I) or set("{}[]<>|").intersection(answer):
            return ""
        return answer
