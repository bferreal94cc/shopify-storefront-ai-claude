"""Order fraud and chargeback scoring.

A dropship operation is unusually exposed: we pay a supplier up front and ship
to an address the customer chose, so a fraudulent order costs us the goods *and*
the chargeback fee, with no inventory to recover. The cheapest moment to catch
it is before the purchase order goes out.

Scoring is deliberately transparent -- a weighted sum of named signals, each of
which can explain itself. When an order is held, the owner sees *which* signals
fired, not an opaque number, because they are the one who has to decide whether
to release it at check-in.

Signals here are heuristics, not certainties. They are tuned to hold the small
fraction of orders worth a human glance, not to be a fraud-detection product.
Chargeback liability ultimately sits with the payment processor's own rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .domain import Money, Order

# Countries where cross-border card fraud rates are high enough that a mismatch
# between the customer and the destination is worth a second look. Presence here
# is not an accusation -- it only adds weight to a signal.
_ELEVATED_RISK_DESTINATIONS = frozenset({"NG", "GH", "ID", "VN", "RO", "UA"})

# Address fragments that mark a reshipper or freight forwarder, a classic
# card-fraud destination because the real recipient stays hidden.
_FORWARDER_MARKERS = ("freight forward", "reship", "cargo agent", "suite 1000")


@dataclass(frozen=True)
class RiskSignal:
    """One named contribution to an order's score."""

    name: str
    weight: float
    detail: str

    def describe(self) -> str:
        return f"{self.name} (+{self.weight:.2f}): {self.detail}"


@dataclass
class RiskAssessment:
    """The verdict on one order."""

    order_id: str
    score: float
    signals: tuple[RiskSignal, ...] = ()
    threshold: float = 0.6

    @property
    def should_hold(self) -> bool:
        return self.score >= self.threshold

    @property
    def band(self) -> str:
        if self.score >= 0.8:
            return "high"
        if self.score >= self.threshold:
            return "elevated"
        if self.score >= 0.3:
            return "watch"
        return "low"

    def summary(self) -> str:
        if not self.signals:
            return f"score {self.score:.2f} ({self.band}), nothing flagged"
        top = ", ".join(s.name for s in self.signals[:3])
        return f"score {self.score:.2f} ({self.band}): {top}"

    def explain(self) -> list[str]:
        return [s.describe() for s in self.signals]


@dataclass
class RiskEngine:
    """Scores orders against a rolling window of recent history.

    The engine is stateful on purpose: velocity signals need memory of what this
    customer and this address did in the last few hours.
    """

    threshold: float = 0.6
    high_value: Money = Money(20_000)
    velocity_window: timedelta = timedelta(hours=6)
    velocity_limit: int = 3
    _history: list[tuple[str, str, datetime]] = field(default_factory=list)

    def observe(self, order: Order) -> None:
        """Remember an order for later velocity checks."""
        self._history.append(
            (order.customer.email.lower(), order.ship_to.postal_code, order.placed_at)
        )

    def _recent_matches(self, order: Order) -> int:
        cutoff = order.placed_at - self.velocity_window
        email = order.customer.email.lower()
        postal = order.ship_to.postal_code
        return sum(
            1
            for seen_email, seen_postal, seen_at in self._history
            if seen_at >= cutoff and (seen_email == email or seen_postal == postal)
        )

    def assess(self, order: Order) -> RiskAssessment:
        """Score ``order`` and explain every signal that fired."""
        signals: list[RiskSignal] = []

        if order.customer.prior_orders == 0 and order.revenue > self.high_value:
            signals.append(
                RiskSignal(
                    "first_order_high_value",
                    0.35,
                    f"first order from {order.customer.id} is {order.revenue}, "
                    f"above the {self.high_value} watch line",
                )
            )

        recent = self._recent_matches(order)
        if recent >= self.velocity_limit:
            signals.append(
                RiskSignal(
                    "order_velocity",
                    0.30,
                    f"{recent} orders from this email or postcode within "
                    f"{int(self.velocity_window.total_seconds() // 3600)}h",
                )
            )

        if order.ship_to.country in _ELEVATED_RISK_DESTINATIONS:
            signals.append(
                RiskSignal(
                    "elevated_risk_destination",
                    0.20,
                    f"ships to {order.ship_to.country}, an elevated cross-border fraud lane",
                )
            )

        haystack = f"{order.ship_to.line1} {order.ship_to.name}".lower()
        if any(marker in haystack for marker in _FORWARDER_MARKERS):
            signals.append(
                RiskSignal(
                    "freight_forwarder",
                    0.30,
                    "destination looks like a reshipper or freight forwarder",
                )
            )

        if order.customer.name and not _names_agree(order.customer.name, order.ship_to.name):
            signals.append(
                RiskSignal(
                    "name_mismatch",
                    0.15,
                    "shipping name does not match the account name",
                )
            )

        heaviest = max((line.quantity for line in order.lines), default=0)
        if heaviest >= 10:
            signals.append(
                RiskSignal(
                    "bulk_quantity",
                    0.15,
                    f"{heaviest} units of a single SKU on a consumer order",
                )
            )

        if order.margin.cents < 0:
            signals.append(
                RiskSignal(
                    "negative_margin",
                    0.25,
                    f"order loses {-order.margin} before fees -- likely a pricing error",
                )
            )

        score = min(1.0, sum(s.weight for s in signals))
        ranked = tuple(sorted(signals, key=lambda s: -s.weight))
        self.observe(order)
        return RiskAssessment(
            order_id=order.id, score=round(score, 3), signals=ranked, threshold=self.threshold
        )


def _names_agree(left: str, right: str) -> bool:
    """Loose surname comparison -- initials and middle names should not flag."""
    left_parts = {p for p in left.lower().replace(".", "").split() if len(p) > 1}
    right_parts = {p for p in right.lower().replace(".", "").split() if len(p) > 1}
    if not left_parts or not right_parts:
        return True
    return bool(left_parts & right_parts)
