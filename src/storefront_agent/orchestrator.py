"""The orchestrator: owns the engines, routes requests, audits everything.

Callers talk to this class, not to the engines underneath. It holds the live
context, decides which specialist handles an incoming request, and makes sure
every consequential action reaches the audit log.

The specialists map onto modules as follows:

=========================  ====================================================
Research                   :class:`~storefront_agent.research.ProductResearchAgent`
Listing                    :class:`~storefront_agent.catalog.CatalogBuilder`
Storefronts                :class:`~storefront_agent.channels.ChannelConnector`
Orders                     :class:`~storefront_agent.orders.OrderPipeline`
Fulfilment                 :class:`~storefront_agent.fulfillment.FulfillmentEngine`
Approvals                  :class:`~storefront_agent.approvals.ApprovalQueue`
Marketing                  :class:`~storefront_agent.marketing.MarketingAgent`
Social                     :class:`~storefront_agent.social.SocialAgent`
Policy                     :class:`~storefront_agent.policy.AutomationPolicy`
=========================  ====================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .approvals import ApprovalKind, ApprovalQueue, CheckInDigest, Decision
from .audit import AuditLog
from .catalog import CatalogBuilder
from .channels import ChannelConnector, ChannelError, InMemoryChannel
from .domain import (
    Channel,
    Listing,
    ListingState,
    Money,
    Shipment,
    Storefront,
    StorefrontContext,
)
from .fulfillment import FulfillmentEngine
from .marketing import AdPlatform, MarketingAgent
from .nlu import Intent, NLUPipeline, RunConfig
from .orders import OrderPipeline
from .policy import AutomationPolicy
from .providers import LLMProvider, StubProvider
from .research import CandidateScore, ProductResearchAgent, ProductSource, SeedCatalogSource
from .social import SocialAgent, SocialPlatform


@dataclass
class AgentResult:
    """Uniform envelope for whatever a specialist did."""

    agent: str
    action: str
    ok: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        marker = "ok" if self.ok else "!!"
        return f"[{marker}] {self.agent}/{self.action}: {self.summary}"


class Orchestrator:
    """Routes work to specialists and holds the live state of the business."""

    def __init__(
        self,
        context: StorefrontContext | None = None,
        policy: AutomationPolicy | None = None,
        provider: LLMProvider | None = None,
        sources: list[ProductSource] | None = None,
        connectors: dict[Channel, ChannelConnector] | None = None,
        audit_log: AuditLog | None = None,
        owner: str = "owner",
    ) -> None:
        self.context = context or StorefrontContext()
        self.policy = policy or AutomationPolicy()
        self.provider = provider or StubProvider()
        self.audit = audit_log or AuditLog()

        self.research_agent = ProductResearchAgent(sources=sources or [SeedCatalogSource()])
        self.catalog = CatalogBuilder(policy=self.policy)
        self.pipeline = OrderPipeline(self.context, self.policy, self.audit)
        self.connectors = connectors or {}
        self.fulfilment = FulfillmentEngine(
            self.context, self.pipeline, self.policy, self.connectors
        )
        self.approvals = ApprovalQueue(audit=self.audit, owner=owner)
        self.marketing = MarketingAgent(provider=self.provider, policy=self.policy)
        self.social = SocialAgent(provider=self.provider, policy=self.policy)
        self.nlu = NLUPipeline(provider=self.provider)

        self.shortlist: list[CandidateScore] = []
        self.pending_tracking: dict[str, Shipment] = {}
        self._last_poll: datetime | None = None

    # -- configuration -----------------------------------------------------

    def apply_config(self, config: RunConfig) -> None:
        """Fold a run configuration into policy and ensure storefronts exist."""
        config.to_policy(self.policy)
        self.catalog.policy = self.policy
        for channel in config.channels:
            if self.context.storefront_for(channel) is None:
                self.context.storefronts.append(Storefront.for_channel(channel))
            self.connectors.setdefault(channel, InMemoryChannel(channel=channel))
        self.fulfilment.connectors = self.connectors
        self.audit.record(
            actor="orchestrator",
            action="config_applied",
            subject="policy",
            reasoning=self.policy.summary(),
            metadata={"channels": [c.value for c in config.channels]},
        )

    # -- natural language --------------------------------------------------

    def handle(self, text: str, at: datetime | None = None) -> AgentResult:
        """Interpret an instruction and dispatch it."""
        parsed = self.nlu.understand(text)
        self.audit.record(
            actor=self.approvals.owner,
            action="request_received",
            subject=parsed.intent.value,
            reasoning=text,
            metadata={"confidence": parsed.confidence},
            at=at,
        )
        if parsed.intent is Intent.CHECK_IN:
            digest = self.check_in(at=at)
            return AgentResult(
                "approvals", "check_in", True, digest.summary(), {"digest": digest}
            )
        if parsed.intent is Intent.STATUS:
            return self.status()
        if parsed.intent is Intent.APPROVE_ALL:
            return self.approve_all(at=at)
        if parsed.intent is Intent.RESEARCH:
            scored = self.research(parsed.config.niche or text)
            return AgentResult(
                "research", "research", bool(scored),
                f"{len(scored)} candidate(s) for '{parsed.config.niche or text}'",
                {"scored": scored},
            )
        if parsed.intent is Intent.PAUSE:
            return AgentResult(
                "orchestrator", "pause", True,
                "Nothing new will be listed or purchased until you start a run again.",
            )
        return AgentResult(
            "orchestrator", "clarify", False,
            "Could not tell what was being asked. Try 'check in' or "
            "'start selling <niche> on shopify'.",
            {"confidence": parsed.confidence},
        )

    # -- stage bodies ------------------------------------------------------

    def research(self, niche: str, top_n: int = 10) -> list[CandidateScore]:
        """Source and rank candidates, remembering the shortlist."""
        scored = self.research_agent.research(niche, top_n=top_n)
        self.shortlist = scored
        self.audit.record(
            actor="research",
            action="candidates_scored",
            subject=niche,
            reasoning=f"{len(scored)} candidate(s) from {len(self.research_agent.sources)} source(s)",
            metadata={"count": len(scored)},
        )
        return scored

    def select(self, config: RunConfig, at: datetime | None = None) -> dict[str, Any]:
        """Filter the shortlist through policy and gate on the owner if needed."""
        eligible: list[CandidateScore] = []
        blocked: list[tuple[CandidateScore, str]] = []
        needs_owner: list[CandidateScore] = []

        for scored in self.shortlist:
            verdicts = [
                self.policy.evaluate_candidate(
                    scored.candidate, channel, scored.projected_price
                )
                for channel in config.channels
            ]
            if any(v.is_allowed for v in verdicts):
                eligible.append(scored)
            elif any(v.needs_owner for v in verdicts):
                needs_owner.append(scored)
                reason = next(v.reason for v in verdicts if v.needs_owner)
                blocked.append((scored, reason))
            else:
                blocked.append((scored, verdicts[0].reason if verdicts else "no channel"))

        self.shortlist = eligible + needs_owner
        item = None
        if needs_owner:
            from .policy import needs_approval

            item = self.approvals.route(
                needs_approval(
                    "shortlist_review",
                    f"{len(needs_owner)} candidate(s) are outside policy on every channel",
                ),
                ApprovalKind.SHORTLIST,
                summary=f"{len(needs_owner)} borderline candidate(s) in '{config.niche}'",
                detail="; ".join(s.candidate.product.title for s in needs_owner[:5]),
                at=at,
            )
        summary = (
            f"{len(eligible)} candidate(s) clear policy, {len(needs_owner)} need you, "
            f"{len(blocked) - len(needs_owner)} blocked outright"
        )
        return {
            "summary": summary,
            "item": item,
            "eligible": len(eligible),
            "blocked": len(blocked),
        }

    def publish_shortlist(
        self, config: RunConfig, at: datetime | None = None
    ) -> tuple[list[Listing], list[Listing]]:
        """Build and publish a listing per candidate per channel."""
        published: list[Listing] = []
        blocked: list[Listing] = []
        storefronts = [
            sf for sf in self.context.storefronts if sf.channel in config.channels
        ]
        for scored in self.shortlist[: self.policy.max_new_listings_per_run]:
            for listing in self.catalog.build_all(scored.candidate, storefronts):
                self.context.listings.append(listing)
                if listing.state is ListingState.BLOCKED:
                    blocked.append(listing)
                    self.audit.record(
                        actor="catalog",
                        action="listing_blocked",
                        subject=listing.sku,
                        reasoning=listing.blocked_reason,
                        metadata={"channel": listing.channel.value},
                        at=at,
                    )
                    continue
                connector = self.connectors.get(listing.channel)
                if connector is None:
                    continue
                try:
                    connector.publish_listing(listing)
                except (ChannelError, NotImplementedError) as exc:
                    listing.state = ListingState.BLOCKED
                    listing.blocked_reason = str(exc)
                    blocked.append(listing)
                    continue
                published.append(listing)
                self.audit.record(
                    actor="catalog",
                    action="listing_published",
                    subject=listing.sku,
                    reasoning=listing.describe(),
                    metadata={"channel": listing.channel.value, "price_cents": listing.price.cents},
                    at=at,
                )
        return published, blocked

    def promote(self, at: datetime | None = None) -> dict[str, Any]:
        """Draft ad campaigns and social posts for what is live."""
        moment = at or datetime.now()
        live = [l for l in self.context.listings if l.state is ListingState.PUBLISHED]
        campaigns = 0
        posts = 0
        gated = False
        duration = 7
        # Size the daily budget so a default campaign lands just inside the cap;
        # anything the owner has tightened below that still escalates.
        daily = self.policy.ad_auto_approve_cap * (0.8 / duration)
        for listing in live[:5]:
            campaign, decision = self.marketing.build_campaign(
                listing, AdPlatform.META, daily, duration
            )
            campaigns += 1
            item = self.approvals.route(
                decision,
                ApprovalKind.AD_SPEND,
                summary=(
                    f"{campaign.platform.label} ads for {listing.channel.label} "
                    f"{listing.title[:32]}"
                ),
                detail=campaign.describe(),
                amount=campaign.total_budget,
                subject_id=campaign.id,
                at=moment,
            )
            gated = gated or item is not None
            for post in self.social.campaign_for(
                listing, (SocialPlatform.INSTAGRAM, SocialPlatform.X), moment
            ):
                posts += 1
                if post.state.value == "awaiting_approval":
                    from .policy import needs_approval

                    item = self.approvals.route(
                        needs_approval(
                            "social_requires_approval",
                            f"queued for {post.platform.label}",
                        ),
                        ApprovalKind.SOCIAL_POST,
                        summary=f"{post.platform.label}: {post.body[:50]}",
                        subject_id=post.id,
                        at=moment,
                    )
                    gated = gated or item is not None
        return {
            "summary": f"{campaigns} campaign(s) and {posts} social post(s) drafted",
            "gated": gated,
            "campaigns": campaigns,
            "posts": posts,
        }

    def operate(self, at: datetime | None = None) -> dict[str, Any]:
        """One turn of the steady-state loop: orders in, parcels out."""
        moment = at or datetime.now()
        pulled = self.poll_orders()
        tally = self.pipeline.intake(pulled)
        drafted = self.fulfilment.draft_purchase_orders(at=moment)

        placed = 0
        gated = False
        for po in drafted:
            decision = self.policy.evaluate_purchase_order(po, moment)
            item = self.approvals.route(
                decision,
                ApprovalKind.PURCHASE_ORDER,
                summary=f"to {po.supplier_id}, covering {len(po.order_ids)} order(s)",
                detail=po.describe(),
                amount=po.total,
                subject_id=po.id,
                at=moment,
            )
            if item is not None:
                gated = True
                continue
            if decision.is_blocked:
                # Park it rather than cancel: the orders behind a capped batch
                # are usually fine, and one of them may just be oversized.
                self.fulfilment.hold_for_review(po, decision.reason)
                gated = True
                continue
            self.fulfilment.approve(po, "policy", decision.reason)
            if self.fulfilment.place(po, at=moment):
                placed += 1

        for order in self.pipeline.held():
            from .policy import needs_approval

            if any(
                i.subject_id == order.id and i.state.is_open for i in self.approvals.items
            ):
                continue
            item = self.approvals.route(
                needs_approval("order_held", order.hold_reason or "held for review"),
                ApprovalKind.HELD_ORDER,
                summary=f"{order.channel.label} {order.external_id} held",
                detail=order.hold_reason,
                subject_id=order.id,
                at=moment,
            )
            gated = gated or item is not None

        shipped = self.flush_tracking()
        return {
            "summary": (
                f"pulled {len(pulled)} order(s), {tally.get('risk_cleared', 0)} cleared, "
                f"{len(drafted)} PO(s) drafted, {placed} placed, {shipped} shipped"
            ),
            "gated": gated,
            "pulled": len(pulled),
            "drafted": len(drafted),
            "placed": placed,
            "shipped": shipped,
        }

    # -- storefront I/O ----------------------------------------------------

    def poll_orders(self) -> list:
        """Pull new orders from every connector that can answer."""
        pulled = []
        for channel, connector in self.connectors.items():
            try:
                pulled.extend(connector.fetch_orders(self._last_poll))
            except (ChannelError, NotImplementedError) as exc:
                self.audit.record(
                    actor="orchestrator",
                    action="poll_failed",
                    subject=channel.value,
                    reasoning=str(exc),
                )
        self._last_poll = datetime.now()
        return pulled

    def receive_tracking(self, order_id: str, shipment: Shipment) -> None:
        """Queue tracking from a supplier for the next operate turn."""
        self.pending_tracking[order_id] = shipment

    def flush_tracking(self) -> int:
        """Apply queued tracking to placed orders."""
        shipped = 0
        for order_id, shipment in list(self.pending_tracking.items()):
            order = self.context.order_by_id(order_id)
            if order is None or order.state.value != "placed":
                continue
            if self.fulfilment.ingest_tracking(order_id, shipment):
                shipped += 1
            self.pending_tracking.pop(order_id, None)
        return shipped

    # -- owner-facing ------------------------------------------------------

    def check_in(self, at: datetime | None = None) -> CheckInDigest:
        """Build the digest, folding in operational warnings."""
        moment = at or datetime.now()
        notes: list[str] = []
        late = self.fulfilment.late_orders(moment)
        if late:
            notes.append(f"{len(late)} order(s) past their promised ship date:")
            notes.extend(f"  {l.describe()}" for l in late[:3])
        remaining = self.policy.remaining_today(moment)
        notes.append(f"Spend today: {self.policy.spent_today(moment)} of {self.policy.daily_spend_cap} ({remaining} left).")
        return self.approvals.digest(at=moment, notes=tuple(notes))

    def approve_all(self, at: datetime | None = None) -> AgentResult:
        """Approve every pending item and act on it.

        Convenience for a check-in where the owner has read the digest and is
        content with all of it. Each item is still decided and audited
        individually, so the trail is identical to clicking them one by one.
        """
        moment = at or datetime.now()
        approved = 0
        for item in list(self.approvals.pending()):
            self.approvals.decide(item.id, Decision.APPROVE, self.approvals.owner, "approved in bulk at check-in", at=moment)
            self.act_on(item.id, moment)
            approved += 1
        return AgentResult(
            "approvals", "approve_all", True, f"approved {approved} item(s)", {"count": approved}
        )

    def act_on(self, item_id: str, at: datetime | None = None) -> AgentResult:
        """Carry out whatever an approved item authorised."""
        item = self.approvals.item(item_id)
        if item is None:
            return AgentResult("approvals", "act_on", False, f"no item {item_id}")
        moment = at or datetime.now()
        if item.state.value == "rejected":
            if item.kind is ApprovalKind.PURCHASE_ORDER:
                po = self.context.po_by_id(item.subject_id)
                if po is not None:
                    self.fulfilment.reject(po, item.decided_by, item.note or "rejected at check-in")
            return AgentResult("approvals", "act_on", True, f"{item_id} rejected and unwound")
        if item.kind is ApprovalKind.PURCHASE_ORDER:
            po = self.context.po_by_id(item.subject_id)
            if po is None:
                return AgentResult("approvals", "act_on", False, f"no purchase order {item.subject_id}")
            self.fulfilment.approve(po, item.decided_by, item.note or "approved at check-in")
            self.fulfilment.place(po, at=moment)
            return AgentResult("fulfilment", "act_on", True, f"placed {po.id} ({po.total})")
        if item.kind is ApprovalKind.HELD_ORDER:
            order = self.context.order_by_id(item.subject_id)
            if order is not None:
                self.pipeline.release(order, item.decided_by, item.note or "released at check-in")
            return AgentResult("orders", "act_on", True, f"released {item.subject_id}")
        if item.kind is ApprovalKind.SOCIAL_POST:
            post = next((p for p in self.social.posts if p.id == item.subject_id), None)
            if post is not None:
                self.social.approve(post, at=moment)
            return AgentResult("social", "act_on", True, f"scheduled {item.subject_id}")
        return AgentResult("approvals", "act_on", True, f"{item_id} approved")

    def status(self) -> AgentResult:
        """A one-line read on the whole business."""
        live = sum(1 for l in self.context.listings if l.state is ListingState.PUBLISHED)
        pending = len(self.approvals.pending())
        return AgentResult(
            "orchestrator",
            "status",
            True,
            (
                f"{live} live listing(s) across {len(self.context.storefronts)} storefront(s), "
                f"{len(self.context.orders)} order(s), {pending} item(s) awaiting you, "
                f"{self.fulfilment.committed_spend()} committed"
            ),
            {"live_listings": live, "pending_approvals": pending},
        )
