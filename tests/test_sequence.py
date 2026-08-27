"""The sequence gates on humans and survives the process."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from conftest import NOW, dollars
from storefront_agent.approvals import Decision
from storefront_agent.domain import Channel
from storefront_agent.nlu import RunConfig
from storefront_agent.policy import AutonomyLevel
from storefront_agent.sequence import RunState, RunStatus, Stage, StageResult, StorefrontSequence

PROMPT = (
    "start selling desk accessories on shopify, amazon and ebay, "
    "budget $2000, balanced, 25% margin, 5 products"
)


@pytest.fixture
def sequence(agent, tmp_path):
    return StorefrontSequence(agent, run_dir=tmp_path)


class TestStageOrder:
    def test_stages_run_in_the_documented_order(self):
        stage, walk = Stage.INTERPRET, []
        for _ in range(7):
            walk.append(stage.value)
            stage = stage.next()
        assert walk == [
            "interpret", "research", "select", "list", "promote", "operate", "report"
        ]

    def test_report_loops_back_to_operate_as_the_steady_state(self):
        assert Stage.REPORT.next() is Stage.OPERATE

    def test_ordinals_are_monotonic(self):
        assert Stage.INTERPRET.ordinal < Stage.OPERATE.ordinal < Stage.REPORT.ordinal


class TestStarting:
    def test_a_good_prompt_starts_at_research(self, sequence):
        state = sequence.start(PROMPT, at=NOW)
        assert state.is_running and state.stage is Stage.RESEARCH
        assert state.config.niche == "desk accessories"

    def test_a_non_start_intent_fails_with_an_explanation(self, sequence):
        state = sequence.start("check in", at=NOW)
        assert state.status is RunStatus.FAILED
        assert "not a request to start a run" in state.blocked_on

    def test_an_incomplete_prompt_fails_before_doing_anything(self, sequence):
        state = sequence.start("launch a run", at=NOW)
        assert state.status is RunStatus.FAILED
        assert "niche" in state.blocked_on
        assert sequence.agent.context.listings == []

    def test_starting_applies_the_configuration_to_policy(self, sequence):
        sequence.start(PROMPT, at=NOW)
        assert sequence.agent.policy.daily_spend_cap == dollars(2000)
        assert sequence.agent.policy.autonomy is AutonomyLevel.BALANCED

    def test_starting_creates_the_named_storefronts(self, sequence):
        sequence.start(PROMPT, at=NOW)
        assert set(sequence.agent.context.channels()) == set(Channel)

    def test_run_ids_are_unique(self, sequence):
        first = sequence.start(PROMPT, at=NOW).run_id
        second = sequence.start(PROMPT, at=NOW).run_id
        assert first != second


class TestGating:
    def test_the_run_pauses_at_the_first_gate(self, sequence):
        state = sequence.start(PROMPT, at=NOW)
        results = sequence.run(state, at=NOW)
        assert state.status is RunStatus.AWAITING_APPROVAL
        assert results[-1].gate

    def test_nothing_downstream_runs_while_gated(self, sequence):
        state = sequence.start(PROMPT, at=NOW)
        sequence.run(state, at=NOW)
        gated_stage = state.stage
        sequence.run(state, at=NOW)
        assert state.stage is gated_stage

    def test_resume_does_nothing_while_approvals_are_outstanding(self, sequence):
        state = sequence.start(PROMPT, at=NOW)
        sequence.run(state, at=NOW)
        sequence.resume(state, at=NOW)
        assert state.status is RunStatus.AWAITING_APPROVAL

    def test_resume_advances_once_the_queue_is_clear(self, sequence):
        state = sequence.start(PROMPT, at=NOW)
        sequence.run(state, at=NOW)
        sequence.agent.approve_all(at=NOW)
        sequence.resume(state, at=NOW)
        assert state.status is RunStatus.ACTIVE

    def test_a_gate_at_operate_re_runs_operate_rather_than_skipping_it(self, sequence):
        """Approved purchase orders still have to be placed."""
        state = sequence.start(PROMPT, at=NOW)
        state.stage = Stage.OPERATE
        state.status = RunStatus.AWAITING_APPROVAL
        sequence.resume(state, at=NOW)
        assert state.stage is Stage.OPERATE


class TestPersistence:
    def test_a_run_round_trips_through_json(self, sequence, tmp_path):
        state = sequence.start(PROMPT, at=NOW)
        sequence.run(state, at=NOW)
        path = state.save(tmp_path)
        reloaded = RunState.load(path)
        assert reloaded.run_id == state.run_id
        assert reloaded.stage is state.stage
        assert reloaded.status is state.status
        assert len(reloaded.history) == len(state.history)

    def test_the_configuration_survives_the_round_trip(self, sequence, tmp_path):
        state = sequence.start(PROMPT, at=NOW)
        reloaded = RunState.load(state.save(tmp_path))
        assert reloaded.config.niche == "desk accessories"
        assert reloaded.config.budget == dollars(2000)
        assert reloaded.config.channels == state.config.channels
        assert reloaded.config.autonomy is AutonomyLevel.BALANCED

    def test_a_run_with_no_budget_round_trips(self, sequence, tmp_path):
        state = RunState("run-x", "p", RunConfig(niche="lamps", channels=(Channel.SHOPIFY,)))
        assert RunState.load(state.save(tmp_path)).config.budget is None

    def test_the_saved_file_is_readable_json(self, sequence, tmp_path):
        state = sequence.start(PROMPT, at=NOW)
        payload = json.loads(state.save(tmp_path).read_text())
        assert payload["run_id"] == state.run_id
        assert payload["config"]["niche"] == "desk accessories"

    def test_unserialisable_stage_data_is_dropped_not_fatal(self):
        result = StageResult(Stage.RESEARCH, True, "ok", data={"count": 3, "obj": object()})
        assert result.to_dict()["data"] == {"count": 3}

    def test_history_records_every_stage_that_ran(self, sequence):
        state = sequence.start(PROMPT, at=NOW)
        sequence.run(state, at=NOW)
        assert [r.stage for r in state.history][:3] == [
            Stage.INTERPRET, Stage.RESEARCH, Stage.SELECT
        ]

    def test_last_finds_the_most_recent_result_for_a_stage(self, sequence):
        state = sequence.start(PROMPT, at=NOW)
        sequence.run(state, at=NOW)
        assert state.last(Stage.RESEARCH) is not None
        assert state.last(Stage.REPORT) is None


class TestFailureModes:
    def test_a_niche_with_no_candidates_fails_the_run(self, sequence):
        state = sequence.start(
            "start selling submarine periscopes on shopify, budget $500", at=NOW
        )
        sequence.run(state, at=NOW)
        assert state.status is RunStatus.FAILED
        assert "no candidates" in state.blocked_on

    def test_advancing_a_failed_run_is_a_no_op_with_an_explanation(self, sequence):
        state = sequence.start("check in", at=NOW)
        result = sequence.advance(state, at=NOW)
        assert not result.ok and "failed" in result.summary

    def test_run_stops_at_the_stage_budget(self, sequence):
        state = sequence.start(PROMPT, at=NOW)
        assert len(sequence.run(state, max_stages=2, at=NOW)) == 2
