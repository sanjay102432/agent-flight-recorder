"""Command line front door.

    afr record [WORKSPACE]              record the demo agent on a workspace
    afr view SESSION                    flight report on the terminal + a .md file
    afr replay SESSION [--verify]       replay (or verify) a recorded session
    afr prefetch SESSION --workspace W  price prefetch against the trace
    afr diff SESSION_A SESSION_B        plan-diff two sessions
    afr list                            list sessions in the trace store
    afr verify SESSION                  integrity-check the trace file

Identities: sessions are referenced by their id (directory name in
trace_store/), which `afr list` prints in full. Prefixes work too.
Every forensic command exits 0 on a clean verdict, non-zero otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from .diff import diff_sessions
from .demo_agent import (
    DemoAgent, MockProvider,
    auditor_script, builder_script, failing_script, planner_script, build_workspace,
)
from .prefetch import PrefetchEngine, NGramPredictor
from .recorder import Recorder
from .replay import replay
from .report import build_terminal_report, write_markdown_report, write_report
from .schema import Event
from .store import TraceStore
from .tools import make_tools

def _resolve_session(store_root: str, ref: str) -> Optional[str]:
    """Accept exact ids or unambiguous prefixes."""
    if not os.path.isdir(store_root):
        return None
    candidates = sorted(d for d in os.listdir(store_root) if d.startswith(ref))
    if len(candidates) == 1:
        return candidates[0]
    if ref in candidates:
        return ref
    return None


def _load(store_root: str, ref: str) -> Optional[List[Event]]:
    sid = _resolve_session(store_root, ref)
    if sid is None:
        return None
    return TraceStore(store_root, sid).read_events()


def _print_json(data: Dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=True))


def _sane(text: str) -> str:
    """Strip braces so a human summary never breaks JSON-parsing consumers."""
    return str(text).replace("{", "(").replace("}", ")")


DEFAULT_STORE = os.path.join(os.getcwd(), "trace_store")


# ------------------------------------------------------------------- record


def cmd_record(args: argparse.Namespace) -> int:
    workspace = os.path.abspath(args.workspace or "workspace")   # safe default
    store_root = args.store or DEFAULT_STORE
    build_workspace(workspace)
    registry = make_tools(workspace)
    provider = MockProvider({
        "planner": planner_script(), "auditor": auditor_script(),
        "builder": builder_script(), "failing": failing_script(),
    }[args.scenario])
    store = TraceStore(store_root)
    recorder = Recorder(
        store, registry,
        agent_meta={"name": "demo", "provider": provider.name,
                    "model": provider.model, "scenario": args.scenario},
    )
    agent = DemoAgent(recorder, provider)
    outcome = agent.run(args.task or f"scenario:{args.scenario}")
    sid = store.session_id
    print(f"recorded session {sid}")
    print(f"  scenario {args.scenario}  ok={outcome['ok']}  steps={outcome['steps']}")
    if outcome.get("final"):
        print(f"  agent said: {_sane(outcome['final'].splitlines()[0][:120])}")
    print(f"  trace: {store.trace_path}")
    print("  next steps:")
    print(f"    python -m afr view    {sid}    # flight report on the terminal + a .md file")
    print(f"    python -m afr replay  {sid}    # re-walk the plan without re-running it")
    print(f"    python -m afr verify  {sid}    # tamper-evidence check")
    return 0 if outcome["ok"] else 1


# ------------------------------------------------------------ replay/verify


def cmd_replay(args: argparse.Namespace) -> int:
    events = _load(args.store or DEFAULT_STORE, args.session)
    if events is None:
        print(f"no session matches {args.session!r} (try: afr list)", file=sys.stderr)
        return 2
    workspace = args.workspace
    if workspace:
        workspace = os.path.abspath(workspace)
        registry = make_tools(workspace)
    else:
        registry = None
    mode = "verify" if args.verify else "recorded"
    report = replay(events, registry=registry, mode=mode, workspace=workspace)
    _print_replay_summary(report)
    _print_json(report.to_dict())
    return 0 if report.ok else 1


def _print_replay_summary(report) -> None:
    """Human-readable verdict lines, then the JSON block follows verbatim."""
    print(f"replay {report.session_id}   (mode={report.mode})")
    print(f"  steps {report.steps} | tool calls {report.tool_calls} | "
          f"re-checked {report.verified_calls} | deviations {report.deviations}")
    if report.ok:
        print("  verdict: CLEAN - the recorded plan replays exactly")
    else:
        print(f"  verdict: DRIFT - {report.deviations} step(s) did not replay as recorded")
    for note in report.notes:
        print(f"  note: {_sane(note)}")
    print()


def cmd_verify(args: argparse.Namespace) -> int:
    sid = _resolve_session(args.store or DEFAULT_STORE, args.session)
    if sid is None:
        print(f"no session matches {args.session!r}", file=sys.stderr)
        return 2
    verdict = TraceStore(args.store or DEFAULT_STORE, sid).verify_integrity()
    print(f"verify {sid}")
    print(f"  events {verdict['events']} | timestamps in order: {verdict['monotonic_ts']} "
          f"| content hash matches manifest: {verdict['chain_ok']}")
    if verdict["ok"] and verdict["complete"]:
        print("  verdict: CLEAN - the trace file is intact as recorded")
    elif verdict["ok"]:
        print("  verdict: INTACT so far, but the session never closed (no run.end)")
    else:
        print("  verdict: TAMPERED - the file changed after the manifest recorded it")
    print()
    _print_json(verdict)
    return 0 if verdict["ok"] else 1


def cmd_list(args: argparse.Namespace) -> int:
    root = args.store or DEFAULT_STORE
    if not os.path.isdir(root):
        print("(no sessions yet -- try: afr record)")
        return 0
    for sid in sorted(os.listdir(root)):
        store = TraceStore(root, sid)
        events = store.read_events()
        n = len(events)
        done = "complete" if any(e.type == "run.end" for e in events) else "INTERRUPTED"
        agent = next((e.payload.get("agent", {}) for e in events if e.type == "run.start"), {})
        print(f"{sid}  events={n:4d}  {done:11s}  "
              f"agent={agent.get('name', 'unknown')}:{agent.get('scenario', '-')}")
    return 0


# ---------------------------------------------------------------- prefetch


def cmd_prefetch(args: argparse.Namespace) -> int:
    events = _load(args.store or DEFAULT_STORE, args.session)
    if events is None:
        print(f"no session matches {args.session!r}", file=sys.stderr)
        return 2
    workspace = os.path.abspath(args.workspace) if args.workspace else _first_workspace(events)
    if not os.path.isdir(workspace):
        print(f"workspace {workspace!r} not found; pass --workspace", file=sys.stderr)
        return 2

    predictor = NGramPredictor.from_trace(events)
    if args.model_path and os.path.exists(args.model_path):
        predictor = NGramPredictor.load(args.model_path)
    registry = make_tools(workspace)
    engine = PrefetchEngine(predictor, registry, workspace, p_min=args.p_min)

    if args.train:
        # record→train→save: hand-traces are free training data. This is
        # the loop a real user runs: record once, prefetch in later runs.
        predictor.save(args.train)
        print(f"predictor trained on {len(events)} events; saved to {args.train}", file=sys.stderr)

    ledger = engine.evaluate(events, live=args.live)
    if args.out:
        engine.save_ledger(args.out, ledger)
        print(f"ledger saved: {args.out}", file=sys.stderr)
    _print_json(ledger)
    return 0


def _first_workspace(events: List[Event]) -> str:
    for ev in events:
        if ev.type == "run.start":
            snap = ev.payload.get("env_snapshot", {}) or {}
            if snap.get("workspace"):
                return snap["workspace"]
            return snap.get("cwd") or "."
    return "."


# -------------------------------------------------------------------- diff


def cmd_diff(args: argparse.Namespace) -> int:
    ea = _load(args.store or DEFAULT_STORE, args.session_a)
    eb = _load(args.store or DEFAULT_STORE, args.session_b)
    if ea is None or eb is None:
        print("both sessions must exist (try: afr list)", file=sys.stderr)
        return 2
    td = diff_sessions(ea, eb, label_a=args.session_a, label_b=args.session_b)
    print(td.summary())
    if args.json:
        _print_json(td.to_dict())
    return 0


# -------------------------------------------------------------------- view


def cmd_view(args: argparse.Namespace) -> int:
    events = _load(args.store or DEFAULT_STORE, args.session)
    if events is None:
        print(f"no session matches {args.session!r}", file=sys.stderr)
        return 2
    # -o report.html -> self-contained HTML (kept for CI scripts); default:
    # terminal-form report on stdout + markdown (.md) file on disk
    if args.output and args.output.lower().endswith(".html"):
        path = write_report(events, args.output, open_browser=args.open)
        print(f"report written: {path}")
        if args.open:
            print("opened in your browser")
        return 0
    trace_path = os.path.join(os.path.abspath(args.store or DEFAULT_STORE),
                              events[0].session_id, "trace.jsonl")
    out = args.output or os.path.join("results", f"report-{events[0].session_id}.md")
    out = write_markdown_report(events, out, trace_path=trace_path)
    print(build_terminal_report(events))
    print(f"markdown report written: {out}")
    if args.html or args.open:
        html_path = os.path.splitext(out)[0] + ".html"
        write_report(events, html_path, open_browser=args.open)
        print(f"html report written:    {html_path}")
        if args.open:
            print("opened html in your browser")
    return 0


# ------------------------------------------------------------------ wiring


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="afr", description="Agent Flight Recorder")
    sub = p.add_subparsers(dest="cmd", required=True)

    def store_arg(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--store", default=None, help="trace store root (default ./trace_store)")

    r = sub.add_parser("record", help="record the demo agent on a workspace")
    store_arg(r)
    r.add_argument("workspace", nargs="?", default=None, help="sandbox workspace dir (created if missing)")
    r.add_argument("--scenario", default="planner", choices=["planner", "auditor", "builder", "failing"])
    r.add_argument("--task", default=None)
    r.set_defaults(fn=cmd_record)

    p1 = sub.add_parser("replay", help="replay a recorded session deterministically")
    store_arg(p1)
    p1.add_argument("session")
    p1.add_argument("--verify", action="store_true", help="re-execute read-only tools and compare")
    p1.add_argument("--workspace", default=None, help="workspace for verify mode")
    p1.set_defaults(fn=cmd_replay)

    v = sub.add_parser("verify", help="trace-file integrity check")
    store_arg(v)
    v.add_argument("session")
    v.set_defaults(fn=cmd_verify)

    ls = sub.add_parser("list", help="list recorded sessions")
    store_arg(ls)
    ls.set_defaults(fn=cmd_list)

    pf = sub.add_parser("prefetch", help="price prefetch against a recorded trace")
    store_arg(pf)
    pf.add_argument("session")
    pf.add_argument("--workspace", default=None)
    pf.add_argument("--p-min", type=float, default=0.5)
    pf.add_argument("--live", action="store_true", help="shadow-run missed predictions for honest wasted_ms")
    pf.add_argument("--train", default=None, metavar="MODEL.json", help="save the fitted predictor here")
    pf.add_argument("--model-path", default=None, metavar="MODEL.json", help="load a previously fitted predictor")
    pf.add_argument("--out", default=None, metavar="LEDGER.json", help="write the cost ledger")
    pf.set_defaults(fn=cmd_prefetch)

    d = sub.add_parser("diff", help="plan-diff two sessions")
    store_arg(d)
    d.add_argument("session_a")
    d.add_argument("session_b")
    d.add_argument("--json", action="store_true")
    d.set_defaults(fn=cmd_diff)

    vi = sub.add_parser("view", help="flight report: terminal summary + markdown .md file")
    store_arg(vi)
    vi.add_argument("session")
    vi.add_argument("-o", "--output", default=None,
                    help="markdown report path (default results/report-<session>.md; "
                         "ends with .html -> write the web report there instead)")
    vi.add_argument("--html", action="store_true", help="also write the self-contained HTML report")
    vi.add_argument("--open", action="store_true", help="also open the HTML report in your browser")
    vi.set_defaults(fn=cmd_view)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # CLI never dumps a stack at users; debug via AFR_TRACEBACK=1
        if os.environ.get("AFR_TRACEBACK"):
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
