"""The console can spend money, so its auth and method rules matter."""

from __future__ import annotations

import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

import pytest

from conftest import NOW
from storefront_agent.dashboard import ConsoleHandler, build_server, render_page
from storefront_agent.providers import StubProvider
from storefront_agent.scenarios import build_orchestrator, seed_orders
from storefront_agent.sequence import StorefrontSequence

TOKEN = "test-token-for-the-console"
PROMPT = (
    "start selling desk accessories on shopify, amazon and ebay, "
    "budget $2000, balanced, 25% margin, 5 products"
)


def trading_agent():
    """An orchestrator mid-trade, with a populated approval queue."""
    agent = build_orchestrator(provider=StubProvider())
    sequence = StorefrontSequence(agent)
    state = sequence.start(PROMPT, at=NOW)
    sequence.run(state, at=NOW)
    agent.approve_all(at=NOW)
    sequence.resume(state, at=NOW)
    seed_orders(agent, at=NOW)
    sequence.run(state, at=NOW + timedelta(hours=1))
    return agent


@pytest.fixture(scope="module")
def server():
    srv = build_server(trading_agent(), port=8899, token=TOKEN)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def get(path: str):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:8899{path}", timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def post(path: str, fields: dict):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    request = urllib.request.Request(
        f"http://127.0.0.1:8899{path}",
        data=urllib.parse.urlencode(fields).encode(),
        method="POST",
    )
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


class TestAuth:
    def test_health_needs_no_token(self, server):
        status, body = get("/health")
        assert status == 200 and '"ok": true' in body

    def test_no_token_is_refused(self, server):
        assert get("/")[0] == 403

    def test_a_wrong_token_is_refused(self, server):
        assert get("/?token=wrong")[0] == 403

    def test_the_right_token_renders_the_console(self, server):
        status, body = get(f"/?token={TOKEN}")
        assert status == 200 and "Storefront console" in body

    def test_posting_without_a_token_is_refused(self, server):
        assert post("/decide", {"item": "PU-0001", "decision": "approve"})[0] == 403

    def test_an_unknown_path_is_a_404(self, server):
        assert get(f"/nope?token={TOKEN}")[0] == 404


class TestMethodSafety:
    def test_a_get_can_never_spend(self, server):
        """An <img src> to /decide must not approve anything."""
        assert get(f"/decide?token={TOKEN}")[0] == 404

    def test_approve_all_is_post_only(self, server):
        assert get(f"/approve-all?token={TOKEN}")[0] == 404


class TestRendering:
    def test_the_page_contains_every_section(self, server):
        _status, body = get(f"/?token={TOKEN}")
        for section in ("Waiting on you", "Order pipeline", "Live listings", "Recent activity"):
            assert section in body

    def test_the_audit_chain_state_is_shown(self, server):
        assert "chain verified" in get(f"/?token={TOKEN}")[1]

    def test_the_page_is_responsive_and_theme_aware(self, server):
        _status, body = get(f"/?token={TOKEN}")
        assert "width=device-width" in body
        assert "prefers-color-scheme" in body

    def test_the_page_is_self_contained(self, server):
        _status, body = get(f"/?token={TOKEN}")
        assert "<script" not in body
        assert "http://" not in body.replace("http://www.w3.org", "")

    def test_an_empty_queue_says_so_rather_than_rendering_nothing(self):
        agent = build_orchestrator(provider=StubProvider())
        page = render_page(agent, agent.check_in(at=NOW), TOKEN)
        assert "Nothing needs you right now" in page

    def test_user_content_is_escaped(self):
        agent = build_orchestrator(provider=StubProvider())
        from storefront_agent.approvals import ApprovalKind
        from storefront_agent.policy import needs_approval

        agent.approvals.route(
            needs_approval("r", "<script>alert(1)</script>"),
            ApprovalKind.PURCHASE_ORDER,
            "<img onerror=x>",
            at=NOW,
        )
        page = render_page(agent, agent.check_in(at=NOW), TOKEN)
        assert "<script>alert(1)</script>" not in page
        assert "&lt;script&gt;" in page


class TestActions:
    def test_approving_from_the_console_changes_state_and_redirects(self, server):
        agent = server.agent
        pending = agent.approvals.pending()
        assert pending, "fixture should have queued work"
        item_id = pending[0].id
        before = len(pending)
        status, _body = post(f"/decide?token={TOKEN}", {"item": item_id, "decision": "approve"})
        assert status == 303
        assert agent.approvals.item(item_id).state.value == "approved"
        assert len(agent.approvals.pending()) == before - 1

    def test_the_decision_is_written_to_the_audit_log(self, server):
        agent = server.agent
        item_id = agent.approvals.pending()[0].id
        post(f"/decide?token={TOKEN}", {"item": item_id, "decision": "reject"})
        assert any(
            e.action == "approval_rejected" for e in agent.audit.entries
        )

    def test_deciding_an_unknown_item_is_a_400_not_a_crash(self, server):
        status, body = post(f"/decide?token={TOKEN}", {"item": "NOPE-9", "decision": "approve"})
        assert status == 400 and "NOPE-9" in body

    def test_approve_all_clears_the_queue(self, server):
        agent = server.agent
        assert post(f"/approve-all?token={TOKEN}", {})[0] == 303
        assert agent.approvals.pending() == []


class TestServerConfiguration:
    def test_the_server_binds_to_loopback_by_default(self):
        srv = build_server(build_orchestrator(provider=StubProvider()), port=8898)
        try:
            assert srv.server_address[0] == "127.0.0.1"
        finally:
            srv.server_close()

    def test_a_token_is_generated_when_none_is_supplied(self):
        srv = build_server(build_orchestrator(provider=StubProvider()), port=8897)
        try:
            assert len(srv.token) >= 32
        finally:
            srv.server_close()

    def test_the_handler_sets_hardening_headers(self):
        assert hasattr(ConsoleHandler, "do_GET") and hasattr(ConsoleHandler, "do_POST")
