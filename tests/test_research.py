"""Sourcing: gather from every source, de-duplicate, rank transparently."""

from __future__ import annotations

import pytest

from conftest import dollars, make_candidate, make_supplier
from storefront_agent.research import (
    PlaywrightSource,
    ProductResearchAgent,
    ScoreWeights,
    SeedCatalogSource,
)


@pytest.fixture
def catalogue():
    return [
        make_candidate("p-1", title="LED Desk Lamp", category="lighting", demand=0.8, competition=0.3),
        make_candidate("p-2", title="Desk Cable Organiser", category="office", demand=0.6, competition=0.7),
        make_candidate("p-3", title="Ring Light", category="lighting",
                       supplier=make_supplier("sup-slow", rating=3.2, lead_time_days=20),
                       transit_days=20, demand=0.9, competition=0.85),
    ]


class TestSeedCatalogSource:
    def test_matches_on_title_and_category(self, catalogue):
        results = SeedCatalogSource(catalogue).search("lighting")
        assert {c.product.id for c in results} == {"p-1", "p-3"}

    def test_ranks_by_number_of_matching_terms(self, catalogue):
        results = SeedCatalogSource(catalogue).search("desk lamp")
        assert results[0].product.id == "p-1"

    def test_an_empty_niche_returns_the_whole_catalogue(self, catalogue):
        assert len(SeedCatalogSource(catalogue).search("")) == 3

    def test_short_words_are_ignored_as_noise(self, catalogue):
        assert SeedCatalogSource(catalogue).search("an of to") == catalogue

    def test_a_miss_returns_nothing(self, catalogue):
        assert SeedCatalogSource(catalogue).search("snowboards") == []

    def test_limit_is_respected(self, catalogue):
        assert len(SeedCatalogSource(catalogue).search("", limit=2)) == 2


class TestPlaywrightSource:
    def test_disabled_by_default_and_returns_nothing(self):
        source = PlaywrightSource()
        assert not source.is_available()
        assert source.search("anything") == []

    def test_enabling_without_an_implementation_raises_with_guidance(self):
        source = PlaywrightSource(catalogue_url="https://example.com", enabled=True)
        assert source.is_available()
        with pytest.raises(NotImplementedError, match="terms permit"):
            source.search("anything")


class TestGathering:
    def test_duplicates_across_sources_keep_the_cheaper_landed_cost(self):
        dear = make_candidate("p-1", unit_cost=10, ship_cost=5)
        cheap = make_candidate("p-1", unit_cost=6, ship_cost=1)
        agent = ProductResearchAgent(
            sources=[SeedCatalogSource([dear]), SeedCatalogSource([cheap])]
        )
        gathered = agent.gather("led")
        assert len(gathered) == 1
        assert gathered[0].landed_cost == dollars(7)

    def test_same_product_from_two_suppliers_is_kept_twice(self):
        first = make_candidate("p-1", supplier=make_supplier("sup-a"))
        second = make_candidate("p-1", supplier=make_supplier("sup-b"))
        agent = ProductResearchAgent(sources=[SeedCatalogSource([first, second])])
        assert len(agent.gather("led")) == 2

    def test_unavailable_sources_are_skipped(self, catalogue):
        agent = ProductResearchAgent(
            sources=[PlaywrightSource(enabled=False), SeedCatalogSource(catalogue)]
        )
        assert len(agent.gather("")) == 3


class TestScoring:
    def test_score_components_are_all_present_and_bounded(self, catalogue):
        score = ProductResearchAgent().score(catalogue[0])
        assert set(score.components) == {"margin", "demand", "competition", "speed", "supplier"}
        assert all(0.0 <= v <= 1.0 for v in score.components.values())
        assert 0.0 <= score.total <= 1.0

    def test_slow_shipping_is_penalised_despite_high_demand(self, catalogue):
        agent = ProductResearchAgent()
        fast, slow = agent.score(catalogue[0]), agent.score(catalogue[2])
        assert slow.components["speed"] < fast.components["speed"]
        assert slow.total < fast.total

    def test_low_competition_scores_higher(self):
        agent = ProductResearchAgent()
        crowded = agent.score(make_candidate(competition=0.9))
        open_field = agent.score(make_candidate(competition=0.1))
        assert open_field.components["competition"] > crowded.components["competition"]

    def test_projected_price_uses_a_charm_ending(self):
        candidate = make_candidate(unit_cost=6.20, ship_cost=1.80)
        assert ProductResearchAgent().project_price(candidate).cents % 100 == 99

    def test_projected_price_clears_landed_cost_at_the_target_markup(self):
        candidate = make_candidate(unit_cost=6.20, ship_cost=1.80)
        agent = ProductResearchAgent(target_markup=3.0)
        assert agent.project_price(candidate) > candidate.landed_cost * 2

    def test_research_ranks_and_truncates(self, catalogue):
        agent = ProductResearchAgent(sources=[SeedCatalogSource(catalogue)])
        results = agent.research("", top_n=2)
        assert len(results) == 2
        assert results[0].total >= results[1].total

    def test_ties_break_on_title_so_ordering_is_stable(self):
        twins = [make_candidate("p-b", title="Bravo"), make_candidate("p-a", title="Alpha")]
        agent = ProductResearchAgent(sources=[SeedCatalogSource(twins)])
        assert [r.candidate.product.title for r in agent.research("")] == ["Alpha", "Bravo"]

    def test_weights_change_the_ranking(self, catalogue):
        demand_led = ProductResearchAgent(
            sources=[SeedCatalogSource(catalogue)],
            weights=ScoreWeights(margin=0.0, demand=1.0, competition=0.0, speed=0.0, supplier=0.0),
        )
        assert demand_led.research("")[0].candidate.product.id == "p-3"

    def test_describe_exposes_the_breakdown(self, catalogue):
        described = ProductResearchAgent().score(catalogue[0]).describe()
        assert "margin" in described and "demand" in described
