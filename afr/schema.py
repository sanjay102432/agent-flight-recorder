"""AFR-1: the Agent Flight Recorder trace schema.

````

What belongs in this module
---------------------------
Everything that defines *what a trace looks like*. It is deliberately
small and dependent-free: schema.py is imported by every other module,
so it must never import them back (no cycles, ever).

The schema is event-sourced
---------------------------
A trace is a flat, append-only list of events (JSON lines). There is no
mutable state stored anywhere; every observable fact is one event. If a
tool call happened, there is exactly one ``tool.intent`` and exactly one
``tool.result`` for it. If the model answered, there is exactly one
``llm.response``. Nothing is ever overwritten after being flushed.

Event types (the closed vocabulary of AFR-1 v1)::

    run.start          one agent run begins
    agent.step_start   the agent is about to think+act (loop iteration)
    llm.request        prompt text sent to the model (hashed + optionally inlined)
    llm.response       raw model answer -- recorded as a FACT, never re-derived
    tool.intent        agent declares it wants to call tool T with args A
    tool.result        tool T ran with args A; result + receipt are the facts
    prefetch.prediction   predictor believes next tool will be T.args
    prefetch.result    was that prediction right? did the shadow execution pay?
    agent.step_end     loop iteration closes
    run.end            the trace is closed; manifest gets ``complete: true``
    error              anything that interrupted the loop above
```

All timestamps are integer nanoseconds since the epoch (UTC wall clock)
so they sort exactly and survive JSON round-trips as integers.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List

SCHEMA_VERSION = 1

# -- Event types -----------------------------------------------------------
RUN_START = "run.start"
RUN_END = "run.end"
STEP_START = "agent.step_start"
STEP_END = "agent.step_end"
LLM_REQUEST = "llm.request"
LLM_RESPONSE = "llm.response"
TOOL_INTENT = "tool.intent"
TOOL_RESULT = "tool.result"
PREFETCH_PREDICTION = "prefetch.prediction"
PREFETCH_RESULT = "prefetch.result"
ERROR = "error"

EVENT_TYPES = (
    RUN_START, RUN_END, STEP_START, STEP_END,
    LLM_REQUEST, LLM_RESPONSE, TOOL_INTENT, TOOL_RESULT,
    PREFETCH_PREDICTION, PREFETCH_RESULT, ERROR,
)


def text_sha256(text: str) -> str:
    """Stable hash for any recorded text. sha256 hex, first 16 chars.

    We keep the hash short because full hashes are mainly for *dedup and
    linkage regimes*; collision risk on a single machine's day of work is
    negligible, and short hashes stay grep-able in the JSONL.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class Event:
    """One row of a trace. ``payload`` is type-specific; see SCHEMAS below."""

    type: str
    ts_ns: int
    eid: str               # event id: e<zero-padded seq>, e.g. e000041
    run_id: str
    session_id: str
    depth: int = 0         # nesting depth (1 tool inside 1 llm step -> depth 1)
    payload: Dict[str, Any] = field(default_factory=dict)

    # -- serialization -------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "v": SCHEMA_VERSION,
            "type": self.type,
            "ts_ns": self.ts_ns,
            "eid": self.eid,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "depth": self.depth,
            "payload": self.payload,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=True, sort_keys=True)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Event":
        if d.get("v") != SCHEMA_VERSION:
            raise SchemaError(f"unsupported schema version {d.get('v')!r}")
        return Event(
            type=d["type"],
            ts_ns=int(d["ts_ns"]),
            eid=d["eid"],
            run_id=d["run_id"],
            session_id=d["session_id"],
            depth=int(d.get("depth", 0)),
            payload=dict(d.get("payload", {})),
        )

    @staticmethod
    def from_json(line: str) -> "Event":
        return Event.from_dict(json.loads(line))


class SchemaError(ValueError):
    """Raised when an event would violate the AFR-1 contract."""


# The one field that MUST exist per event type, beyond the envelope keys.
# Used by validate() to fail fast on writer bugs instead of silently
# producing a trace that only half-replays days later.
_REQUIRED = {
    RUN_START: ("agent", "mode"),
    RUN_END: ("summary",),
    STEP_START: ("step",),
    STEP_END: ("step", "status"),
    LLM_REQUEST: ("step", "prompt"),
    LLM_RESPONSE: ("step", "response"),
    TOOL_INTENT: ("step", "tool", "args"),
    TOOL_RESULT: ("step", "tool", "args", "receipt", "result_kind", "duration_ns"),
    PREFETCH_PREDICTION: ("step", "tool", "p"),
    PREFETCH_RESULT: ("step", "tool", "correct", "shadow_result_kind"),
    ERROR: ("message",),
}


def validate(ev: Event) -> Event:
    """Return ``ev`` unchanged if valid, raise SchemaError if not.

    Checks (cheap and boring on purpose):
      * type is a known event type
      * ts_ns is a non-negative int, monotonically checkable later by the store
      * required fields exist and have sane types
      * ids / session ids are non-empty
    """
    if ev.type not in EVENT_TYPES:
        raise SchemaError(f"unknown event type {ev.type!r}")
    if not isinstance(ev.ts_ns, int) or ev.ts_ns < 0:
        raise SchemaError("ts_ns must be a non-negative int")
    if not ev.run_id or not ev.session_id:
        raise SchemaError("run_id and session_id must be non-empty")
    if not isinstance(ev.payload, dict):
        raise SchemaError("payload must be a dict")
    required = _REQUIRED[ev.type]
    for key in required:
        if key not in ev.payload:
            raise SchemaError(f"event {ev.type} missing payload field {key!r}")
    # light type checks on the fields that downstream code *relies* on
    _require_int(ev, "step")
    if ev.type == TOOL_RESULT:
        _require_int(ev, "duration_ns")
    if ev.type == PREFETCH_RESULT:
        _require_bool(ev, "correct")
    if ev.depth < 0:
        raise SchemaError("depth must be >= 0")
    return ev


def _require_int(ev: Event, key: str) -> None:
    if key in ev.payload and not isinstance(ev.payload[key], int):
        raise SchemaError(f"{ev.type}.{key} must be int")


def _require_bool(ev: Event, key: str) -> None:
    if key in ev.payload and not isinstance(ev.payload[key], bool):
        raise SchemaError(f"{ev.type}.{key} must be bool")


# -- Small handy payload builders -----------------------------------------


def run_start_payload(mode: str, agent: Dict[str, Any], policy: Dict[str, Any], env_snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """mode: 'record' (real run) or 'replay' (deterministic walk)."""
    return {
        "mode": mode,
        "agent": agent,
        "policy": policy,
        "env_snapshot": env_snapshot,  # what the world looked like at T0
    }


def run_end_payload(ok: bool, steps: int, reason: str, counts: Dict[str, int]) -> Dict[str, Any]:
    return {
        "summary": {"ok": ok, "steps": steps, "reason": reason},
        "counts": counts,
    }


def llm_request_payload(step: int, prompt: str, provider: str, model: str) -> Dict[str, Any]:
    """The prompt text itself is inlined. Prompts are small; traces want to
    be self-contained. If a future provider sends megabyte contexts, the
    store cuts them to ``prompt_head`` + hash -- an explicit, findable cut."""
    return {
        "step": step,
        "prompt": prompt,
        "prompt_sha": text_sha256(prompt),
        "provider": provider,
        "model": model,
    }


def llm_response_payload(step: int, response: str, tokens_in: int, tokens_out: int) -> Dict[str, Any]:
    return {
        "step": step,
        "response": response,          # recorded fact, never re-derived on replay
        "response_sha": text_sha256(response),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
    }


def tool_intent_payload(step: int, tool: str, args: Dict[str, Any], safety: str) -> Dict[str, Any]:
    return {
        "step": step,
        "tool": tool,
        "args": args,
        "args_sha": text_sha256(repr(sorted(args.items()))),
        "safety": safety,     # safety class from the taxonomy; see tools.py
    }


def tool_result_payload(
    step: int,
    tool: str,
    args: Dict[str, Any],
    receipt: Dict[str, Any],
    result_kind: str,           # 'text' | 'json' | 'binary' | 'ok'
    result: Any,
    duration_ns: int,
    ok: bool,
) -> Dict[str, Any]:
    payload = {
        "step": step,
        "tool": tool,
        "args": args,             # repeated so tool.result is self-contained
        "receipt": receipt,       # what the tool *touched*, not just what it said
        "result_kind": result_kind,
        "result": result,
        "result_sha": text_sha256(repr(result)),
        "duration_ns": duration_ns,
        "ok": ok,
    }
    return payload


def prefetch_prediction_payload(step: int, tool: str, args: Dict[str, Any], p: float, predictor: str, rank: int) -> Dict[str, Any]:
    return {
        "step": step,
        "tool": tool,
        "args": args,
        "p": p,                  # model probability P(next tool = tool)
        "predictor": predictor,  # which predictor produced it
        "rank": rank,            # 1 if it was the top prediction
    }


def prefetch_result_payload(
    step: int,
    tool: str,
    correct: bool,
    shadow_result_kind: str,
    reused: bool,
    saved_ms: float,
    wasted_ms: float,
    shadow_receipt: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "step": step,
        "tool": tool,
        "correct": correct,
        "shadow_result_kind": shadow_result_kind,
        "reused": reused,        # did the real call make use of the shadow?
        "saved_ms": saved_ms,    # only counts on a hit, in shadow-execution cost accounting
        "wasted_ms": wasted_ms,  # only counts on a miss
        "shadow_receipt": shadow_receipt,
    }


def error_payload(kind: str, message: str, step: int = -1) -> Dict[str, Any]:
    return {"kind": kind, "message": message, "step": step}


# -- Whole-trace utilities -------------------------------------------------


def read_trace(path: str) -> List[Event]:
    """Parse a JSONL trace into events. Syntactically corrupt lines raise."""
    events: List[Event] = []
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(Event.from_json(line))
            except SchemaError:
                raise
            except Exception as exc:  # malformed line
                raise SchemaError(f"trace line {i} is not valid AFR-1: {exc}") from exc
    return events


# canonical shorter name used by store/cli/replay
read_events = read_trace



def check_monotonic(events: List[Event]) -> None:
    """Traces must be append-only in time: cross-run, later lines are >= earlier."""
    last = -1
    for ev in events:
        if ev.ts_ns < last:
            raise SchemaError(
                f"trace is not monotonic at {ev.eid} ({ev.ts_ns} < {last})"
            )
        last = ev.ts_ns
