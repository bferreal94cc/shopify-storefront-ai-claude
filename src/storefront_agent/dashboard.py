"""The owner-facing console: a local web app with no dependencies.

Built on :mod:`http.server` from the standard library, so running the UI needs
no npm, no framework and no build step -- one command and a browser. The design
target is a phone at 9am, because that is where a twice-daily check-in actually
happens: single column, large tap targets, and the queue ordered by what costs
most to get wrong.

Three security decisions, all deliberate:

* **Bound to 127.0.0.1 by default.** This console can spend money. It is not
  something to expose on a network interface by accident, so binding elsewhere
  is an explicit argument and :func:`serve` warns when it is used.
* **Session token on every request.** A token is minted at startup and compared
  in constant time. Without it, any page in the browser could POST an approval
  to localhost.
* **Approvals are POSTs, never GETs.** A GET that spends money can be triggered
  by an image tag.
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .approvals import ApprovalItem, CheckInDigest, Decision
from .domain import ListingState, OrderState
from .orchestrator import Orchestrator
from .security import constant_time_equals, session_token

_STYLE = """
:root{--bg:#f6f7f9;--card:#fff;--ink:#14181f;--muted:#5b6472;--line:#e2e6ec;
--accent:#2563eb;--ok:#0f7b4f;--warn:#a15c00;--bad:#b42318;--chip:#eef2f7}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#0d1117;--card:#161b22;
--ink:#e6edf3;--muted:#9198a1;--line:#30363d;--accent:#4c8dff;--ok:#3fb950;--warn:#d29922;
--bad:#f85149;--chip:#21262d}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:16px}
header{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;
margin-bottom:4px}
h1{font-size:20px;margin:0}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);
margin:24px 0 8px}
.sub{color:var(--muted);font-size:14px;margin:0 0 8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;
margin-bottom:10px}
.row{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}
.amt{font-variant-numeric:tabular-nums;font-weight:640;white-space:nowrap}
.why{color:var(--muted);font-size:14px;margin-top:6px}
.chip{display:inline-block;background:var(--chip);color:var(--muted);border-radius:999px;
padding:2px 9px;font-size:12px;margin-right:6px}
.chip.od{background:var(--bad);color:#fff}
.acts{display:flex;gap:8px;margin-top:12px}
button{flex:1;padding:12px;border-radius:9px;border:1px solid var(--line);background:var(--card);
color:var(--ink);font-size:15px;font-weight:560;cursor:pointer;min-height:46px}
button.yes{background:var(--accent);border-color:var(--accent);color:#fff}
button.no:hover{border-color:var(--bad);color:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:left;color:var(--muted);font-weight:560;font-size:12px;text-transform:uppercase;
letter-spacing:.05em;padding:6px 8px;border-bottom:1px solid var(--line)}
td{padding:8px;border-bottom:1px solid var(--line);vertical-align:top}
td.n{text-align:right;font-variant-numeric:tabular-nums}
.scroll{overflow-x:auto}
.empty{text-align:center;padding:28px 14px;color:var(--muted)}
.note{font-size:14px;color:var(--muted);white-space:pre-wrap;margin:2px 0}
footer{color:var(--muted);font-size:12px;margin:24px 0 8px;text-align:center}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 0}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 12px;
flex:1;min-width:104px}
.stat b{display:block;font-size:19px;font-variant-numeric:tabular-nums}
.stat span{font-size:12px;color:var(--muted)}
"""


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def render_page(agent: Orchestrator, digest: CheckInDigest, token: str) -> str:
    """The whole console as one self-contained HTML document."""
    ctx = agent.context
    live = sum(1 for l in ctx.listings if l.state is ListingState.PUBLISHED)
    open_orders = [o for o in ctx.orders if not o.state.is_terminal]
    parts: list[str] = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>Storefront console</title>",
        f"<style>{_STYLE}</style></head><body><div class='wrap'>",
        "<header><h1>Storefront console</h1>",
        f"<span class='chip'>{_esc(digest.generated_at.strftime('%a %d %b, %H:%M'))}</span>"
        "</header>",
        f"<p class='sub'>{_esc(digest.summary())}</p>",
        "<div class='bar'>",
        f"<div class='stat'><b>{live}</b><span>live listings</span></div>",
        f"<div class='stat'><b>{len(open_orders)}</b><span>open orders</span></div>",
        f"<div class='stat'><b>{len(digest.items)}</b><span>need you</span></div>",
        f"<div class='stat'><b>{_esc(agent.fulfilment.committed_spend())}</b>"
        "<span>committed</span></div>",
        "</div>",
    ]

    if digest.is_empty:
        parts.append(
            "<div class='card empty'>Nothing needs you right now.<br>"
            "Everything else is running inside policy.</div>"
        )
    else:
        parts.append("<h2>Waiting on you</h2>")
        parts.append(
            f"<form method='post' action='/approve-all?token={_esc(token)}'>"
            "<button class='yes' type='submit'>Approve all "
            f"{len(digest.items)} items</button></form>"
        )
        for kind, items in sorted(
            digest.grouped().items(), key=lambda kv: kv[0].urgency_rank
        ):
            label = kind.value.replace("_", " ")
            parts.append(f"<h2>{_esc(label)} ({len(items)})</h2>")
            parts.extend(_render_item(item, token, item in digest.overdue) for item in items)

    parts.append(_render_orders(agent))
    parts.append(_render_listings(agent))
    if digest.notes:
        parts.append("<h2>Notes</h2><div class='card'>")
        parts.extend(f"<p class='note'>{_esc(n)}</p>" for n in digest.notes)
        parts.append("</div>")
    parts.append(_render_audit(agent))
    parts.append(
        "<footer>Local console, bound to this machine. "
        "Every action here is written to the audit log.</footer>"
    )
    parts.append("</div></body></html>")
    return "".join(parts)


def _render_item(item: ApprovalItem, token: str, overdue: bool) -> str:
    amount = f"<span class='amt'>{_esc(item.amount)}</span>" if item.amount else ""
    flag = "<span class='chip od'>overdue</span>" if overdue else ""
    why = f"<p class='why'>{_esc(item.decision_reason)}</p>" if item.decision_reason else ""
    detail = f"<p class='why'>{_esc(item.detail)}</p>" if item.detail else ""
    return (
        "<div class='card'>"
        f"<div class='row'><div><span class='chip'>{_esc(item.id)}</span>{flag}"
        f"<div>{_esc(item.summary)}</div></div>{amount}</div>"
        f"{why}{detail}"
        f"<form class='acts' method='post' action='/decide?token={_esc(token)}'>"
        f"<input type='hidden' name='item' value='{_esc(item.id)}'>"
        "<button class='yes' name='decision' value='approve' type='submit'>Approve</button>"
        "<button class='no' name='decision' value='reject' type='submit'>Reject</button>"
        "</form></div>"
    )


def _render_orders(agent: Orchestrator) -> str:
    orders = [o for o in agent.context.orders if not o.state.is_terminal]
    if not orders:
        return ""
    rows = "".join(
        "<tr>"
        f"<td>{_esc(o.channel.label)}<br><span class='why'>{_esc(o.external_id)}</span></td>"
        f"<td>{_esc(o.state.value.replace('_', ' '))}"
        + (f"<br><span class='why'>{_esc(o.hold_reason[:64])}</span>" if o.hold_reason else "")
        + f"</td><td class='n'>{_esc(o.units)}</td>"
        f"<td class='n'>{_esc(o.revenue)}</td></tr>"
        for o in sorted(orders, key=lambda o: (o.state.value, o.external_id))[:20]
    )
    return (
        f"<h2>Order pipeline ({len(orders)} open)</h2><div class='card scroll'><table>"
        "<tr><th>Channel</th><th>State</th><th>Units</th><th>Revenue</th></tr>"
        f"{rows}</table></div>"
    )


def _render_listings(agent: Orchestrator) -> str:
    listings = [l for l in agent.context.listings if l.state is ListingState.PUBLISHED]
    if not listings:
        return ""
    rows = "".join(
        f"<tr><td>{_esc(l.title[:44])}<br><span class='why'>{_esc(l.channel.label)} · "
        f"{_esc(l.sku)}</span></td><td class='n'>{_esc(l.price)}</td>"
        f"<td class='n'>{l.margin_rate:.0%}</td><td class='n'>{_esc(l.quantity)}</td></tr>"
        for l in listings[:20]
    )
    return (
        f"<h2>Live listings ({len(listings)})</h2><div class='card scroll'><table>"
        "<tr><th>Product</th><th>Price</th><th>Margin</th><th>Stock</th></tr>"
        f"{rows}</table></div>"
    )


def _render_audit(agent: Orchestrator) -> str:
    entries = agent.audit.entries[-8:]
    if not entries:
        return ""
    verified = agent.audit.verify()
    state = "chain verified" if verified.valid else f"CHAIN BROKEN: {verified.detail}"
    rows = "".join(
        f"<tr><td>{_esc(e.timestamp.strftime('%H:%M'))}</td>"
        f"<td>{_esc(e.actor)}</td><td>{_esc(e.action)}<br>"
        f"<span class='why'>{_esc(e.reasoning[:72])}</span></td></tr>"
        for e in reversed(entries)
    )
    return (
        f"<h2>Recent activity — {_esc(state)}</h2><div class='card scroll'><table>"
        f"<tr><th>Time</th><th>Actor</th><th>Action</th></tr>{rows}</table></div>"
    )


class ConsoleHandler(BaseHTTPRequestHandler):
    """Request handler. ``agent`` and ``token`` are set on the server."""

    server_version = "storefront-console"

    # -- plumbing ----------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:  # pragma: no cover - quiet by default
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _authorised(self, query: dict[str, list[str]]) -> bool:
        supplied = (query.get("token") or [""])[0]
        return constant_time_equals(supplied, self.server.token)

    def _send(self, status: int, body: str, content_type: str = "text/html; charset=utf-8") -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'"
        )
        self.end_headers()
        self.wfile.write(payload)

    def _redirect_home(self) -> None:
        self.send_response(303)
        self.send_header("Location", f"/?token={self.server.token}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- routes ------------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/health":
            self._send(200, json.dumps({"ok": True}), "application/json")
            return
        if not self._authorised(query):
            self._send(403, "<h1>403</h1><p>Session token required.</p>")
            return
        if parsed.path != "/":
            self._send(404, "<h1>404</h1>")
            return
        agent = self.server.agent
        digest = agent.check_in(at=datetime.now())
        self._send(200, render_page(agent, digest, self.server.token))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if not self._authorised(query):
            self._send(403, "<h1>403</h1><p>Session token required.</p>")
            return
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode("utf-8")) if length else {}
        agent = self.server.agent
        now = datetime.now()

        if parsed.path == "/approve-all":
            agent.approve_all(at=now)
            self._redirect_home()
            return
        if parsed.path == "/decide":
            item_id = (form.get("item") or [""])[0]
            raw = (form.get("decision") or ["approve"])[0]
            decision = Decision.APPROVE if raw == "approve" else Decision.REJECT
            try:
                agent.approvals.decide(item_id, decision, agent.approvals.owner, "decided in console", at=now)
                agent.act_on(item_id, now)
            except (KeyError, ValueError, PermissionError) as exc:
                self._send(400, f"<h1>400</h1><p>{_esc(exc)}</p>")
                return
            self._redirect_home()
            return
        self._send(404, "<h1>404</h1>")


def build_server(
    agent: Orchestrator, host: str = "127.0.0.1", port: int = 8787, token: str | None = None
) -> ThreadingHTTPServer:
    """A configured console server. Caller owns ``serve_forever``."""
    server = ThreadingHTTPServer((host, port), ConsoleHandler)
    server.agent = agent
    server.token = token or session_token()
    server.verbose = False
    return server


def serve(
    agent: Orchestrator, host: str = "127.0.0.1", port: int = 8787, token: str | None = None
) -> None:  # pragma: no cover - blocking
    """Run the console until interrupted."""
    server = build_server(agent, host, port, token)
    url = f"http://{host}:{port}/?token={server.token}"
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"WARNING: binding to {host} exposes a console that can spend money "
            "beyond this machine. Use 127.0.0.1 unless you know why you are not."
        )
    print(f"Console ready: {url}\nPress Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
