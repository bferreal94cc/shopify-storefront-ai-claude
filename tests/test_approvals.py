"""The queue: what reaches the owner, what never does, and what waits."""

from __future__ import annotations

from datetime import timedelta

import pytest

from conftest import NOW, dollars
from storefront_agent.approvals import (
    ApprovalKind,
    ApprovalQueue,
    Decision,
    ItemState,
    TimeoutPolicy,
)
from storefront_agent.policy import allow, block, needs_approval


@pytest.fixture
def queue():
    return ApprovalQueue(delegates=("va-1",))


class TestRouting:
    def test_an_allowed_action_never_reaches_the_owner(self, queue):
        item = queue.route(
            allow("po_within_limits", "inside the cap"),
            ApprovalKind.PURCHASE_ORDER, "PO-1", amount=dollars(120), at=NOW,
        )
        assert item is None
        assert queue.auto_approved == 1
        assert queue.auto_approved_value == dollars(120)
        assert queue.pending() == []

    def test_a_blocked_action_is_not_offered_as_a_choice(self, queue):
        item = queue.route(
            block("amazon.seller_of_record", "supplier will not ship blind"),
            ApprovalKind.LISTING, "Lamp on Amazon", at=NOW,
        )
        assert item is None
        assert queue.pending() == []
        assert any(e.action == "blocked" for e in queue.audit.entries)

    def test_an_escalated_action_is_queued_with_its_reason(self, queue):
        item = queue.route(
            needs_approval("po_auto_approve_cap", "$420 is over the $150 limit"),
            ApprovalKind.PURCHASE_ORDER, "PO-3", amount=dollars(420), subject_id="PO-3", at=NOW,
        )
        assert item is not None
        assert item.decision_reason == "$420 is over the $150 limit"
        assert item.subject_id == "PO-3"
        assert queue.pending() == [item]

    def test_every_route_is_audited(self, queue):
        queue.route(allow("r", "why"), ApprovalKind.LISTING, "x", at=NOW)
        queue.route(needs_approval("r", "why"), ApprovalKind.LISTING, "y", at=NOW)
        queue.route(block("r", "why"), ApprovalKind.LISTING, "z", at=NOW)
        assert len(queue.audit.entries) == 3
        assert queue.audit.verify().valid


class TestOrdering:
    def test_money_and_stuck_customers_sort_above_marketing(self, queue):
        queue.route(needs_approval("r", "w"), ApprovalKind.SOCIAL_POST, "post", at=NOW)
        queue.route(needs_approval("r", "w"), ApprovalKind.PURCHASE_ORDER, "po",
                    amount=dollars(100), at=NOW)
        queue.route(needs_approval("r", "w"), ApprovalKind.HELD_ORDER, "held", at=NOW)
        assert [i.kind for i in queue.pending()] == [
            ApprovalKind.PURCHASE_ORDER, ApprovalKind.HELD_ORDER, ApprovalKind.SOCIAL_POST
        ]

    def test_within_a_kind_the_largest_amount_sorts_first(self, queue):
        for amount in (50, 900, 300):
            queue.route(needs_approval("r", "w"), ApprovalKind.PURCHASE_ORDER,
                        f"po-{amount}", amount=dollars(amount), at=NOW)
        assert [i.amount.dollars for i in queue.pending()] == [900.0, 300.0, 50.0]


class TestDecisions:
    def _queued(self, queue):
        return queue.route(
            needs_approval("po_auto_approve_cap", "over the limit"),
            ApprovalKind.PURCHASE_ORDER, "PO-3", amount=dollars(420), at=NOW,
        )

    def test_approving_records_who_and_when(self, queue):
        item = self._queued(queue)
        queue.decide(item.id, Decision.APPROVE, "owner", "supplier is reliable", at=NOW)
        assert item.state is ItemState.APPROVED
        assert item.decided_by == "owner" and item.note == "supplier is reliable"

    def test_rejecting_is_recorded_too(self, queue):
        item = self._queued(queue)
        queue.decide(item.id, Decision.REJECT, "owner", "too expensive", at=NOW)
        assert item.state is ItemState.REJECTED

    def test_deciding_twice_raises(self, queue):
        item = self._queued(queue)
        queue.decide(item.id, Decision.APPROVE, "owner", at=NOW)
        with pytest.raises(ValueError, match="already approved"):
            queue.decide(item.id, Decision.APPROVE, "owner", at=NOW)

    def test_a_stranger_cannot_decide(self, queue):
        item = self._queued(queue)
        with pytest.raises(PermissionError, match="not the owner or a delegate"):
            queue.decide(item.id, Decision.APPROVE, "stranger", at=NOW)

    def test_a_delegate_can_decide(self, queue):
        item = self._queued(queue)
        queue.decide(item.id, Decision.APPROVE, "va-1", at=NOW)
        assert item.decided_by == "va-1"

    def test_an_unknown_item_raises(self, queue):
        with pytest.raises(KeyError):
            queue.decide("NOPE-1", Decision.APPROVE, "owner")


class TestTimeouts:
    def test_spending_never_auto_approves_on_a_timeout(self, queue):
        item = queue.route(
            needs_approval("po_auto_approve_cap", "over"),
            ApprovalKind.PURCHASE_ORDER, "PO-3", amount=dollars(420), at=NOW,
        )
        queue.tick(NOW + timedelta(days=30))
        assert item.state is ItemState.PENDING
        assert item.on_timeout is TimeoutPolicy.HOLD

    def test_a_stale_social_draft_expires(self, queue):
        item = queue.route(
            needs_approval("social_requires_approval", "queued"),
            ApprovalKind.SOCIAL_POST, "a post", at=NOW,
        )
        queue.tick(NOW + timedelta(hours=20))
        assert item.state is ItemState.EXPIRED

    def test_a_held_order_escalates_to_a_delegate(self, queue):
        item = queue.route(
            needs_approval("order_held", "fraud signals"),
            ApprovalKind.HELD_ORDER, "order #1", at=NOW,
        )
        queue.tick(NOW + timedelta(hours=20))
        assert item.state is ItemState.ESCALATED
        assert item.escalate_to == "va-1"

    def test_escalation_happens_once_not_on_every_tick(self, queue):
        queue.route(needs_approval("r", "w"), ApprovalKind.HELD_ORDER, "order", at=NOW)
        queue.tick(NOW + timedelta(hours=20))
        assert queue.tick(NOW + timedelta(hours=21)) == []

    def test_nothing_expires_before_its_deadline(self, queue):
        item = queue.route(needs_approval("r", "w"), ApprovalKind.SOCIAL_POST, "post", at=NOW)
        assert queue.tick(NOW + timedelta(hours=1)) == []
        assert item.state is ItemState.PENDING

    def test_an_escalated_item_is_still_open_and_decidable(self, queue):
        item = queue.route(needs_approval("r", "w"), ApprovalKind.HELD_ORDER, "o", at=NOW)
        queue.tick(NOW + timedelta(hours=20))
        queue.decide(item.id, Decision.APPROVE, "va-1", at=NOW + timedelta(hours=21))
        assert item.state is ItemState.APPROVED


class TestDigest:
    def test_an_empty_digest_says_so_plainly(self, queue):
        digest = queue.digest(at=NOW)
        assert digest.is_empty
        assert "Nothing needs you" in digest.summary()

    def test_the_digest_totals_what_approving_everything_would_cost(self, queue):
        for amount in (120, 300):
            queue.route(needs_approval("r", "w"), ApprovalKind.PURCHASE_ORDER,
                        "po", amount=dollars(amount), at=NOW)
        assert queue.digest(at=NOW).pending_spend() == dollars(420)

    def test_auto_approved_work_is_reported_but_not_listed(self, queue):
        queue.route(allow("r", "w"), ApprovalKind.PURCHASE_ORDER, "po",
                    amount=dollars(50), at=NOW)
        digest = queue.digest(at=NOW)
        assert digest.is_empty and digest.auto_approved == 1
        assert "1 action(s) auto-approved" in digest.summary()

    def test_items_are_grouped_by_kind(self, queue):
        queue.route(needs_approval("r", "w"), ApprovalKind.PURCHASE_ORDER, "po", at=NOW)
        queue.route(needs_approval("r", "w"), ApprovalKind.SOCIAL_POST, "post", at=NOW)
        grouped = queue.digest(at=NOW).grouped()
        assert set(grouped) == {ApprovalKind.PURCHASE_ORDER, ApprovalKind.SOCIAL_POST}

    def test_overdue_items_are_marked(self, queue):
        queue.route(needs_approval("r", "w"), ApprovalKind.PURCHASE_ORDER,
                    "po", amount=dollars(400), at=NOW)
        digest = queue.digest(at=NOW + timedelta(days=2))
        assert len(digest.overdue) == 1
        assert "overdue" in digest.summary()

    def test_lines_explain_why_each_item_needs_the_owner(self, queue):
        queue.route(needs_approval("po_auto_approve_cap", "$420 is over the $150 limit"),
                    ApprovalKind.PURCHASE_ORDER, "PO-3", amount=dollars(420), at=NOW)
        text = "\n".join(queue.digest(at=NOW).lines())
        assert "why you: $420 is over the $150 limit" in text

    def test_notes_are_appended(self, queue):
        digest = queue.digest(at=NOW, notes=("2 orders are late.",))
        assert "2 orders are late." in "\n".join(digest.lines())

    def test_taking_a_digest_records_the_check_in(self, queue):
        queue.digest(at=NOW)
        assert queue.last_check_in == NOW
        assert any(e.action == "check_in" for e in queue.audit.entries)

    def test_decided_items_drop_out_of_the_next_digest(self, queue):
        item = queue.route(needs_approval("r", "w"), ApprovalKind.PURCHASE_ORDER,
                           "po", amount=dollars(400), at=NOW)
        queue.decide(item.id, Decision.APPROVE, "owner", at=NOW)
        assert queue.digest(at=NOW).is_empty
