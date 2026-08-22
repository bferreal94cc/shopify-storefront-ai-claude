"""Posting is approval-gated and spaced out by default."""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import NOW, make_listing
from storefront_agent.policy import AutomationPolicy
from storefront_agent.providers import StubProvider
from storefront_agent.social import (
    InMemorySocialConnector,
    PostScheduler,
    PostState,
    SocialAgent,
    SocialPlatform,
)


@pytest.fixture
def agent():
    return SocialAgent(
        connectors={p: InMemorySocialConnector(platform=p) for p in SocialPlatform}
    )


class TestDrafting:
    def test_the_fallback_writes_a_usable_post(self, agent):
        post = agent.draft(make_listing(), SocialPlatform.INSTAGRAM)
        assert post.body and post.source == "fallback"
        assert post.state is PostState.DRAFT

    def test_a_model_response_supplies_body_and_hashtags(self):
        agent = SocialAgent(provider=StubProvider(default="A nice post\n#one #two #three"))
        post = agent.draft(make_listing(), SocialPlatform.INSTAGRAM)
        assert post.body == "A nice post"
        assert post.hashtags == ("one", "two", "three")

    def test_hashtags_are_capped_per_platform(self):
        agent = SocialAgent(provider=StubProvider(default="Post\n" + " ".join(f"#t{i}" for i in range(20))))
        assert len(agent.draft(make_listing(), SocialPlatform.X).hashtags) <= 2

    def test_an_overlong_post_is_trimmed_to_fit(self):
        agent = SocialAgent(provider=StubProvider(default=("word " * 200) + "\n#tag"))
        post = agent.draft(make_listing(), SocialPlatform.X)
        assert post.fits() and len(post.rendered) <= 280

    def test_rendering_joins_body_and_hashtags(self):
        agent = SocialAgent(provider=StubProvider(default="Body here\n#one"))
        assert agent.draft(make_listing(), SocialPlatform.INSTAGRAM).rendered == "Body here\n\n#one"


class TestGating:
    def test_posts_await_approval_by_default(self, agent):
        post = agent.draft(make_listing(), SocialPlatform.INSTAGRAM)
        decision = agent.submit(post)
        assert decision.needs_owner
        assert post.state is PostState.AWAITING_APPROVAL

    def test_nothing_publishes_while_unapproved(self, agent):
        agent.campaign_for(make_listing(), (SocialPlatform.INSTAGRAM,), NOW)
        assert agent.publish_due(NOW + timedelta(days=1)) == []

    def test_approval_makes_a_post_schedulable_and_publishable(self, agent):
        posts = agent.campaign_for(make_listing(), (SocialPlatform.INSTAGRAM,), NOW)
        agent.approve(posts[0])
        agent.scheduler.schedule(posts, NOW)
        published = agent.publish_due(NOW + timedelta(days=1))
        assert len(published) == 1 and published[0].state is PostState.PUBLISHED
        assert published[0].external_id.startswith("instagram-")

    def test_an_aggressive_policy_schedules_immediately(self):
        agent = SocialAgent(
            policy=AutomationPolicy.aggressive(),
            connectors={SocialPlatform.X: InMemorySocialConnector(platform=SocialPlatform.X)},
        )
        post = agent.campaign_for(make_listing(), (SocialPlatform.X,), NOW)[0]
        assert post.state is PostState.SCHEDULED

    def test_a_prohibited_claim_rejects_the_post_outright(self, agent):
        agent.provider = StubProvider(default="This lamp cures eye strain\n#health")
        post = agent.draft(make_listing(), SocialPlatform.X)
        decision = agent.submit(post)
        assert decision.is_blocked and post.state is PostState.REJECTED

    def test_approving_a_post_that_is_not_waiting_raises(self, agent):
        post = agent.draft(make_listing(), SocialPlatform.X)
        with pytest.raises(ValueError, match="not awaiting approval"):
            agent.approve(post)

    def test_publishing_an_unscheduled_post_raises(self, agent):
        post = agent.draft(make_listing(), SocialPlatform.X)
        with pytest.raises(ValueError, match="only scheduled posts publish"):
            agent.connectors[SocialPlatform.X].publish(post)


class TestScheduling:
    def test_posts_on_one_platform_are_spaced_by_its_minimum_gap(self, agent):
        posts = [agent.draft(make_listing(), SocialPlatform.INSTAGRAM) for _ in range(3)]
        PostScheduler().schedule(posts, NOW)
        gaps = [
            (b.scheduled_for - a.scheduled_for).total_seconds() / 3600
            for a, b in zip(posts, posts[1:])
        ]
        assert all(gap >= SocialPlatform.INSTAGRAM.min_gap_hours for gap in gaps)

    def test_different_platforms_do_not_delay_each_other(self, agent):
        posts = [
            agent.draft(make_listing(), SocialPlatform.INSTAGRAM),
            agent.draft(make_listing(), SocialPlatform.X),
        ]
        PostScheduler().schedule(posts, NOW)
        assert posts[0].scheduled_for == posts[1].scheduled_for == NOW

    def test_only_due_posts_are_returned(self, agent):
        posts = agent.campaign_for(make_listing(), (SocialPlatform.X,), NOW)
        for post in posts:
            agent.approve(post)
        agent.scheduler.schedule(posts, NOW + timedelta(days=3))
        assert PostScheduler().due(posts, NOW) == []

    def test_unapproved_posts_are_left_unscheduled(self, agent):
        posts = agent.campaign_for(make_listing(), (SocialPlatform.INSTAGRAM,), NOW)
        assert all(p.scheduled_for is None for p in posts)

    def test_a_backlog_approved_late_does_not_publish_as_a_burst(self, agent):
        # Three posts drafted on day zero, all cleared together two days later.
        # Scheduling them from the draft time would make every one due at once.
        drafted = []
        for _ in range(3):
            drafted += agent.campaign_for(make_listing(), (SocialPlatform.INSTAGRAM,), NOW)
        approved_at = NOW + timedelta(days=2)
        for post in drafted:
            agent.approve(post, at=approved_at)

        times = sorted(p.scheduled_for for p in drafted)
        assert all(t >= approved_at for t in times)
        gaps = [(b - a).total_seconds() / 3600 for a, b in zip(times, times[1:])]
        assert all(gap >= SocialPlatform.INSTAGRAM.min_gap_hours for gap in gaps)
        assert len(PostScheduler().due(drafted, approved_at)) == 1

    def test_awaiting_approval_lists_what_the_owner_must_clear(self, agent):
        agent.campaign_for(make_listing(), (SocialPlatform.INSTAGRAM, SocialPlatform.X), NOW)
        assert len(agent.awaiting_approval()) == 2
