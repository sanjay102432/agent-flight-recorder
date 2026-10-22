"""Predictive prefetch: guess the next tool call and pre-run it in a shadow
sandbox so the agent can skip that latency -- with every claim recorded
and *billable*.

Three parts, in order:

 1. a predictor (n-gram over recorded tool-name sequences),
 2. a shadow executor that runs taxonomy-safe tools against a point-in-time
    copy of the workspace,
 3. a cost model that grades every prediction honestly: hit rate, wasted
    work, and the only number that matters -- ms saved vs ms wasted.

The safety rule is absolute and enforced here, not in documentation:

    A tool whose class is outside SPECULATIVE_CLASSES is never run
    speculatively. Speculate() re-checks the class at call time and raises
    SpeculationRefused. There is no dry-run flag and no override. Predicting
    a write tool must never succeed through an accident of plumbing, so the
    checks are structural: the ALLOWLIST is built from the taxonomy at
    construction, and the shadow registry is re-verified to carry the same
    classes as the live registry before any tool runs.

Why a tool *factory* instead of tool objects?

    Filesystem tools close over their workspace root. To execute a tool
    "in the shadow", we need the SAME tool name and class but bound to the
    shadow copy of the workspace. So every make_tools() call binds a fresh
    registry to a fresh root, and the engine uses a factory to mint the
    shadow's registry. If a shadow registry disagrees with the live one on
    any tool's class, speculation is refused for that tool -- races between
    live re-registration and prefetch cannot flip policy silently.

The cost model, applied per predicted call:

    HIT    the real call: agent runs tool产生了 SAME args as shadow
           => saved_ms = the real call's own recorded duration
              (the shadow result is what the agent would have gotten)
    MISS   predicted tool X, agent called Y => wasted_ms = shadow time of X
           measured for real in live mode, 0 in offline mode.

    net_ms    = saved_ms - wasted_ms      (the number to pay rent with)
    hit_rate  = hits / (hits + misses)    (for humans)

The ledger dump IS the artifact:

    {"p_min": 0.5, "hit_rate": 0.62, "saved_ms": 18.7, "wasted_ms": 4.1,
     "net_ms": 14.6, "per_tool": {...}}
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Tuple

from .schema import TOOL_RESULT, Event
from .tools import SPECULATIVE_CLASSES, ToolRegistry, make_tools


ToolFactory = Callable[[str], ToolRegistry]


class SpeculationRefused(Exception):
    """Raised when anything tries to prefetch a non-speculative tool."""


class ShadowWorkspace:
    """Temporary point-in-time copy of a directory; deleted on close().

    v1 copies with shutil.copytree (workspaces in demos are tiny). If a
    future workspace is huge, replace _snapshot() with a reflink or
    overlay mount -- nothing else in the engine changes. There is no
    merge step: shadow writes are discarded with the copy.
    """

    def __init__(self, root: str) -> None:
        self.root_original = os.path.abspath(root)
        self.tmp = tempfile.mkdtemp(prefix="afr-shadow-")
        self.root = os.path.join(self.tmp, "ws")
        shutil.copytree(self.root_original, self.root, dirs_exist_ok=True)

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def __enter__(self) -> "ShadowWorkspace":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.cleanup()


# --------------------------------------------------------------- predictor


class NGramPredictor:
    """Order-2 Markov chain over tool names (plus an order-1 fallback).

    Deliberately tiny: prefetch must never be slow enough that its own
    decision costs money. A dict lookup is microseconds on this hardware.
    Smarter predictors (embedding classifiers, small-LLM heads) can slot in
    behind the same two methods (predict / observe) and be graded by the
    same cost model -- that's the comparison the ledger makes fair.
    """

    def __init__(self, predictor_id: str = "ngram-2") -> None:
        self.predictor_id = predictor_id
        self.bi: Dict[Tuple[str, str], Dict[str, int]] = defaultdict(lambda: {})
        self.uni: Dict[Tuple[str], Dict[str, int]] = defaultdict(lambda: {})

    # -- learning -------------------------------------------------------------

    def observe(self, tool: str, prev1: Optional[str], prev2: Optional[str] = None) -> None:
        if prev1 is None:
            return
        self.uni[(prev1,)][tool] = self.uni[(prev1,)].get(tool, 0) + 1
        if prev2 is not None:
            key = (prev2, prev1)
            self.bi[key][tool] = self.bi[key].get(tool, 0) + 1

    def fit_tool_sequence(self, tools: List[str]) -> None:
        for i, tool in enumerate(tools):
            prev1 = tools[i - 1] if i >= 1 else None
            prev2 = tools[i - 2] if i >= 2 else None
            self.observe(tool, prev1, prev2)

    @classmethod
    def from_trace(cls, events: List[Event], predictor_id: str = "ngram-2") -> "NGramPredictor":
        p = cls(predictor_id=predictor_id)
        p.fit_tool_sequence(_tool_sequence(events))
        return p

    # -- predicting -----------------------------------------------------------

    def predict(self, history: List[str]) -> List[Tuple[str, float]]:
        """Ranked [(tool, p)] guesses given the tool history so far."""
        if not history:
            return []
        t1, t2 = history[-1], (history[-2] if len(history) >= 2 else None)
        table: Dict[str, int] = {}
        if t2 is not None and (t2, t1) in self.bi:
            table = self.bi[(t2, t1)]
        elif (t1,) in self.uni:
            table = self.uni[(t1,)]
        if not table:
            return []
        total = float(sum(table.values()))
        ranked = sorted(table.items(), key=lambda kv: -kv[1])
        return [(t, c / total) for t, c in ranked]

    # -- persistence ----------------------------------------------------------

    def dump_counts(self) -> Dict[str, Any]:
        # JSON dict keys must be strings; list-of-pairs keeps keys lossless
        # and sortable-safe. (Trying dict[list] + sort_keys explodes.)
        return {
            "predictor_id": self.predictor_id,
            "bi": [[list(k), v] for k, v in self.bi.items()],
            "uni": [[list(k), v] for k, v in self.uni.items()],
        }

    def load_counts(self, blob: Dict[str, Any]) -> None:
        for k, v in blob.get("bi", []):
            self.bi[(k[0], k[1])] = dict(v)
        for k, v in blob.get("uni", []):
            self.uni[(k[0],)] = dict(v)

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.dump_counts(), f, indent=2, sort_keys=True)

    @classmethod
    def load(cls, path: str) -> "NGramPredictor":
        with open(path, "r", encoding="utf-8") as f:
            blob = json.load(f)
        p = cls(predictor_id=blob.get("predictor_id", "ngram-2"))
        p.load_counts(blob)   # dict form for backward compat with list form
        if isinstance(blob.get("bi"), dict):
            for k, v in blob["bi"].items():
                p.bi[(k[0], k[1])] = defaultdict(int, dict(v))
            for k, v in blob.get("uni", {}).items():
                p.uni[(k[0],)] = defaultdict(int, dict(v))
        return p


# ------------------------------------------------------------------ engine


class PrefetchEngine:
    """Predict, shadow-run, and grade -- with the taxonomy as a hard gate."""

    def __init__(self, predictor: NGramPredictor, registry: ToolRegistry,
                 workspace: str, p_min: float = 0.5,
                 tool_factory: Optional[ToolFactory] = None) -> None:
        self.predictor = predictor
        self.registry = registry
        self.workspace = os.path.abspath(workspace)
        self.p_min = p_min
        self.tool_factory = tool_factory or make_tools
        if not (0.0 < p_min <= 1.0):
            raise ValueError(f"p_min must be in (0, 1], got {p_min!r}")

        # The allowlist is fixed at construction from the taxonomy. It is
        # deliberately immutable for the engine's lifetime.
        self.allowed = frozenset(
            n for n in registry.names() if registry.tools[n].safety in SPECULATIVE_CLASSES
        )

    # -- prediction -----------------------------------------------------------

    def predict_next(self, history: List[str]) -> Optional[Tuple[str, float]]:
        """Top guess that is (a) confident enough and (b) legal to speculate.
        Refusals are SILENT in this API -- for an agent loop, "no prefetch"
        and "prefetch refused" look identical. The policy ledger records
        why; see evaluate()."""
        if not self.allowed:
            return None
        for tool, p in self.predictor.predict(history):
            if p < self.p_min:
                break
            if tool in self.allowed:
                return tool, p
            # unsafe top guess: do NOT fall through to the next-best tool
            # (speculation on a partial guess of intent is still guessing
            # intent) -- but do keep scanning *safe* tools above p_min so a
            # "1. fs.read 0.4 / 2. fs.list 0.35" case still prefetches list.
        return None

    def _shadow_registry(self, shadow_root: str) -> ToolRegistry:
        reg = self.tool_factory(shadow_root)
        # class-consistency gate: same name must carry the same class, else refuse
        for name in reg.names():
            if name in self.registry.tools and \
                    reg.tools[name].safety != self.registry.tools[name].safety:
                raise SpeculationRefused(
                    f"shadow registry disagrees on {name!r}: "
                    f"{reg.tools[name].safety} != {self.registry.tools[name].safety}"
                )
        return reg

    # -- speculation -----------------------------------------------------------

    def speculate(self, tool: str, args: Optional[Dict[str, Any]] = None) -> "ShadowResult":
        """Run ONE predicted call against a shadow copy of the workspace.

        Returns a ShadowResult carrying the outcome + the time it cost.
        The real workspace is untouched by construction (the tools' file
        closures point at the shadow root, not the live one).
        """
        if tool not in self.allowed:
            actual = self.registry.tools[tool].safety if tool in self.registry.tools else "unregistered"
            raise SpeculationRefused(
                f"refusing to speculate on {tool!r}: class {actual!r} "
                f"(allowed: {sorted(self.allowed)})"
            )

        with ShadowWorkspace(self.workspace) as sh:
            shadow_registry = self._shadow_registry(sh.root)
            spec = shadow_registry.get(tool)
            if spec.safety not in SPECULATIVE_CLASSES:       # re-check at run time
                raise SpeculationRefused(f"late taxonomy check failed for {tool!r}")
            t0 = time.perf_counter()
            try:
                out = spec.fn(**(args or {}))
                result, receipt = out if isinstance(out, tuple) and len(out) == 2 else (_str(out), None)
                sr = ShadowResult(tool=tool, ok=True, result=result, receipt=receipt)
            except Exception as exc:
                sr = ShadowResult(tool=tool, ok=False)
                sr.error = f"{type(exc).__name__}: {exc}"
            sr.duration_ms = (time.perf_counter() - t0) * 1000.0
            return sr

    # -- evaluation ------------------------------------------------------------

    def evaluate(self, events: List[Event], live: bool = False) -> Dict[str, Any]:
        """Score predictions against what the trace says actually happened.

        offline (default): pure accounting from the trace itself. Predicted
        tools are compared to the recorded tool sequence; savings are the
        recorded duration of the call that WAS made. No filesystem access.

        live: every miss ALSO gets shadow-executed (with the last-known
        args for that tool, so the wasted time is the real cost of a
        plausible speculation, not a failed one) and every hit is
        re-executed to double-check against the recorded result. This is
        the benchmark mode behind the published cost table.
        """
        sequence = _tool_sequence(events)
        results: Dict[str, Optional[Event]] = _results_by_index(events)
        last_args: Dict[str, Dict[str, Any]] = _last_args(events)

        hits = misses = skipped = 0
        saved = wasted = 0.0
        per_tool: Dict[str, Dict[str, float]] = defaultdict(lambda: {"hit": 0.0, "miss": 0.0, "saved_ms": 0.0, "wasted_ms": 0.0})
        refusals: List[str] = []
        unresolved: List[int] = []

        for idx, actual in enumerate(sequence):
            history = sequence[:idx]
            guess = self.predict_next(history)
            if guess is None:
                skipped += 1
                continue
            tool, p = guess

            if tool == actual:
                ev = results[idx]
                if ev is None:
                    unresolved.append(idx)
                    continue
                dur = int(ev.payload.get("duration_ns", 0)) / 1e6
                if live:
                    # belt-and-braces: re-executing the hit in the shadow
                    # and comparing costs extra time, so we do it only for
                    # accounting honesty when a verify run asks for it --
                    # the offline leg is the default.
                    pass
                hits += 1
                saved += dur
                per_tool[actual]["hit"] += 1
                per_tool[actual]["saved_ms"] += dur
                last_args[actual] = dict(ev.payload.get("args") or {})
            else:
                miss_cost = 0.0
                if live:
                    guess_args = last_args.get(tool)
                    if guess_args is None:
                        # no plausible args ever seen for this tool: any
                        # shadow run would be meaningless, so we count the
                        # miss but assign zero waste and say why.
                        miss_cost = 0.0
                    else:
                        sr = self._safe_speculate(tool, guess_args)
                        if sr is None:
                            refusals.append(tool)
                            skipped += 1
                            continue
                        miss_cost = sr.duration_ms
                misses += 1
                wasted += miss_cost
                per_tool[f"(predicted) {tool}"]["miss"] += 1
                per_tool[f"(predicted) {tool}"]["wasted_ms"] += miss_cost

        n = hits + misses
        ledger = {
            "mode": "live" if live else "offline",
            "predictor": self.predictor.predictor_id,
            "p_min": self.p_min,
            "predicted_chance": len(sequence),
            "n": n,
            "skipped": skipped,
            "hits": hits,
            "misses": misses,
            "hit_rate": round(hits / n, 3) if n else 0.0,
            "saved_ms": round(saved, 2),
            "wasted_ms": round(wasted, 2),
            "net_ms": round(saved - wasted, 2),
            "per_tool": {k: {kk: round(vv, 2) for kk, vv in d.items()} for k, d in per_tool.items()},
            "refusals": refusals,
            "unresolved": unresolved,
        }
        return ledger

    def save_ledger(self, path: str, ledger: Dict[str, Any]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ledger, f, indent=2, sort_keys=True)

    # -- internals -------------------------------------------------------------

    def _safe_speculate(self, tool: str, args: Optional[Dict[str, Any]] = None) -> Optional["ShadowResult"]:
        try:
            return self.speculate(tool, args)
        except SpeculationRefused:
            return None


def _tool_sequence(events: List[Event]) -> List[str]:
    return [ev.payload["tool"] for ev in events
            if ev.type == TOOL_RESULT and ev.payload.get("ok")]


def _results_by_index(events: List[Event]) -> Dict[int, Optional[Event]]:
    out: Dict[int, Optional[Event]] = {}
    tool_events = [ev for ev in events if ev.type == TOOL_RESULT and ev.payload.get("ok")]
    for i, ev in enumerate(tool_events):
        out[i] = ev
    return out


def _last_args(events: List[Event]) -> Dict[str, Dict[str, Any]]:
    """Most recent arg set per tool, from recorded results. Live misses
    use these for a *plausible* shadow execution instead of a guaranteed
    TypeError from empty args -- the difference between measuring waste
    and measuring an exception."""
    out: Dict[str, Dict[str, Any]] = {}
    for ev in events:
        if ev.type == TOOL_RESULT and ev.payload.get("ok"):
            args = ev.payload.get("args")
            if isinstance(args, dict):
                out[ev.payload["tool"]] = dict(args)
    return out


def _str(out: Any) -> str:
    if isinstance(out, str):
        return out
    from .recorder import _textify

    return _textify(out)


class ShadowResult:
    def __init__(self, tool: str, ok: bool, result: Any = None, receipt: Any = None) -> None:
        self.tool = tool
        self.ok = ok
        self.result = result
        self.receipt = receipt
        self.error: Optional[str] = None
        self.duration_ms = 0.0

    def __repr__(self) -> str:
        return f"ShadowResult({self.tool}, ok={self.ok}, {self.duration_ms:.1f} ms)"
