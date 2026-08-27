"""The CLI is the front door; its argument validation is a boundary."""

from __future__ import annotations

import argparse

import pytest

from storefront_agent.cli import _autonomy, _latest_run, _port, main
from storefront_agent.policy import AutonomyLevel


class TestArgumentValidation:
    @pytest.mark.parametrize("value,expected", [("8787", 8787), ("65535", 65535)])
    def test_valid_ports_parse(self, value, expected):
        assert _port(value) == expected

    @pytest.mark.parametrize("value", ["0", "80", "65536", "-1", "abc", "8.5"])
    def test_bad_ports_are_rejected_at_the_boundary(self, value):
        with pytest.raises(argparse.ArgumentTypeError):
            _port(value)

    def test_privileged_ports_are_rejected_with_guidance(self):
        with pytest.raises(argparse.ArgumentTypeError, match="above 1023"):
            _port("443")

    @pytest.mark.parametrize("value", ["conservative", "BALANCED", "Aggressive"])
    def test_autonomy_is_case_insensitive(self, value):
        assert isinstance(_autonomy(value), AutonomyLevel)

    def test_an_unknown_autonomy_lists_the_options(self):
        with pytest.raises(argparse.ArgumentTypeError, match="conservative"):
            _autonomy("reckless")


class TestRunDiscovery:
    def test_a_missing_directory_yields_nothing(self, tmp_path):
        assert _latest_run(tmp_path / "absent") is None

    def test_an_empty_directory_yields_nothing(self, tmp_path):
        assert _latest_run(tmp_path) is None


class TestEntryPoint:
    def test_no_arguments_prints_help_and_signals_misuse(self, capsys):
        assert main([]) == 1
        assert "storefront-agent" in capsys.readouterr().out

    def test_an_unusable_prompt_exits_two_with_an_example(self, capsys, tmp_path):
        assert main(["the weather is nice", "--run-dir", str(tmp_path)]) == 2
        assert "start selling" in capsys.readouterr().out

    def test_check_in_without_a_run_says_so(self, capsys, tmp_path):
        assert main(["--check-in", "--run-dir", str(tmp_path)]) == 2
        assert "No runs found" in capsys.readouterr().out

    def test_a_real_prompt_runs_and_persists(self, capsys, tmp_path):
        code = main([
            "start selling desk accessories on shopify and ebay, budget $900, 25% margin",
            "--run-dir", str(tmp_path),
        ])
        assert code == 0
        output = capsys.readouterr().out
        assert "RUN run-" in output and "What needs you" in output
        assert list(tmp_path.glob("run-*.json"))

    def test_check_in_then_reports_the_persisted_run(self, capsys, tmp_path):
        main([
            "start selling desk accessories on shopify, budget $900",
            "--run-dir", str(tmp_path),
        ])
        capsys.readouterr()
        assert main(["--check-in", "--run-dir", str(tmp_path)]) == 0
        assert "CHECK-IN" in capsys.readouterr().out

    def test_the_demo_runs_clean_end_to_end(self, capsys, tmp_path):
        assert main(["--demo", "--run-dir", str(tmp_path)]) == 0
        output = capsys.readouterr().out
        for marker in (
            "1. START PROMPT", "3. FIRST CHECK-IN", "4. CUSTOMERS ORDER",
            "6. SUPPLIERS SHIP", "chain valid: True", "demo result: CLEAN",
        ):
            assert marker in output

    def test_the_demo_ships_orders_and_holds_the_problem_ones(self, capsys, tmp_path):
        main(["--demo", "--run-dir", str(tmp_path)])
        output = capsys.readouterr().out
        assert "order(s) shipped" in output
        assert "order(s) held" in output

    def test_autonomy_flag_is_accepted(self, capsys, tmp_path):
        code = main([
            "start selling desk accessories on shopify, budget $900",
            "--autonomy", "aggressive", "--run-dir", str(tmp_path),
        ])
        assert code == 0
