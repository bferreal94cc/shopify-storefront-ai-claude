# shopify-storefront-ai-claude

`storefront-agent` — multi-storefront automation: product research, cross-channel
listing, order-to-ship fulfilment, batched owner approvals, marketing and social.

One start prompt turns into a resumable seven-stage run. You stay in the loop
through a twice-daily check-in digest rather than a stream of interruptions.

```bash
pip install -e '.[dev]'

storefront-agent --demo                      # offline end-to-end walkthrough
storefront-agent "start selling desk lamps on shopify"
storefront-agent --check-in                  # what needs you right now
storefront-agent --host 127.0.0.1 --port 8000   # owner console
```

## Safety posture

**Shopify writes are rehearsed by default.** Every mutation goes through
`ShopifyConnector.mutate`, where `dry_run` defaults to `True`: the document and
variables are recorded, a synthetic id comes back, and nothing leaves the
process. Reads always execute for real. Turning writes on is an explicit
constructor argument — no environment variable can silently promote a rehearsal
into a run that changes a live store.

The autonomy dial in `policy.py` decides what proceeds alone and what waits for
you (`conservative | balanced | aggressive`). `audit.py` keeps an append-only,
tamper-evident trail of everything the orchestrator did.

## Layout

| Module | Role |
|---|---|
| `orchestrator.py` | Owns the engines, routes requests, audits everything |
| `sequence.py` | One start prompt, seven stages, resumable across days |
| `domain.py` | Core vocabulary: money, products, listings, orders, POs |
| `policy.py` | The autonomy dial |
| `approvals.py` | Approval queue and twice-daily check-in digest |
| `nlu.py` | Turns the owner's sentence into a run configuration |
| `research.py` → `catalog.py` | Find things worth selling; turn a candidate into per-channel listings |
| `orders.py` → `risk.py` → `fulfillment.py` | Audited order pipeline, fraud scoring, cleared order to tracked parcel |
| `marketing.py` / `social.py` | Ad creative and budgeting; approval-gated posting |
| `channels.py` | One port, three marketplaces |
| `shopify.py` / `shopify_gql.py` | Live Shopify adapter over the GraphQL Admin API |
| `security.py` | Secrets, webhook authenticity, idempotency, redaction |
| `dashboard.py` | Owner console — a local web app with no dependencies |
| `providers.py` | Provider-agnostic LLM access |
| `scenarios.py` | Seed data exercising every branch |

## Channels

`ChannelConnector` (ABC) is the port. `InMemoryChannel` backs the hermetic
tests. Shopify is the one live adapter. Amazon and eBay are deliberately
**unwired** — `_UnwiredConnector` raises an honest `NotImplementedError` rather
than pretending to publish. Wiring a marketplace is a new subclass and nothing
else.

## Tests

502 tests, hermetic, no network and no live store required:

```bash
python -m pytest -q
```

## Provenance

Extracted from `bferreal94cc/playwright-mcp`, branch
`claude/multi-storefront-automation-ej9a2e` (commit `3d10b2b`), transferred here
intact — all 45 files, no pruning. Zero runtime dependencies; `pytest` for dev,
optional `google-adk` / `anthropic` extras for LLM providers.
