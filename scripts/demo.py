"""One-command tour of the flight recorder. Runs in ~5 seconds, fully offline.

    python scripts/demo.py            (from the repo root)

It records two agent sessions (planner, auditor) in ./examples/trace,
replays them, prices prefetch in both offline and live modes, diffs the
two plans, captures integrity verdicts, and writes the HTML flight
report. Everything it prints is either a receipt, a lead-time number,
or a diff -- deliberately no `trust me` sentences.
"""

from __future__ import annotations

import json
import os
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from afr.cli import (
    cmd_diff, cmd_prefetch, cmd_replay, cmd_record, cmd_verify, cmd_view,
)


class Args:
    def __init__(self, **kwargs) -> None:
        self.store = None
        self.workspace = None
        self.verify = False
        self.json = False
        self.p_min = 0.5
        self.live = False
        self.train = None
        self.model_path = None
        self.out = None
        self.output = None
        self.open = False
        self.session = None
        self.session_a = None
        self.session_b = None
        self.scenario = "planner"
        self.task = None
        for k, v in kwargs.items():
            setattr(self, k, v)


def banner(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def main() -> int:
    import os

    t0 = time.perf_counter()
    store = os.path.join(REPO, "examples", "trace")
    wsA = os.path.join(REPO, "examples", "ws-run-a")
    wsB = os.path.join(REPO, "examples", "ws-run-b")
    for root in (wsA, wsB):
        os.makedirs(root, exist_ok=True)

    banner("1) record: the planner agent, twice (two workspaces, one system)")
    cmd_record(Args(store=store, workspace=wsA, scenario="planner"))
    cmd_record(Args(store=store, workspace=wsB, scenario="auditor"))

    sessions = sorted(os.listdir(store))
    a, b = sessions[-2], sessions[-1]
    print(f"  (sessions: {a} | {b})")

    banner("2) replay: deterministic walk of the first session")
    rc = cmd_replay(Args(store=store, session=a))
    if rc != 0:
        return rc

    banner("3) verify: replay + re-run the read-only tools against the live workspace")
    cmd_replay(Args(store=store, session=a, verify=True, workspace=wsA))

    banner("4) integrity: tamper-evidence of the trace file")
    cmd_verify(Args(store=store, session=a))

    banner("5) prefetch: offline ledger (no shadow execution)")
    cmd_prefetch(Args(store=store, session=a, workspace=wsA,
                      train=os.path.join(REPO, "results", "predictor.json"),
                      out=os.path.join(REPO, "results", "ledger-offline.json")))

    banner("6) prefetch: LIVE ledger (each miss shadow-runs for real wasted_ms)")
    cmd_prefetch(Args(store=store, session=b, workspace=wsB, live=True,
                      model_path=os.path.join(REPO, "results", "predictor.json"),
                      out=os.path.join(REPO, "results", "ledger-live.json")))

    banner("7) diff: what changed between the two agent runs")
    cmd_diff(Args(store=store, session_a=a, session_b=b, json=True))

    banner("8) view: the HTML flight report")
    out = os.path.join(REPO, "examples", f"report-{a}.html")
    cmd_view(Args(store=store, session=a, output=out, open_browser=False))

    banner(f"done in {time.perf_counter() - t0:.1f}s.  Open the report:")
    print(f"    {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
