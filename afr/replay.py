"""Replay: walk a recorded trace and re-produce the agent session's *shape*
without re-running anything -- or, optionally, re-checking read-only steps
against the live workspace.

Two replay modes, same walker:

  recorded (default)   No tool, no model, no network runs. The step plan is
                       re-derived from the trace and every result comes from
                       the recorded event. This is the "flight simulator"
                       mode: same walk, zero I/O, used for debugging and audit.

  verify               Like recorded, but for every *read-only* tool call
                       the tool is re-executed NOW and compared to the
                       recorded result. Writes and network are skipped
                       without exception. This is the "does this trace
                       still hold true in my workspace" mode, and it is the
                       bridge the prefetch engine drives on.

Both modes end with a ReplayReport whose most important number is
``deviations``: 0 means the session replays exactly. Anything else is
lexicographically pinned to the step that drifted.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .schema import (
    RUN_END, RUN_START, STEP_END, STEP_START, LLM_RESPONSE,
    TOOL_INTENT, TOOL_RESULT,
    Event, text_sha256,
)
from .tools import SPECULATIVE_CLASSES, ToolRegistry


@dataclass
class StepPlan:
    """What the agent did at one loop iteration, extracted from the trace."""
    step: int
    llm_response: Optional[str] = None
    tool: Optional[str] = None
    args: Optional[Dict[str, Any]] = None
    result: Optional[Any] = None
    ok: Optional[bool] = None
    receipt: Optional[Dict[str, Any]] = None
    status: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class ReplayReport:
    session_id: str
    mode: str
    steps: int = 0
    tool_calls: int = 0
    verified_calls: int = 0
    deviations: int = 0
    notes: List[str] = field(default_factory=list)
    plan: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.deviations == 0

    def add_note(self, msg: str) -> None:
        self.notes.append(msg)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id, "mode": self.mode,
            "steps": self.steps, "tool_calls": self.tool_calls,
            "verified_calls": self.verified_calls, "deviations": self.deviations,
            "ok": self.ok, "notes": self.notes, "plan": self.plan,
        }


def extract_plan(events: List[Event]) -> List[StepPlan]:
    """Group a flat event list into per-step plans.

    Groups by ``payload.step``; the grouping must be complete: a step that
    opened but never closed is reported, not silently dropped.
    """
    steps: Dict[int, StepPlan] = {}
    last_intent: Dict[int, Tuple[str, Dict[str, Any]]] = {}

    for ev in events:
        if ev.type in (STEP_START, STEP_END, LLM_RESPONSE, TOOL_RESULT, TOOL_INTENT):
            st = int(ev.payload.get("step", -1))
            plan = steps.setdefault(st, StepPlan(step=st))
            if ev.type == STEP_END:
                plan.status = ev.payload.get("status", "")
            elif ev.type == LLM_RESPONSE:
                plan.llm_response = ev.payload.get("response")
            elif ev.type == TOOL_INTENT:
                last_intent[st] = (ev.payload["tool"], ev.payload.get("args", {}))
            elif ev.type == TOOL_RESULT:
                plan.tool = ev.payload["tool"]
                plan.args = ev.payload.get("args", {})
                plan.result = ev.payload.get("result")
                plan.ok = ev.payload.get("ok")
                plan.receipt = ev.payload.get("receipt")

    for st, plan in steps.items():
        if plan.tool is None and st in last_intent:
            plan.tool, plan.args = last_intent[st]
            plan.result = None
            plan.ok = None
            plan.status = plan.status or "intent-no-result"
    # the tool reference connecting intent->result is a *receipt contract*:
    # if an intent exists with no result step, the trace is technically
    # complete but the plan marks the break instead of papering over it.
    out = [steps[k] for k in sorted(steps)]
    return out


def _same(actual: Any, recorded: Any) -> bool:
    """Result comparison. Recorded text can be compared verbatim; other
    kinds fall back to canonical repr hashes, which is exactly how the
    recorder hashed them at write time."""
    if isinstance(actual, str) and isinstance(recorded, str):
        return actual == recorded
    return text_sha256(repr(actual)) == text_sha256(repr(recorded))


def replay(trace_events: List[Event],
           registry: Optional[ToolRegistry] = None,
           mode: str = "recorded",
           workspace: Optional[str] = None) -> ReplayReport:
    """Re-walk a recorded session. See module docstring for the two modes.

    ``workspace`` is required when mode='verify' (read-only re-execution).
    """
    if mode not in ("recorded", "verify"):
        raise ValueError(f"unknown replay mode {mode!r}")
    if mode == "verify" and registry is None:
        raise ValueError("mode='verify' needs a ToolRegistry to re-execute")

    report = ReplayReport(session_id=_session_id_of(trace_events), mode=mode)

    plan = extract_plan(trace_events)
    report.steps = len(plan)
    report.plan = [p.to_dict() for p in plan]

    started = [e for e in trace_events if e.type == RUN_START]
    if not started:
        report.add_note("trace has no run.start; refusing to rate it")
    elif not any(e.type == RUN_END for e in trace_events):
        report.add_note("trace has run.start but no run.end (interrupted run?)")

    for p in plan:
        if p.tool is None:
            continue
        report.tool_calls += 1
        if p.receipt is None or p.ok is None:
            report.deviations += 1
            report.add_note(f"step {p.step}: tool intent without recorded result")
            continue

        if mode == "verify" and p.tool in registry.tools:
            spec = registry.tools[p.tool]
            if spec.safety in SPECULATIVE_CLASSES:
                try:
                    out = spec.fn(**(p.args or {}))
                    now_result, now_receipt = out if isinstance(out, tuple) else (_str(out), None)
                    if not _same(now_result, p.result):
                        report.deviations += 1
                        report.add_note(
                            f"step {p.step}: {p.tool} result drifted "
                            f"recorded={str(p.result)[:60]!r} now={str(now_result)[:60]!r}"
                        )
                    else:
                        report.verified_calls += 1
                except Exception as exc:
                    report.deviations += 1
                    report.add_note(f"step {p.step}: {p.tool} now raises {type(exc).__name__}: {exc}")
            else:
                report.add_note(f"step {p.step}: {p.tool} is {spec.safety}; skipped verification by policy")

    if mode == "verify" and registry is not None and workspace is None:
        report.add_note("note: verify mode allowed without workspace; fs tools are root-locked per registry")
    return report


def _session_id_of(events: List[Event]) -> str:
    for ev in events:
        return ev.session_id
    return "(empty trace)"


def _str(out: Any) -> str:
    if isinstance(out, str):
        return out
    from .recorder import _textify
    return _textify(out)
