"""The approval queue and the twice-daily check-in digest.

The promise this package makes is that the owner looks at the business twice a
day and otherwise leaves it alone. That only works if two things are true.

**Most decisions never reach them.** ``policy.py`` decides; anything it returns
``ALLOW`` for is auto-approved here, recorded, and never shown as a question.
The queue holds only what policy escalated.

**Nothing gets stuck waiting.** Every item carries a deadline and a
:class:`TimeoutPolicy`. Skip a check-in and items do not silently rot: they
escalate to a delegate, expire back to a safe default, or -- for anything that
spends money -- simply stay pending and shout louder in the next digest. A
purchase order never auto-approves on a timeout, because "the owner was busy"
is not consent to spend.

:class:`CheckInDigest` is what the owner actually sees: one screen, ordered by
what costs the most to get wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from .audit import AuditLog
from .domain import Money
from .policy import PolicyDecision


class ApprovalKind(str, Enum):
    """What sort of thing is waiting. Drives ordering and the UI grouping."""

    PURCHASE_ORDER = "purchase_order"
    HELD_ORDER = "held_order"
    SHORTLIST = "shortlist"
    LISTING = "listing"
    AD_SPEND = "ad_spend"
    SOCIAL_POST = "social_post"

    @property
    def urgency_rank(self) -> int:
        """Lower sorts first. Money and stuck customers before marketing."""
        return {
            "purchase_order": 0,
            "held_order": 1,
            "shortlist": 2,
            "listing": 3,
            "ad_spend": 4,
            "social_post": 5,
        }[self.value]


class Decision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


class ItemState(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    AUTO_APPROVED = "auto_approved"
    ESCALATED = "escalated"
    EXPIRED = "expired"

    @property
    def is_open(self) -> bool:
        return self in (ItemState.PENDING, ItemState.ESCALATED)


class TimeoutPolicy(str, Enum):
    """What happens when a deadline passes with no answer."""

    HOLD = "hold"          # stay pending, keep surfacing. The default for spend.
    ESCALATE = "escalate"  # hand to a delegate.
    EXPIRE = "expire"      # withdraw the request; safe for marketing drafts.


@dataclass
class ApprovalItem:
    """One thing waiting on a human."""

    id: str
    kind: ApprovalKind
    summary: str
    detail: str = ""
    amount: Money | None = None
    decision_reason: str = ""
    rule: str = ""
    created_at: datetime = field(default_factory=datetime.now)
    timeout_hours: float = 12.0
    on_timeout: TimeoutPolicy = TimeoutPolicy.HOLD
    escalate_to: str = ""
    state: ItemState = ItemState.PENDING
    decided_by: str = ""
    decided_at: datetime | None = None
    note: str = ""
    subject_id: str = ""

    @property
    def deadline(self) -> datetime:
        return self.created_at + timedelta(hours=self.timeout_hours)

    def is_overdue(self, now: datetime) -> bool:
        return self.state.is_open and now > self.deadline

    def age_hours(self, now: datetime) -> float:
        return max(0.0, (now - self.created_at).total_seconds() / 3600)

    def describe(self) -> str:
        money = f" {self.amount}" if self.amount is not None else ""
        return f"[{self.kind.value}]{money} {self.summary}"


@dataclass
class CheckInDigest:
    """Everything the owner needs at one check-in, in one object."""

    generated_at: datetime
    items: tuple[ApprovalItem, ...] = ()
    auto_approved: int = 0
    auto_approved_value: Money = field(default_factory=Money.zero)
    overdue: tuple[ApprovalItem, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.items

    def by_kind(self, kind: ApprovalKind) -> list[ApprovalItem]:
        return [i for i in self.items if i.kind is kind]

    def pending_spend(self) -> Money:
        """What approving everything in the queue would cost."""
        total = Money.zero()
        for item in self.items:
            if item.amount is not None:
                total = total + item.amount
        return total

    def grouped(self) -> dict[ApprovalKind, list[ApprovalItem]]:
        out: dict[ApprovalKind, list[ApprovalItem]] = {}
        for item in self.items:
            out.setdefault(item.kind, []).append(item)
        return out

    def summary(self) -> str:
        if self.is_empty:
            return (
                f"Nothing needs you. {self.auto_approved} action(s) auto-approved "
                f"within policy, {self.auto_approved_value} committed."
            )
        overdue = f", {len(self.overdue)} overdue" if self.overdue else ""
        return (
            f"{len(self.items)} item(s) need you{overdue}, {self.pending_spend()} at stake. "
            f"{self.auto_approved} handled automatically."
        )

    def lines(self) -> list[str]:
        """Plain-text digest for the CLI check-in."""
        out = [self.summary()]
        for kind, items in sorted(self.grouped().items(), key=lambda kv: kv[0].urgency_rank):
            out.append(f"\n{kind.value.replace('_', ' ').upper()} ({len(items)})")
            for item in items:
                flag = " OVERDUE" if item in self.overdue else ""
                out.append(f"  {item.id}{flag}: {item.describe()}")
                if item.decision_reason:
                    out.append(f"      why you: {item.decision_reason}")
        out.extend(f"\n{note}" for note in self.notes)
        return out


@dataclass
class ApprovalQueue:
    """Holds what policy escalated; records what policy handled itself."""

    audit: AuditLog = field(default_factory=AuditLog)
    owner: str = "owner"
    delegates: tuple[str, ...] = ()
    items: list[ApprovalItem] = field(default_factory=list)
    auto_approved: int = 0
    auto_approved_value: Money = field(default_factory=Money.zero)
    last_check_in: datetime | None = None
    _counter: int = field(default=0, init=False)

    # -- intake ------------------------------------------------------------

    def _next_id(self, kind: ApprovalKind) -> str:
        self._counter += 1
        return f"{kind.value[:2].upper()}-{self._counter:04d}"

    def route(
        self,
        decision: PolicyDecision,
        kind: ApprovalKind,
        summary: str,
        detail: str = "",
        amount: Money | None = None,
        subject_id: str = "",
        at: datetime | None = None,
        timeout_hours: float = 12.0,
        on_timeout: TimeoutPolicy | None = None,
    ) -> ApprovalItem | None:
        """Apply a policy decision.

        Returns the queued item when the owner must decide, or ``None`` when
        policy handled it -- auto-approved or blocked outright. A blocked action
        is never queued: approving it would breach a marketplace rule or a hard
        cap, so it is not the owner's to override from a phone at 9am.
        """
        moment = at or datetime.now()
        if decision.is_blocked:
            self.audit.record(
                actor="policy",
                action="blocked",
                subject=subject_id or summary,
                reasoning=decision.reason,
                metadata={"rule": decision.rule, "kind": kind.value},
                at=moment,
            )
            return None
        if decision.is_allowed:
            self.auto_approved += 1
            if amount is not None:
                self.auto_approved_value = self.auto_approved_value + amount
            self.audit.record(
                actor="policy",
                action="auto_approved",
                subject=subject_id or summary,
                reasoning=decision.reason,
                metadata={
                    "rule": decision.rule,
                    "kind": kind.value,
                    "amount_cents": amount.cents if amount else 0,
                },
                at=moment,
            )
            return None

        item = ApprovalItem(
            id=self._next_id(kind),
            kind=kind,
            summary=summary,
            detail=detail,
            amount=amount,
            decision_reason=decision.reason,
            rule=decision.rule,
            created_at=moment,
            timeout_hours=timeout_hours,
            on_timeout=on_timeout or _default_timeout(kind),
            escalate_to=self.delegates[0] if self.delegates else "",
            subject_id=subject_id,
        )
        self.items.append(item)
        self.audit.record(
            actor="policy",
            action="approval_requested",
            subject=subject_id or item.id,
            reasoning=decision.reason,
            metadata={"rule": decision.rule, "kind": kind.value, "item": item.id},
            at=moment,
        )
        return item

    # -- owner decisions ---------------------------------------------------

    def decide(
        self,
        item_id: str,
        decision: Decision,
        actor: str = "",
        note: str = "",
        at: datetime | None = None,
    ) -> ApprovalItem:
        """Record the owner's answer."""
        item = self.item(item_id)
        if item is None:
            raise KeyError(f"no approval item {item_id}")
        if not item.state.is_open:
            raise ValueError(f"{item_id} is already {item.state.value}")
        who = actor or self.owner
        if who != self.owner and who not in self.delegates:
            raise PermissionError(f"{who} is not the owner or a delegate")
        item.state = (
            ItemState.APPROVED if decision is Decision.APPROVE else ItemState.REJECTED
        )
        item.decided_by = who
        item.decided_at = at or datetime.now()
        item.note = note
        self.audit.record(
            actor=who,
            action=f"approval_{item.state.value}",
            subject=item.subject_id or item.id,
            reasoning=note or f"{decision.value} at check-in",
            metadata={"item": item.id, "kind": item.kind.value},
            at=item.decided_at,
        )
        return item

    def item(self, item_id: str) -> ApprovalItem | None:
        return next((i for i in self.items if i.id == item_id), None)

    def pending(self) -> list[ApprovalItem]:
        """Open items, most consequential first."""
        return sorted(
            (i for i in self.items if i.state.is_open),
            key=lambda i: (i.kind.urgency_rank, -(i.amount.cents if i.amount else 0), i.id),
        )

    # -- deadlines ---------------------------------------------------------

    def tick(self, now: datetime) -> list[ApprovalItem]:
        """Apply timeout policy to overdue items. Returns what changed.

        Note what is *not* here: no branch auto-approves. Spending money always
        requires an affirmative answer, however late it arrives.
        """
        changed: list[ApprovalItem] = []
        for item in self.items:
            if not item.is_overdue(now):
                continue
            if item.on_timeout is TimeoutPolicy.ESCALATE and item.escalate_to:
                if item.state is not ItemState.ESCALATED:
                    item.state = ItemState.ESCALATED
                    self.audit.record(
                        actor="approval_queue",
                        action="approval_escalated",
                        subject=item.subject_id or item.id,
                        reasoning=f"no answer by {item.deadline:%d %b %H:%M}, escalated to {item.escalate_to}",
                        metadata={"item": item.id},
                        at=now,
                    )
                    changed.append(item)
            elif item.on_timeout is TimeoutPolicy.EXPIRE:
                item.state = ItemState.EXPIRED
                self.audit.record(
                    actor="approval_queue",
                    action="approval_expired",
                    subject=item.subject_id or item.id,
                    reasoning=f"withdrawn unanswered after {item.timeout_hours:g}h",
                    metadata={"item": item.id},
                    at=now,
                )
                changed.append(item)
        return changed

    # -- the digest --------------------------------------------------------

    def digest(self, at: datetime | None = None, notes: tuple[str, ...] = ()) -> CheckInDigest:
        """Build the check-in view and mark the check-in as taken."""
        moment = at or datetime.now()
        self.tick(moment)
        pending = self.pending()
        digest = CheckInDigest(
            generated_at=moment,
            items=tuple(pending),
            auto_approved=self.auto_approved,
            auto_approved_value=self.auto_approved_value,
            overdue=tuple(i for i in pending if i.is_overdue(moment)),
            notes=notes,
        )
        self.last_check_in = moment
        self.audit.record(
            actor=self.owner,
            action="check_in",
            subject="digest",
            reasoning=digest.summary(),
            metadata={"pending": len(pending), "auto_approved": self.auto_approved},
            at=moment,
        )
        return digest


def _default_timeout(kind: ApprovalKind) -> TimeoutPolicy:
    """Marketing drafts go stale; money waits for a human."""
    if kind in (ApprovalKind.SOCIAL_POST, ApprovalKind.AD_SPEND):
        return TimeoutPolicy.EXPIRE
    if kind is ApprovalKind.HELD_ORDER:
        return TimeoutPolicy.ESCALATE
    return TimeoutPolicy.HOLD
