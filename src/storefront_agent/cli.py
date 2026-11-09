"""Command line: start a run, take a check-in, serve the console.

    storefront-agent "start selling desk accessories on shopify and ebay, $2000"
    storefront-agent --check-in
    storefront-agent serve
    storefront-agent --demo

Runs persist to ``.runs/`` as JSON, so the second command genuinely resumes the
first -- across processes, terminals and days. That is the whole point of a
system you touch twice a day.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from pathlib import Path

from .approvals import Decision
from .dashboard import serve
from .domain import ListingState
from .orchestrator import Orchestrator
from .policy import AutomationPolicy, AutonomyLevel
from .providers import provider_from_env
from .scenarios import DEMO_START, build_orchestrator, seed_orders, seed_tracking
from .sequence import RunState, StorefrontSequence

RULE = "=" * 74


def _heading(text: str) -> None:
    print(f"\n{RULE}\n{text}\n{RULE}")


def _step(label: str) -> None:
    print(f"\n-- {label}")


# -- argument validation ---------------------------------------------------


def _port(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a whole number")
    if not 1 <= parsed <= 65535:
        raise argparse.ArgumentTypeError(f"port must be 1-65535, got {parsed}")
    if parsed < 1024:
        raise argparse.ArgumentTypeError(
            f"port {parsed} is privileged; pick something above 1023"
        )
    return parsed


def _autonomy(value: str) -> AutonomyLevel:
    try:
        return AutonomyLevel(value.lower())
    except ValueError:
        options = ", ".join(a.value for a in AutonomyLevel)
        raise argparse.ArgumentTypeError(f"{value!r} is not one of: {options}")


# -- run persistence -------------------------------------------------------


def _latest_run(run_dir: Path) -> RunState | None:
    if not run_dir.exists():
        return None
    files = sorted(run_dir.glob("run-*.json"), key=lambda p: p.stat().st_mtime)
    return RunState.load(files[-1]) if files else None


# -- commands --------------------------------------------------------------

def cmd_start(args: argparse.Namespace) -> int:
    """Begin a run from the owner's prompt."""
    agent = build_orchestrator(
        provider=provider_from_env(),
        policy=AutomationPolicy(autonomy=args.autonomy) if args.autonomy else None,
    )
    sequence = StorefrontSequence(agent, run_dir=args.run_dir)
    state = sequence.start(args.prompt)
    if not state.is_running and state.status.value == "failed":
        print(f"Cannot start: {state.blocked_on}")
        print('Try: storefront-agent "start selling desk lamps on shopify, budget $500"')
        return 2

    _heading(f"RUN {state.run_id}")
    print(state.config.describe())
    for result in sequence.run(state):
        print(f"   {result}")
    state.save(args.run_dir)

    digest = agent.check_in()
    _step("What needs you")
    print("\n".join(f"   {line}" for line in digest.lines()))
    print(f"\nRun saved to {args.run_dir / (state.run_id + '.json')}")
    print("Take the check-in with:  storefront-agent --check-in")
    return 0


def cmd_check_in(args: argparse.Namespace) -> int:
    """Show the queue; optionally approve all of it."""
    state = _latest_run(args.run_dir)
    if state is None:
        print(f"No runs found in {args.run_dir}. Start one first.")
        return 2
    # A fresh process has the run's configuration but not its live state; this
    # reports what the persisted run recorded rather than inventing numbers.
    _heading(f"CHECK-IN — {datetime.now():%a %d %b, %H:%M}")
    print(state.describe())
    print(f"\nStage history ({len(state.history)} step(s)):")
    for result in state.history[-8:]:
        print(f"   {result}")
    if state.blocked_on:
        print(f"\nWaiting on: {state.blocked_on}")
        print("Open the console to act on it:  storefront-agent serve")
    else:
        print("\nNothing recorded as blocking. Run the console for live detail.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Launch the console over a demo-seeded business."""
    agent = _demo_agent(args)
    serve(agent, host=args.host, port=args.port)
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """The whole system end to end, offline, on seed data."""
    agent = build_orchestrator(provider=provider_from_env())
    sequence = StorefrontSequence(agent, run_dir=args.run_dir)
    t = DEMO_START

    _heading("1. START PROMPT")
    prompt = (
        "start selling desk accessories on shopify, amazon and ebay, "
        "budget $2000, balanced, 25% margin, 5 products"
    )
    print(f'   "{prompt}"')
    state = sequence.start(prompt, at=t)
    print(f"   -> {state.config.describe()}")

    _heading("2. RESEARCH, SELECT, LIST, PROMOTE")
    for result in sequence.run(state, at=t):
        print(f"   {result}")
    blocked = [l for l in agent.context.listings if l.state is ListingState.BLOCKED]
    for listing in blocked[:4]:
        print(f"   BLOCKED {listing.channel.label} {listing.title[:34]}: {listing.blocked_reason[:78]}")

    _heading("3. FIRST CHECK-IN")
    digest = agent.check_in(at=t)
    print("\n".join(f"   {line}" for line in digest.lines()))

    _step("owner approves everything")
    print(f"   {agent.approve_all(at=t).summary}")
    sequence.resume(state, at=t)

    _heading("4. CUSTOMERS ORDER")
    print(f"   {seed_orders(agent, at=t)} order(s) arrived across the storefronts")
    t2 = t + timedelta(hours=1)
    for result in sequence.run(state, at=t2):
        print(f"   {result}")

    _heading("5. SECOND CHECK-IN")
    digest2 = agent.check_in(at=t2)
    print("\n".join(f"   {line}" for line in digest2.lines()))

    _step("owner clears the queue")
    for item in list(agent.approvals.pending()):
        agent.approvals.decide(item.id, Decision.APPROVE, agent.approvals.owner, "approved", at=t2)
        agent.act_on(item.id, t2)
    print(f"   {len(digest2.items)} item(s) actioned")

    _heading("6. SUPPLIERS SHIP")
    t3 = t2 + timedelta(days=2)
    sequence.resume(state, at=t3)
    print(f"   tracking for {seed_tracking(agent, at=t3)} placed order(s)")
    for result in sequence.run(state, at=t3):
        print(f"   {result}")

    _heading("7. WHERE IT LANDED")
    print(f"   {agent.status().summary}")
    counts: dict[str, int] = {}
    for order in agent.context.orders:
        counts[order.state.value] = counts.get(order.state.value, 0) + 1
    for state_name, count in sorted(counts.items()):
        print(f"   {count:>3} order(s) {state_name.replace('_', ' ')}")

    verification = agent.audit.verify()
    print(f"\n   audit: {len(agent.audit.entries)} entries, chain valid: {verification.valid}")
    path = state.save(args.run_dir)
    print(f"   run state: {path}")

    reloaded = RunState.load(path)
    print(f"   reloaded from disk at stage '{reloaded.stage.value}' with "
          f"{len(reloaded.history)} step(s) -- resumable")

    clean = verification.valid and bool(agent.context.orders)
    print(f"\n   demo result: {'CLEAN' if clean else 'ATTENTION NEEDED'}")
    return 0 if clean else 1


def _demo_agent(args: argparse.Namespace) -> Orchestrator:
    """A seeded business with a populated queue, for the console."""
    agent = build_orchestrator(provider=provider_from_env())
    sequence = StorefrontSequence(agent, run_dir=args.run_dir)
    t = DEMO_START
    state = sequence.start(
        "start selling desk accessories on shopify, amazon and ebay, "
        "budget $2000, balanced, 25% margin, 5 products",
        at=t,
    )
    sequence.run(state, at=t)
    # Clear the launch gate the way an owner would, so the console opens on a
    # business that is actually trading rather than one still waiting to start.
    agent.approve_all(at=t)
    sequence.resume(state, at=t)
    seed_orders(agent, at=t)
    sequence.run(state, at=t + timedelta(hours=1))
    return agent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="storefront-agent",
        description="Multi-storefront automation: research, list, sell, fulfil.",
    )
    parser.add_argument("prompt", nargs="?", help='e.g. "start selling desk lamps on shopify"')
    parser.add_argument("--check-in", action="store_true", help="show what needs you")
    parser.add_argument("--demo", action="store_true", help="offline end-to-end walkthrough")
    parser.add_argument("--autonomy", type=_autonomy, help="conservative | balanced | aggressive")
    parser.add_argument("--run-dir", type=Path, default=Path(".runs"), help="where runs persist")
    parser.add_argument("--host", default="127.0.0.1", help="console bind address")
    parser.add_argument("--port", type=_port, default=8787, help="console port")
    args = parser.parse_args(argv)

    if args.demo:
        return cmd_demo(args)
    if args.check_in:
        return cmd_check_in(args)
    if args.prompt == "serve":
        return cmd_serve(args)
    if args.prompt:
        return cmd_start(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
