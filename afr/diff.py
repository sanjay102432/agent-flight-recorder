"""Diff two recorded sessions and explain what changed.

Two agent runs that "did the same thing" are rarely the same: the model
answered differently, chose a different tool in the middle, or the
workspace itself moved under it. TraceDiff lines those two sequences up
step-by-step (via difflib over the tool names) and reports, in order:

  * how much of the plan matched (``similarity``),
  * which steps matched on tool but not on args (the interesting ones),
  * which results changed (tool_name + receipts say who moved the world),
  * where one session declared done and the other kept churning.

The decomposition is deliberately plain:
    plan similarity     = matched tool steps / max(len planA, len planB)
    arg drift           = same tool, different args
    result drift        = same tool+args, different result hash
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .schema import EVENT_TYPES, Event, text_sha256
from .schema import (
    LLM_RESPONSE, STEP_START, TOOL_INTENT, TOOL_RESULT,
)


@dataclass
class StepDigest:
    """Minimal fingerprint of one loop step, the atom the diff works on."""
    step: int
    tool: Optional[str]
    args_sha: str
    result_sha: str
    ok: Optional[bool]
    response_sha: str = ""

    @property
    def sig(self) -> Optional[str]:
        """The alignment atom: the tool NAME only. Args and results drift
        *within* aligned steps and are reported separately -- aligning on
        result hashes makes every bit of workspace change look like a plan
        change, which reads as noise in review."""
        return self.tool


def digest_steps(events: List[Event]) -> List[StepDigest]:
    """Reduce an event list into a plain tool/result sequence of digests."""
    digests: List[StepDigest] = []
    latest_response: Dict[int, str] = {}
    pending: Dict[int, StepDigest] = {}

    for ev in events:
        step = ev.payload.get("step")
        if step is None:
            continue
        step = int(step)
        if ev.type == LLM_RESPONSE:
            latest_response[step] = ev.payload.get("response_sha", "")
        elif ev.type == TOOL_INTENT:
            d = StepDigest(
                step=step, tool=ev.payload["tool"],
                args_sha=ev.payload.get("args_sha", ""),
                result_sha="", ok=None,
                response_sha=latest_response.get(step, ""),
            )
            pending[step] = d
        elif ev.type == TOOL_RESULT:
            d = pending.get(step)
            if d is None:  # defensive: result without intent in a broken trace
                d = StepDigest(step=step, tool=ev.payload.get("tool"),
                               args_sha=text_sha256(repr(sorted((ev.payload.get("args") or {}).items()))),
                               result_sha="", ok=None)
                pending[step] = d
            d.tool = ev.payload["tool"]
            d.result_sha = ev.payload.get("result_sha", "")
            d.ok = ev.payload.get("ok")
        elif ev.type == STEP_START:
            pending.setdefault(step, StepDigest(step=step, tool=None, args_sha="", result_sha="", ok=None))

    dump = [d for d in pending.values() if d.tool is not None]
    return sorted(dump, key=lambda d: d.step)


@dataclass
class TraceDiff:
    session_a: str
    session_b: str
    alignment: List[Tuple[Optional[StepDigest], Optional[StepDigest]]] = field(default_factory=list)
    matches: int = 0
    arg_drifts: List[Dict[str, Any]] = field(default_factory=list)
    result_drifts: List[Dict[str, Any]] = field(default_factory=list)
    kept_going: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def similarity(self) -> float:
        width = max(
            len([1 for a, _ in self.alignment if a]),
            len([1 for _, b in self.alignment if b]),
        )
        if width == 0:
            return 1.0
        return round(self.matches / width, 3)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_a": self.session_a,
            "session_b": self.session_b,
            "similarity": self.similarity,
            "matches": self.matches,
            "arg_drifts": self.arg_drifts,
            "result_drifts": self.result_drifts,
            "kept_going": self.kept_going,
            "alignment": [
                {"a": a.step if a else None, "b": b.step if b else None,
                 "tool": (a.tool if a else b.tool),
                 "same_args": (a.args_sha == b.args_sha if a and b else None),
                 "same_result": (a.result_sha == b.result_sha if a and b else None)}
                for a, b in self.alignment
            ],
        }

    def summary(self) -> str:
        lines = [
            f"diff {self.session_a}  vs  {self.session_b}",
            f"plan similarity {self.similarity:.0%}  ({self.matches} aligned steps)",
        ]
        for d in self.arg_drifts:
            lines.append(f"  arg drift   step {d['step_a']} vs {d['step_b']}. Tool {d['tool']}")
        for d in self.result_drifts:
            lines.append(f"  result drift step {d['step_a']} vs {d['step_b']}")
        for k in self.kept_going:
            lines.append(f"  extra steps in {self.session_b}: from step {k['from_step']}")
        if self.similarity == 1.0 and not self.arg_drifts and not self.result_drifts:
            lines.append("sessions replay identically")
        return "\n".join(lines)


def diff_sessions(events_a: List[Event], events_b: List[Event],
                  label_a: str = "a", label_b: str = "b") -> TraceDiff:
    """Align two traces' tool sequences; decompose the difference."""
    da = digest_steps(events_a)
    db = digest_steps(events_b)
    td = TraceDiff(session_a=label_a, session_b=label_b)

    sm = difflib.SequenceMatcher(None, [d.sig for d in da], [d.sig for d in db])
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            for k in range(i2 - i1):
                a, b = da[i1 + k], db[j1 + k]
                td.alignment.append((a, b))
                td.matches += 1
                if a.args_sha != b.args_sha:
                    td.arg_drifts.append({
                        "tool": a.tool, "step_a": a.step, "step_b": b.step,
                        "args_a_sha": a.args_sha, "args_b_sha": b.args_sha,
                    })
                if a.result_sha != b.result_sha:
                    td.result_drifts.append({
                        "tool": a.tool, "step_a": a.step, "step_b": b.step,
                        "result_a_sha": a.result_sha, "result_b_sha": b.result_sha,
                    })
        elif op == "replace":
            for k in range(max(i2 - i1, j2 - j1)):
                a = da[i1 + k] if i1 + k < i2 else None
                b = db[j1 + k] if j1 + k < j2 else None
                td.alignment.append((a, b))
                if a and b and a.tool == b.tool:
                    if a.args_sha != b.args_sha:
                        td.arg_drifts.append({
                            "tool": a.tool, "step_a": a.step, "step_b": b.step,
                            "args_a_sha": a.args_sha, "args_b_sha": b.args_sha,
                        })
                    if a.result_sha != b.result_sha:
                        td.result_drifts.append({
                            "tool": a.tool, "step_a": a.step, "step_b": b.step,
                            "result_a_sha": a.result_sha, "result_b_sha": b.result_sha,
                        })
        elif op == "insert":
            for j in range(j1, j2):
                td.alignment.append((None, db[j]))
        elif op == "delete":
            for i in range(i1, i2):
                td.alignment.append((da[i], None))

    # "kept going": after the last matched step, session B did extra work --
    # a cheap but telling signal when comparing e.g. model versions.
    last_shared = 0
    for a, b in td.alignment:
        if a and b:
            last_shared = max(last_shared, a.step, b.step)
    for d in db:
        if d.step > last_shared:
            td.kept_going.append({"from_step": last_shared, "extra_tool": d.tool})
            break
    return td
