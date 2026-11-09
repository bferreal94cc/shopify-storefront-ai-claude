"""Ad copy must not invent claims, and budgets must be paced."""

from __future__ import annotations

from datetime import date

import pytest

from conftest import dollars, make_listing
from storefront_agent.marketing import (
    AdPlatform,
    BudgetPacer,
    CampaignState,
    MarketingAgent,
    screen_claims,
)
from storefront_agent.policy import AutomationPolicy
from storefront_agent.providers import StubProvider


class TestClaimScreening:
    @pytest.mark.parametrize(
        "text",
        [
            "FDA-approved lamp", "FDA approved lamp", "Clinically proven results",
            "This cures eye strain", "It treats fatigue", "100% guaranteed",
            "best in the world", "guaranteed income",
        ],
    )
    def test_prohibited_claims_are_caught(self, text):
        assert screen_claims(text)

    @pytest.mark.parametrize(
        "text",
        ["A warm dimmable desk lamp", "Ships in six days", "Free returns within 30 days"],
    )
    def test_ordinary_copy_passes(self, text):
        assert screen_claims(text) == []

    def test_the_issue_names_the_phrase_and_the_reason(self):
        issue = screen_claims("Our FDA-approved device")[0]
        assert "FDA-approved" in issue.phrase
        assert "regulatory" in issue.reason


class TestCreative:
    def test_the_fallback_writes_usable_copy_with_no_model(self):
        creative = MarketingAgent().write_creative(make_listing(), AdPlatform.META)
        assert creative.headline and creative.body and creative.is_clean
        assert creative.source == "fallback"

    def test_a_model_response_is_used_when_complete(self):
        agent = MarketingAgent(provider=StubProvider(default="Headline\nBody copy\nBuy now"))
        creative = agent.write_creative(make_listing(), AdPlatform.META)
        assert creative.headline == "Headline" and creative.source == "stub"

    @pytest.mark.parametrize("platform", list(AdPlatform))
    def test_copy_respects_each_platform_limit(self, platform):
        agent = MarketingAgent(provider=StubProvider(default="X" * 400 + "\n" + "Y" * 400 + "\nGo"))
        creative = agent.write_creative(make_listing(), platform)
        assert len(creative.headline) <= platform.headline_limit
        assert len(creative.body) <= platform.body_limit

    def test_variants_differ(self):
        agent = MarketingAgent()
        listing = make_listing()
        a = agent.write_creative(listing, AdPlatform.META, "A")
        b = agent.write_creative(listing, AdPlatform.META, "B")
        assert (a.headline, a.body) != (b.headline, b.body)


class TestCampaigns:
    def test_a_small_budget_runs_unattended(self):
        agent = MarketingAgent(policy=AutomationPolicy(ad_auto_approve_cap=dollars(500)))
        campaign, decision = agent.build_campaign(
            make_listing(), AdPlatform.META, dollars(10), 7
        )
        assert decision.is_allowed
        assert campaign.state is CampaignState.ACTIVE
        assert campaign.started_on is not None

    def test_a_large_budget_escalates(self):
        campaign, decision = MarketingAgent().build_campaign(
            make_listing(), AdPlatform.META, dollars(200), 30
        )
        assert decision.needs_owner
        assert campaign.state is CampaignState.AWAITING_APPROVAL

    def test_total_budget_is_daily_times_duration(self):
        campaign, _ = MarketingAgent().build_campaign(
            make_listing(), AdPlatform.META, dollars(12), 7
        )
        assert campaign.total_budget == dollars(84)

    def test_a_campaign_whose_variants_all_trip_the_screen_is_rejected(self):
        agent = MarketingAgent(
            provider=StubProvider(default="FDA-approved cure\n100% guaranteed safe\nBuy")
        )
        campaign, decision = agent.build_campaign(
            make_listing(), AdPlatform.META, dollars(5), 3
        )
        assert decision.is_blocked and decision.rule == "ad_claims"
        assert campaign.state is CampaignState.REJECTED

    def test_a_zero_duration_is_rejected_at_the_boundary(self):
        with pytest.raises(ValueError, match="at least 1 day"):
            MarketingAgent().build_campaign(make_listing(), AdPlatform.META, dollars(5), 0)

    def test_campaign_ids_are_unique(self):
        agent = MarketingAgent()
        ids = {
            agent.build_campaign(make_listing(), AdPlatform.META, dollars(5), 3)[0].id
            for _ in range(3)
        }
        assert len(ids) == 3


class TestPacing:
    def _campaign(self, spent, started=date(2026, 8, 11)):
        campaign, _ = MarketingAgent().build_campaign(
            make_listing(), AdPlatform.META, dollars(10), 20
        )
        campaign.started_on = started
        campaign.spent = dollars(spent)
        return campaign

    def test_overspending_is_reported_as_ahead_of_pace(self):
        campaign = self._campaign(150)
        assert "ahead of pace" in BudgetPacer().verdict(campaign, date(2026, 8, 21))

    def test_on_target_is_on_pace(self):
        assert BudgetPacer().verdict(self._campaign(98), date(2026, 8, 21)) == "on pace"

    def test_underspending_is_reported_as_behind(self):
        assert "behind pace" in BudgetPacer().verdict(self._campaign(40), date(2026, 8, 21))

    def test_no_spend_yet_is_stated_plainly(self):
        assert BudgetPacer().verdict(self._campaign(0), date(2026, 8, 21)) == "no spend yet"

    def test_spending_before_the_start_date_is_flagged(self):
        campaign = self._campaign(50, started=date(2026, 9, 1))
        assert "before the campaign start" in BudgetPacer().verdict(campaign, date(2026, 8, 21))

    def test_expected_spend_stops_at_the_campaign_end(self):
        campaign = self._campaign(0)
        far_future = BudgetPacer().expected_spend(campaign, date(2027, 1, 1))
        assert far_future == dollars(200)
