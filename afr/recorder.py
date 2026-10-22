"""Recorder: turns a plain function-call into receipt-level trace events.

Everything about "what really happened" flows through two functions
defined here:

  * ``Recorder.llm``  -- wrap a model call: records prompt + response as facts
  * ``Recorder.tool`` -- wrap a tool call: records intent, receipt, result

The principles the code is written to:

  1. Record *outcome*, not opinion. We store the response text, the
     receipt (what was touched), and durations -- never a judgment.
  2. Record *before* you continue. Events hit disk the moment they are
     produced; a crash mid-step leaves a trace that replays up to the
     last durable invariants, not a vacuum.
  3. Never invent data that replay can't reproduce. One event per
     observable fact, always at the same position in the walk order.
"""

from __future__ import annotations

import json
import os
import platform
import time
import traceback
from typing import Any, Callable, Dict, Optional, Tuple

from . import schema
from .store import TraceStore, _now_ns
from .tools import ToolRegistry


class RecordedCallException(Exception):
    """Wraps a tool failure so the agent loop can react to it while the
    recorder has *already* captured the intent/result pair."""


class Recorder:
    """The object an agent loop talks to. Everything else (replay, prefetch,
    diff, report) is downstream and only ever reads what landed in the store."""

    def __init__(
        self,
        store: TraceStore,
        registry: ToolRegistry,
        agent_meta: Optional[Dict[str, Any]] = None,
        policy: Optional[Dict[str, Any]] = None,
        env: str = "python",
    ) -> None:
        self.store = store
        self.registry = registry
        self.agent_meta = agent_meta or {"name": "unknown", "provider": "unknown", "model": "unknown"}
        self.policy = policy or {"version": 1}
        self.mode = "record"
        self._steps = 0
        self._t0 = _now_ns()

    # ------------------------------------------------------------------ runs

    def open_run(self) -> str:
        env_snapshot = {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cwd": os.getcwd(),
            "workspace": self.registry.root,
        }
        self.store.append(
            schema.RUN_START,
            {
                "mode": self.mode,
                "agent": self.agent_meta,
                "policy": self.policy,
                "env_snapshot": env_snapshot,
                "started_ts_ns": self._t0,
            },
        )
        return self.store.session_id

    def close_run(self, ok: bool = True, reason: str = "complete") -> None:
        counts = {
            "steps": self._steps,
            "events": len(self.store.read_events()),
        }
        self.store.close({"ok": ok, "steps": self._steps, "reason": reason, "counts": counts})

    # ------------------------------------------------------------------ LLM

    def llm(self, fn: Callable[[str], str], step: int, prompt: str,
            tokens_in: Optional[int] = None, tokens_out: Optional[int] = None) -> str:
        """Call and trace one LLM turn.

        ``fn`` is anything prompt->text: a live OpenAI-compatible client, an
        offline offline 'MockProvider', whatever the upper agent loop owns.
        The Recorder never makes URLs or API calls and never re-parses
        provider formats; it only records.
        """
        meta = self.agent_meta
        self.store.append(schema.LLM_REQUEST, {
            "step": step,
            "prompt": prompt,
            "prompt_sha": schema.text_sha256(prompt),
            "provider": meta.get("provider", "unknown"),
            "model": meta.get("model", "unknown"),
        })
        t0 = time.perf_counter()
        response = fn(prompt)
        dur_ns = int((time.perf_counter() - t0) * 1e9)
        self.store.append(schema.LLM_RESPONSE, {
            "step": step,
            "response": response,
            "response_sha": schema.text_sha256(response),
            "tokens_in": tokens_in if tokens_in is not None else _rough_tokens(prompt),
            "tokens_out": tokens_out if tokens_out is not None else _rough_tokens(response),
            "duration_ns": dur_ns,
        })
        return response

    # ------------------------------------------------------------------ tools

    def tool_call(self, step: int, name: str, args: Optional[Dict[str, Any]] = None,
                  depth: int = 0) -> Tuple[Optional[str], Optional[Any], bool, Dict[str, Any]]:
        """The workhorse. Returns (result_text, raw_result, ok, receipt).

        Flow: TOOL_INTENT -> dispatch -> TOOL_RESULT (both direct to disk).
        The receipt drops the tool's *own* receipt if it returned one, else a
        generic one. The distinction matters: provenance of side-effects.

        Errors are records, not exceptions -- an agent that gets a KeyError
        gets that key's value from a TOOL_RESULT with ok=False, and this
        trace shows the exception message and class.
        """
        args = dict(args or {})
        spec = self.registry.get(name)

        # closure: intent is validated against the registry's promise
        self.store.append(schema.TOOL_INTENT, {
            "step": step,
            "tool": name,
            "args": args,
            "args_sha": schema.text_sha256(repr(sorted(args.items()))),
            "safety": spec.safety,
            "arg_hint": spec.arg_hint,
        })

        t0 = time.perf_counter()
        try:
            out = spec.fn(**args)
        except Exception as exc:  # tool errors are facts too
            dur_ns = int((time.perf_counter() - t0) * 1e9)
            tb = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            payload = {
                "step": step, "tool": name, "args": args,
                "receipt": {"kind": "error", "raising": type(exc).__name__},
                "result_kind": "text",
                "result": tb,
                "result_sha": schema.text_sha256(tb),
                "duration_ns": dur_ns,
                "ok": False,
            }
            self.store.append(schema.TOOL_RESULT, payload)
            raise ToolExecutionError(name, tb, self.store.trace_path) from exc

        dur_ns = int((time.perf_counter() - t0) * 1e9)
        result, custom_receipt = out if isinstance(out, tuple) and len(out) == 2 else (_textify(out), None)
        receipt = custom_receipt or {"kind": "opaque", "tool": name, "safety": spec.safety}

        self.store.append(schema.TOOL_RESULT, {
            "step": step, "tool": name, "args": args,
            "receipt": receipt,
            "result_kind": _result_kind(result),
            "result": result,
            "result_sha": schema.text_sha256(repr(result)),
            "duration_ns": dur_ns,
            "ok": True,
        })
        return result, result, True, receipt

    # ------------------------------------------------------------------ steps

    def start_step(self, step: int) -> None:
        self.store.append(schema.STEP_START, {"step": step})

    def end_step(self, step: int, status: str = "ok") -> None:
        self.store.append(schema.STEP_END, {"step": step, "status": status})

    def error(self, kind: str, message: str, step: int = -1) -> None:
        self.store.append(schema.ERROR, {"kind": kind, "message": message, "step": step})


class ToolExecutionError(Exception):
    """Re-raised tool error. Carries the trace so the caller can retry while
    the recorder keeps everything consistent."""

    def __init__(self, tool: str, detail: str, trace_path: str) -> None:
        super().__init__(f"tool {tool!r} failed: {detail}")
        self.tool = tool
        self.detail = detail
        self.trace_path = trace_path


def _textify(out: Any) -> str:
    if out is None:
        return "(none)"
    if isinstance(out, str):
        return out
    try:
        return json.dumps(out, ensure_ascii=True, sort_keys=True)
    except TypeError:
        return repr(out)


def _result_kind(result: Any) -> str:
    if isinstance(result, (dict, list)):
        return "json"
    if isinstance(result, (bytes, bytearray)):
        return "binary"
    return "text"


def _rough_tokens(text: str) -> int:
    """tokens ≈ words * 1.3 -- only as a fallback when the caller does not
    know real token counts. Rough is fine; the field is advisory."""
    return max(1, (len(text) // 4))


def parse_tool_call(response_text: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """The demo agent protocol: ```json {\"tool\": \"fs.read\", \"args\": ...} ```
    Kept deliberately tiny: the recorder tests agent plumbing, not prompts."""
    marker = "```json"
    if marker not in response_text:
        return None, None
    after = response_text.split(marker, 1)[1]
    closes = after.rfind("```")
    body = after[:closes] if closes != -1 else after
    try:
        parsed = json.loads(body.strip())
    except json.JSONDecodeError:
        return None, None
    tool = parsed.get("tool")
    if isinstance(tool, str) and tool:
        return tool, dict(parsed.get("args") or {})
    return None, None
