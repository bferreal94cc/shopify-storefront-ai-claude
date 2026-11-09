"""One sentence in, a safe run configuration out."""

from __future__ import annotations

import pytest

from conftest import dollars
from storefront_agent.domain import Channel
from storefront_agent.nlu import (
    Intent,
    NLUPipeline,
    PromptParser,
    RunConfig,
    parse_autonomy,
    parse_channels,
    parse_money,
    parse_niche,
)
from storefront_agent.policy import AutonomyLevel
from storefront_agent.providers import StubProvider


class TestIntent:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("start selling lamps on shopify", Intent.START_RUN),
            ("launch a run", Intent.START_RUN),
            ("check in", Intent.CHECK_IN),
            ("what needs me?", Intent.CHECK_IN),
            ("approve them all", Intent.APPROVE_ALL),
            ("pause everything", Intent.PAUSE),
            ("what's the status?", Intent.STATUS),
            ("research products for home office", Intent.RESEARCH),
            ("the weather is nice", Intent.UNKNOWN),
        ],
    )
    def test_intents_are_recognised(self, text, expected):
        assert PromptParser().parse(text).intent is expected

    def test_an_unknown_intent_is_not_actionable(self):
        assert not PromptParser().parse("the weather is nice").is_actionable


class TestMoney:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("$500", 500), ("budget of 2k", 2000), ("500 dollars", 500),
            ("$1,250.50", 1250.50), ("$1.5m", 1_500_000), ("cap of $75", 75),
        ],
    )
    def test_money_forms_parse(self, text, expected):
        assert parse_money(text) == dollars(expected)

    def test_no_money_is_none_not_zero(self):
        assert parse_money("start selling lamps") is None


class TestChannels:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("on shopify", (Channel.SHOPIFY,)),
            ("shopify and ebay", (Channel.SHOPIFY, Channel.EBAY)),
            ("on e-bay", (Channel.EBAY,)),
            ("across all my storefronts", (Channel.SHOPIFY, Channel.AMAZON, Channel.EBAY)),
            ("every marketplace", (Channel.SHOPIFY, Channel.AMAZON, Channel.EBAY)),
        ],
    )
    def test_channels_are_extracted(self, text, expected):
        assert parse_channels(text) == expected

    def test_repeats_are_deduplicated(self):
        assert parse_channels("shopify, shopify and shopify") == (Channel.SHOPIFY,)

    def test_no_channel_is_empty(self):
        assert parse_channels("start selling lamps") == ()


class TestNiche:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("start selling desk accessories on shopify", "desk accessories"),
            ("sourcing eco-friendly kitchen gadgets across all storefronts", "eco-friendly kitchen gadgets"),
            ("begin stocking pet grooming tools on amazon", "pet grooming tools"),
            ("research products for home office on ebay", "home office"),
            ("find items in camping gear for shopify", "camping gear"),
            ("niche is minimalist watches, list everywhere", "minimalist watches"),
        ],
    )
    def test_niches_are_extracted_from_many_phrasings(self, text, expected):
        assert parse_niche(text) == expected

    def test_no_niche_is_empty(self):
        assert parse_niche("launch a run") == ""


class TestAutonomy:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("be aggressive", AutonomyLevel.AGGRESSIVE),
            ("hands-off please", AutonomyLevel.AGGRESSIVE),
            ("be careful", AutonomyLevel.CONSERVATIVE),
            ("ask me first", AutonomyLevel.CONSERVATIVE),
            ("balanced", AutonomyLevel.BALANCED),
        ],
    )
    def test_autonomy_words_map(self, text, expected):
        assert parse_autonomy(text) is expected

    def test_unstated_autonomy_is_none_so_the_safe_default_stands(self):
        assert parse_autonomy("start selling lamps") is None


class TestRunConfig:
    def test_a_full_prompt_populates_everything(self):
        parsed = PromptParser().parse(
            "start selling desk accessories on shopify and ebay, budget $500, "
            "aggressive, 30% margin, 12 products"
        )
        config = parsed.config
        assert config.niche == "desk accessories"
        assert config.channels == (Channel.SHOPIFY, Channel.EBAY)
        assert config.budget == dollars(500)
        assert config.margin_floor == 0.30
        assert config.autonomy is AutonomyLevel.AGGRESSIVE
        assert config.max_listings == 12
        assert parsed.confidence == 1.0

    def test_defaults_are_conservative_not_permissive(self):
        config = RunConfig()
        assert config.autonomy is AutonomyLevel.CONSERVATIVE
        assert config.budget is None

    def test_validation_names_what_is_missing(self):
        problems = RunConfig().validate()
        assert any("niche" in p for p in problems)
        assert any("storefront" in p for p in problems)

    def test_a_complete_config_validates(self):
        config = RunConfig(niche="lamps", channels=(Channel.SHOPIFY,))
        assert config.validate() == []

    @pytest.mark.parametrize(
        "kwargs,fragment",
        [
            ({"margin_floor": 1.5}, "margin floor"),
            ({"max_listings": 0}, "max listings"),
            ({"budget": dollars(0)}, "budget"),
        ],
    )
    def test_out_of_range_values_are_rejected(self, kwargs, fragment):
        config = RunConfig(niche="lamps", channels=(Channel.SHOPIFY,), **kwargs)
        assert any(fragment in p for p in config.validate())

    def test_the_budget_becomes_both_the_daily_and_the_hard_cap(self):
        config = RunConfig(niche="x", channels=(Channel.SHOPIFY,), budget=dollars(750))
        policy = config.to_policy()
        assert policy.daily_spend_cap == dollars(750)
        assert policy.po_hard_cap == dollars(750)


class TestPipeline:
    def test_the_model_is_only_asked_for_a_missing_niche(self):
        provider = StubProvider(default="portable phone chargers")
        parsed = NLUPipeline(provider=provider).understand("start a run on amazon with $300")
        assert parsed.config.niche == "portable phone chargers"
        assert parsed.source == "pattern+stub"
        assert len(provider.calls) == 1

    def test_the_model_is_not_consulted_when_patterns_suffice(self):
        provider = StubProvider(default="something else")
        parsed = NLUPipeline(provider=provider).understand(
            "start selling desk lamps on shopify"
        )
        assert parsed.config.niche == "desk lamps"
        assert provider.calls == []

    def test_a_junk_model_answer_is_rejected(self):
        parsed = NLUPipeline(provider=StubProvider(default="{}")).understand(
            "start a run on amazon"
        )
        assert parsed.config.niche == ""

    def test_a_refusal_is_rejected(self):
        parsed = NLUPipeline(provider=StubProvider(default="NONE")).understand(
            "start a run on amazon"
        )
        assert parsed.config.niche == ""

    def test_the_model_cannot_set_a_budget(self):
        parsed = NLUPipeline(
            provider=StubProvider(default="lamps with a $999999 budget")
        ).understand("start a run on amazon")
        assert parsed.config.budget is None
