"""Social post generation, scheduling and approval-gated publishing.

The default posture is deliberately restrained, and it is worth saying why.
Bulk automated posting is the fastest way to lose the accounts a storefront
depends on: every major platform's terms restrict automated publishing, and
enforcement is account-level and usually permanent. So this module generates
drafts and *queues* them; ``policy.social_requires_approval`` decides whether
they publish, and it defaults to requiring the owner.

Two further guards follow from the same reasoning:

* :class:`PostScheduler` enforces a minimum gap between posts on a platform, so
  even an approved batch drips out rather than arriving as a burst that looks
  exactly like spam.
* :class:`SocialConnector` is an official-API port. There is no browser-driving
  fallback here on purpose -- automating a consumer account through the UI
  breaches the terms of every platform listed below.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from .domain import Listing
from .marketing import screen_claims
from .policy import AutomationPolicy, PolicyDecision
from .providers import LLMProvider, StubProvider


class SocialPlatform(str, Enum):
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"
    TIKTOK = "tiktok"
    X = "x"
    PINTEREST = "pinterest"

    @property
    def label(self) -> str:
        return {
            "instagram": "Instagram", "facebook": "Facebook", "tiktok": "TikTok",
            "x": "X", "pinterest": "Pinterest",
        }[self.value]

    @property
    def body_limit(self) -> int:
        return {"instagram": 2200, "facebook": 2000, "tiktok": 2200, "x": 280,
                "pinterest": 500}[self.value]

    @property
    def hashtag_limit(self) -> int:
        return {"instagram": 10, "facebook": 3, "tiktok": 6, "x": 2, "pinterest": 5}[self.value]

    @property
    def min_gap_hours(self) -> float:
        """Minimum spacing between our own posts on this platform."""
        return {"instagram": 8.0, "facebook": 8.0, "tiktok": 6.0, "x": 3.0,
                "pinterest": 2.0}[self.value]


class PostState(str, Enum):
    DRAFT = "draft"
    AWAITING_APPROVAL = "awaiting_approval"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    REJECTED = "rejected"


_SOCIAL_SYSTEM = (
    "You write short, plain social posts for a small online shop. One or two "
    "sentences. No hype, no emoji spam, no invented claims, no fake urgency. "
    "Return the post text on the first line and hashtags on the second."
)


@dataclass
class SocialPost:
    """One post, for one platform, about one listing."""

    id: str
    platform: SocialPlatform
    body: str
    hashtags: tuple[str, ...] = ()
    sku: str = ""
    scheduled_for: datetime | None = None
    state: PostState = PostState.DRAFT
    external_id: str = ""
    source: str = "fallback"

    @property
    def rendered(self) -> str:
        tags = " ".join(f"#{t.lstrip('#')}" for t in self.hashtags)
        return f"{self.body}\n\n{tags}".strip()

    def fits(self) -> bool:
        return len(self.rendered) <= self.platform.body_limit

    def describe(self) -> str:
        when = f" at {self.scheduled_for:%d %b %H:%M}" if self.scheduled_for else ""
        return f"{self.platform.label}{when}: {self.body[:60]} [{self.state.value}]"


class SocialConnector(ABC):
    """Publishes through a platform's official API."""

    platform: SocialPlatform

    @abstractmethod
    def publish(self, post: SocialPost) -> str:
        """Publish and return the platform's post id."""


@dataclass
class InMemorySocialConnector(SocialConnector):
    """Reference adapter. Captures what would have been published."""

    platform: SocialPlatform = SocialPlatform.INSTAGRAM
    outbox: list[SocialPost] = field(default_factory=list)
    _counter: int = field(default=0, init=False)

    def publish(self, post: SocialPost) -> str:
        if post.state is not PostState.SCHEDULED:
            raise ValueError(f"{post.id} is {post.state.value}; only scheduled posts publish")
        self._counter += 1
        external = f"{self.platform.value}-{self._counter:05d}"
        post.external_id = external
        post.state = PostState.PUBLISHED
        self.outbox.append(post)
        return external


@dataclass
class PostScheduler:
    """Spaces posts out so an approved batch never lands as a burst."""

    def schedule(
        self, posts: list[SocialPost], start: datetime
    ) -> list[SocialPost]:
        """Assign times, respecting each platform's minimum gap."""
        last_on: dict[SocialPlatform, datetime] = {}
        for post in posts:
            gap = timedelta(hours=post.platform.min_gap_hours)
            previous = last_on.get(post.platform)
            when = start if previous is None else max(start, previous + gap)
            post.scheduled_for = when
            last_on[post.platform] = when
        return posts

    def next_slot(
        self, posts: list[SocialPost], post: SocialPost, now: datetime
    ) -> datetime:
        """Earliest time ``post`` may go out without crowding its platform.

        Unlike :meth:`schedule`, which spaces a batch against itself, this
        looks at what is already on the books for that platform -- which is
        what a post approved long after drafting has to fit around.
        """
        gap = timedelta(hours=post.platform.min_gap_hours)
        taken = [
            p.scheduled_for
            for p in posts
            if p is not post
            and p.platform is post.platform
            and p.scheduled_for is not None
            and p.state in (PostState.SCHEDULED, PostState.PUBLISHED)
        ]
        return now if not taken else max(now, max(taken) + gap)

    def due(self, posts: list[SocialPost], now: datetime) -> list[SocialPost]:
        return [
            p
            for p in posts
            if p.state is PostState.SCHEDULED
            and p.scheduled_for is not None
            and p.scheduled_for <= now
        ]


@dataclass
class SocialAgent:
    """Drafts posts, routes them through policy, and publishes what is cleared."""

    provider: LLMProvider = field(default_factory=StubProvider)
    policy: AutomationPolicy = field(default_factory=AutomationPolicy)
    scheduler: PostScheduler = field(default_factory=PostScheduler)
    connectors: dict[SocialPlatform, SocialConnector] = field(default_factory=dict)
    posts: list[SocialPost] = field(default_factory=list)
    _counter: int = field(default=0, init=False)

    def draft(self, listing: Listing, platform: SocialPlatform) -> SocialPost:
        """One post for one listing, trimmed to the platform's limits."""
        self._counter += 1
        prompt = (
            f"Product: {listing.title}\n"
            f"Price: {listing.price}\n"
            f"Key points: {'; '.join(listing.bullets) or 'none supplied'}\n"
            f"Platform: {platform.label} (max {platform.body_limit} characters)\n\n"
            "Line 1: the post. Line 2: up to "
            f"{platform.hashtag_limit} hashtags, space separated."
        )
        response = self.provider.complete(prompt, system=_SOCIAL_SYSTEM, temperature=0.6)
        lines = response.as_lines()
        if len(lines) >= 2:
            body, tag_line, source = lines[0], lines[1], self.provider.name
        else:
            body, tag_line, source = self._fallback(listing), "", "fallback"

        hashtags = tuple(
            t.lstrip("#") for t in tag_line.split() if t.strip("#")
        )[: platform.hashtag_limit]
        post = SocialPost(
            id=f"POST-{self._counter:04d}",
            platform=platform,
            body=body[: platform.body_limit],
            hashtags=hashtags,
            sku=listing.sku,
            source=source,
        )
        if not post.fits():
            post.hashtags = ()
            post.body = post.body[: platform.body_limit - 3].rstrip() + "..."
        self.posts.append(post)
        return post

    def _fallback(self, listing: Listing) -> str:
        angle = listing.bullets[0] if listing.bullets else "New in stock"
        return f"{angle}. {listing.title} is {listing.price} in the shop."

    def submit(self, post: SocialPost) -> PolicyDecision:
        """Ask policy whether this post may go out unattended."""
        issues = screen_claims(post.rendered)
        if issues:
            from .policy import block

            post.state = PostState.REJECTED
            return block(
                "social_claims",
                "; ".join(i.describe() for i in issues),
            )
        decision = self.policy.evaluate_social_post(post.platform.label)
        post.state = (
            PostState.SCHEDULED if decision.is_allowed else PostState.AWAITING_APPROVAL
        )
        return decision

    def approve(self, post: SocialPost, at: datetime | None = None) -> SocialPost:
        """Owner cleared it at check-in; it becomes schedulable.

        The slot is assigned *now*, not back when the post was drafted. A post
        that waited two days for approval would otherwise carry a time already
        in the past, and a batch cleared together would all be due at once --
        exactly the burst the minimum gap exists to prevent.
        """
        if post.state is not PostState.AWAITING_APPROVAL:
            raise ValueError(f"{post.id} is {post.state.value}, not awaiting approval")
        post.state = PostState.SCHEDULED
        post.scheduled_for = self.scheduler.next_slot(
            self.posts, post, at or datetime.now()
        )
        return post

    def campaign_for(
        self, listing: Listing, platforms: tuple[SocialPlatform, ...], start: datetime
    ) -> list[SocialPost]:
        """Draft one post per platform and space out the ones cleared to go.

        Posts still awaiting the owner are deliberately left unscheduled. They
        get their slot in :meth:`approve`, timed from the approval rather than
        from a ``start`` that may be long past by then.
        """
        drafted = [self.draft(listing, p) for p in platforms]
        for post in drafted:
            self.submit(post)
        self.scheduler.schedule(
            [p for p in drafted if p.state is PostState.SCHEDULED], start
        )
        return drafted

    def publish_due(self, now: datetime) -> list[SocialPost]:
        """Publish everything scheduled and due. Returns what went out."""
        published: list[SocialPost] = []
        for post in self.scheduler.due(self.posts, now):
            connector = self.connectors.get(post.platform)
            if connector is None:
                continue
            connector.publish(post)
            published.append(post)
        return published

    def awaiting_approval(self) -> list[SocialPost]:
        return [p for p in self.posts if p.state is PostState.AWAITING_APPROVAL]
