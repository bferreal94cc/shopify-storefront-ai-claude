"""The sequence: one start prompt, seven stages, resumable across days.

This is the module the whole package exists for. A single sentence from the
owner produces a :class:`RunState` that walks these stages::

    INTERPRET -> RESEARCH -> SELECT -> LIST -> PROMOTE -> OPERATE -> REPORT

Three properties make it usable by someone who checks in twice a day.

**It pauses instead of guessing.** A stage that needs a human returns
``gate=True`` and the run stops there. Nothing downstream runs on an assumption.
The owner answers at their next check-in and the run picks up mid-sequence.

**It survives everything.** :class:`RunState` serialises to JSON, so a run
outlives the process, the terminal, and the laptop lid. Resuming is reading a
file, not replaying the stages.

**OPERATE is a loop, not a step.** Stages 1-5 happen once per product cycle;
``OPERATE`` re-enters on a schedule, pulling orders and pushing parcels, and is
where the system spends most of its life. Reaching ``REPORT`` does not finish a
run -- it produces the digest and returns to ``OPERATE``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .domain import Channel, Money
from .nlu import Intent, RunConfig
from .policy import AutonomyLevel

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance
    from .orchestrator import Orchestrator


class Stage(str, Enum):
    INTERPRET = "interpret"
    RESEARCH = "research"
    SELECT = "select"
    LIST = "list"
    PROMOTE = "promote"
    OPERATE = "operate"
    REPORT = "report"

    @property
    def ordinal(self) -> int:
        return _ORDER.index(self)

    def next(self) -> "Stage":
        """The stage after this one. ``REPORT`` loops back to ``OPERATE``."""
        if self is Stage.REPORT:
            return Stage.OPERATE
        return _ORDER[min(self.ordinal + 1, len(_ORDER) - 1)]


_ORDER: tuple[Stage, ...] = (
    Stage.INTERPRET, Stage.RESEARCH, Stage.SELECT, Stage.LIST,
    Stage.PROMOTE, Stage.OPERATE, Stage.REPORT,
)


class RunStatus(str, Enum):
    ACTIVE = "active"
    AWAITING_APPROVAL = "awaiting_approval"
    PAUSED = "paused"
    FAILED = "failed"


@dataclass
class StageResult:
    """What one stage did."""

    stage: Stage
    ok: bool
    summary: str
    gate: bool = False
    data: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=datetime.now)

    def __str__(self) -> str:
        marker = "gate" if self.gate else ("ok" if self.ok else "!!")
        return f"[{marker}] {self.stage.value}: {self.summary}"

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form. Only JSON-safe values from ``data`` survive."""
        return {
            "stage": self.stage.value,
            "ok": self.ok,
            "summary": self.summary,
            "gate": self.gate,
            "at": self.at.isoformat(),
            "data": {k: v for k, v in self.data.items() if _json_safe(v)},
        }


@dataclass
class RunState:
    """A run, in a form that survives the process."""

    run_id: str
    prompt: str
    config: RunConfig
    stage: Stage = Stage.INTERPRET
    status: RunStatus = RunStatus.ACTIVE
    history: list[StageResult] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    blocked_on: str = ""

    @property
    def is_running(self) -> bool:
        return self.status is RunStatus.ACTIVE

    def record(self, result: StageResult) -> StageResult:
        self.history.append(result)
        self.updated_at = result.at
        return result

    def last(self, stage: Stage) -> StageResult | None:
        return next((r for r in reversed(self.history) if r.stage is stage), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "prompt": self.prompt,
            "stage": self.stage.value,
            "status": self.status.value,
            "blocked_on": self.blocked_on,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "config": {
                "niche": self.config.niche,
                "channels": [c.value for c in self.config.channels],
                "budget_cents": self.config.budget.cents if self.config.budget else None,
                "currency": self.config.budget.currency if self.config.budget else "USD",
                "margin_floor": self.config.margin_floor,
                "autonomy": self.config.autonomy.value,
                "max_listings": self.config.max_listings,
            },
            "history": [r.to_dict() for r in self.history],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RunState":
        raw = payload.get("config", {})
        budget_cents = raw.get("budget_cents")
        config = RunConfig(
            niche=raw.get("niche", ""),
            channels=tuple(Channel(c) for c in raw.get("channels", [])),
            budget=Money(budget_cents, raw.get("currency", "USD"))
            if budget_cents is not None
            else None,
            margin_floor=raw.get("margin_floor", 0.25),
            autonomy=AutonomyLevel(raw.get("autonomy", "conservative")),
            max_listings=raw.get("max_listings", 10),
        )
        state = cls(
            run_id=payload["run_id"],
            prompt=payload.get("prompt", ""),
            config=config,
            stage=Stage(payload.get("stage", "interpret")),
            status=RunStatus(payload.get("status", "active")),
            created_at=datetime.fromisoformat(payload["created_at"]),
            updated_at=datetime.fromisoformat(payload["updated_at"]),
            blocked_on=payload.get("blocked_on", ""),
        )
        for entry in payload.get("history", []):
            state.history.append(
                StageResult(
                    stage=Stage(entry["stage"]),
                    ok=entry["ok"],
                    summary=entry["summary"],
                    gate=entry.get("gate", False),
                    data=entry.get("data", {}),
                    at=datetime.fromisoformat(entry["at"]),
                )
            )
        return state

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.run_id}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2, default=str))
        return path

    @classmethod
    def load(cls, path: Path) -> "RunState":
        return cls.from_dict(json.loads(path.read_text()))

    def describe(self) -> str:
        return (
            f"{self.run_id} [{self.status.value}] at {self.stage.value}: "
            f"{self.config.describe()}"
        )


class StorefrontSequence:
    """Drives a :class:`RunState` through the stages.

    Holds no business logic itself -- each stage delegates to the orchestrator's
    engines. That keeps this class readable as what it is: the order of
    operations and where the humans interrupt.
    """

    def __init__(self, agent: "Orchestrator", run_dir: Path | None = None) -> None:
        self.agent = agent
        self.run_dir = run_dir or Path(".runs")

    # -- lifecycle ---------------------------------------------------------

    def start(self, prompt: str, at: datetime | None = None) -> RunState:
        """Begin a run from the owner's sentence."""
        moment = at or datetime.now()
        parsed = self.agent.nlu.understand(prompt)
        state = RunState(
            run_id=f"run-{moment:%Y%m%d}-{uuid.uuid4().hex[:6]}",
            prompt=prompt,
            config=parsed.config,
            created_at=moment,
            updated_at=moment,
        )
        problems = parsed.config.validate()
        if parsed.intent is not Intent.START_RUN:
            state.status = RunStatus.FAILED
            state.blocked_on = (
                f"that reads as '{parsed.intent.value}', not a request to start a run"
            )
            state.record(
                StageResult(Stage.INTERPRET, False, state.blocked_on, at=moment)
            )
            return state
        if problems:
            state.status = RunStatus.FAILED
            state.blocked_on = "; ".join(problems)
            state.record(
                StageResult(
                    Stage.INTERPRET, False, f"cannot start: {state.blocked_on}", at=moment
                )
            )
            return state

        self.agent.apply_config(parsed.config)
        state.record(
            StageResult(
                Stage.INTERPRET,
                True,
                f"understood: {parsed.config.describe()}",
                data={"confidence": parsed.confidence, "source": parsed.source},
                at=moment,
            )
        )
        state.stage = Stage.RESEARCH
        self.agent.audit.record(
            actor="sequence",
            action="run_started",
            subject=state.run_id,
            reasoning=parsed.config.describe(),
            metadata={"confidence": parsed.confidence},
            at=moment,
        )
        return state

    def advance(self, state: RunState, at: datetime | None = None) -> StageResult:
        """Run the current stage and move on unless it gated."""
        moment = at or datetime.now()
        if not state.is_running:
            return StageResult(
                state.stage, False, f"run is {state.status.value}: {state.blocked_on}", at=moment
            )
        handler = getattr(self, f"_stage_{state.stage.value}")
        result = handler(state, moment)
        state.record(result)
        if result.gate:
            state.status = RunStatus.AWAITING_APPROVAL
            state.blocked_on = result.summary
        elif not result.ok:
            state.status = RunStatus.FAILED
            state.blocked_on = result.summary
        else:
            state.stage = state.stage.next()
        return result

    def run(
        self, state: RunState, max_stages: int = 10, at: datetime | None = None
    ) -> list[StageResult]:
        """Advance until a gate, a failure, or the digest."""
        results: list[StageResult] = []
        for _ in range(max_stages):
            if not state.is_running:
                break
            was = state.stage
            results.append(self.advance(state, at))
            if was is Stage.REPORT:
                break
        return results

    def resume(self, state: RunState, at: datetime | None = None) -> RunState:
        """Un-gate a run once its approvals have been answered."""
        if state.status is not RunStatus.AWAITING_APPROVAL:
            return state
        if self.agent.approvals.pending():
            return state
        state.status = RunStatus.ACTIVE
        state.blocked_on = ""
        # A gate at OPERATE means that turn's work is unfinished: the approved
        # purchase orders still have to be placed and any tracking flushed. Every
        # other stage has already done its work and only needed permission to go
        # on, so those advance.
        if state.stage is not Stage.OPERATE:
            state.stage = state.stage.next()
        self.agent.audit.record(
            actor="sequence",
            action="run_resumed",
            subject=state.run_id,
            reasoning=f"approvals cleared, resuming at {state.stage.value}",
            at=at or datetime.now(),
        )
        return state

    # -- stages ------------------------------------------------------------

    def _stage_interpret(self, state: RunState, at: datetime) -> StageResult:
        # Reached only on a resumed run; start() already interpreted the prompt.
        return StageResult(
            Stage.INTERPRET, True, f"configuration: {state.config.describe()}", at=at
        )

    def _stage_research(self, state: RunState, at: datetime) -> StageResult:
        scored = self.agent.research(state.config.niche, top_n=state.config.max_listings)
        if not scored:
            return StageResult(
                Stage.RESEARCH,
                False,
                f"no candidates found for '{state.config.niche}' in any configured source",
                at=at,
            )
        return StageResult(
            Stage.RESEARCH,
            True,
            f"{len(scored)} candidate(s), best {scored[0].total:.2f} "
            f"({scored[0].candidate.product.title})",
            data={"count": len(scored)},
            at=at,
        )

    def _stage_select(self, state: RunState, at: datetime) -> StageResult:
        selection = self.agent.select(state.config, at=at)
        gated = selection["item"] is not None
        return StageResult(
            Stage.SELECT,
            True,
            selection["summary"],
            gate=gated,
            data={"eligible": selection["eligible"], "blocked": selection["blocked"]},
            at=at,
        )

    def _stage_list(self, state: RunState, at: datetime) -> StageResult:
        published, blocked = self.agent.publish_shortlist(state.config, at=at)
        if not published and blocked:
            return StageResult(
                Stage.LIST,
                False,
                f"every candidate was blocked by channel policy ({len(blocked)} listing(s))",
                data={"blocked": len(blocked)},
                at=at,
            )
        return StageResult(
            Stage.LIST,
            True,
            f"published {len(published)} listing(s), {len(blocked)} blocked by policy",
            data={"published": len(published), "blocked": len(blocked)},
            at=at,
        )

    def _stage_promote(self, state: RunState, at: datetime) -> StageResult:
        promo = self.agent.promote(at=at)
        return StageResult(
            Stage.PROMOTE,
            True,
            promo["summary"],
            gate=promo["gated"],
            data={"campaigns": promo["campaigns"], "posts": promo["posts"]},
            at=at,
        )

    def _stage_operate(self, state: RunState, at: datetime) -> StageResult:
        cycle = self.agent.operate(at=at)
        return StageResult(
            Stage.OPERATE,
            True,
            cycle["summary"],
            gate=cycle["gated"],
            data={k: v for k, v in cycle.items() if isinstance(v, int)},
            at=at,
        )

    def _stage_report(self, state: RunState, at: datetime) -> StageResult:
        digest = self.agent.check_in(at=at)
        return StageResult(
            Stage.REPORT,
            True,
            digest.summary(),
            data={"pending": len(digest.items), "auto_approved": digest.auto_approved},
            at=at,
        )


def _json_safe(value: Any) -> bool:
    return isinstance(value, (str, int, float, bool, type(None)))
