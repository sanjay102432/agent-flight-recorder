"""TraceStore: append-only JSONL trace files plus a small manifest.

Layout on disk (one session = one directory)::

    trace_store/
      20261023-020758-fe3a/        <- session id (UTC stamp + short random)
        trace.jsonl                 <- the events, one JSON object per line
        manifest.json               <- metadata only; no event data lives here

Why JSONL and not SQLite/parquet:
  1. an append-only file means a crash can at worst truncate the last line
  2. `grep` and `jq` work directly on the file, so people *actually use* traces
  3. hash-chaining per line is trivial: each event embeds a digest of its
     own payload, and the manifest embeds a running digest of the file --
     editing or deleting one event breaks the chain visibly.

The chain is what `verify_integrity()` checks and what `replay` walks.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import secrets
from typing import Any, Dict, List, Optional

from .schema import Event, SchemaError, validate


def _now_ns() -> int:
    return int(_dt.datetime.now().timestamp() * 1_000_000_000)


def _now_human() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _new_session_id() -> str:
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-")
    return stamp + secrets.token_hex(2)


def _short_hash(text: str, n: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


class TraceStore:
    """Owns one session directory; densifies validation, sequencing, chaining."""

    def __init__(self, root: str, session_id: Optional[str] = None) -> None:
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self.session_id = session_id or _new_session_id()
        self.dir = os.path.join(self.root, self.session_id)
        os.makedirs(self.dir, exist_ok=True)
        self.trace_path = os.path.join(self.dir, "trace.jsonl")
        self.manifest_path = os.path.join(self.dir, "manifest.json")
        self.closed = False
        # Re-opening an existing session (crash recovery) continues, never truncates.
        self._count = len(self.read_events())
        old: Dict[str, Any] = {}
        if os.path.exists(self.manifest_path):
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    old = json.load(f)
            except json.JSONDecodeError:
                old = {}
        # A reopen marks the resume point in the manifest -- but only when the
        # store actually writes again. Read-only opens (replay/verify/diff view)
        # must not touch the file at all: two concurrent readers, or a reader
        # inside a writer's atomic swap, then never collide on Windows.
        self._pending_resume = None
        if self._count and old.get("trace_content_hash") != self.read_last_chain_hash():
            self._pending_resume = old.get("trace_content_hash") or self.read_last_chain_hash()

    # -- ids -----------------------------------------------------------------

    @staticmethod
    def new_event_id(seq: int) -> str:
        return f"e{seq:06d}"

    # -- writing -------------------------------------------------------------

    def append(self, type_: str, payload: Dict[str, Any], depth: int = 0,
               run_id: str = "r0", ts_ns: Optional[int] = None) -> str:
        if self.closed:
            raise SchemaError("trace already closed -- open a new session to record more")
        ev = Event(
            type=type_,
            ts_ns=ts_ns if ts_ns is not None else _now_ns(),
            eid=self.new_event_id(self._count + 1),
            run_id=run_id,
            session_id=self.session_id,
            depth=depth,
            payload=dict(payload),
        )
        validate(ev)
        record = ev.to_dict()
        record["prev_line_hash"] = self.read_last_chain_hash()
        line = json.dumps(record, sort_keys=True, ensure_ascii=True)

        with open(self.trace_path, "a", encoding="utf-8", newline="") as f:
            f.write(line + "\n")

        self._count += 1
        extra: Dict[str, Any] = {"events": self._count}
        if self._pending_resume is not None:
            extra["resume_from"] = self._pending_resume
            self._pending_resume = None
        self._write_manifest(extra=extra)
        return ev.eid

    # -- reading -------------------------------------------------------------

    def read_events(self) -> List[Event]:
        if not os.path.exists(self.trace_path):
            return []
        events: List[Event] = []
        with open(self.trace_path, "r", encoding="utf-8") as f:
            for i, ln in enumerate(f, 1):
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    raw = json.loads(ln)
                except json.JSONDecodeError as exc:
                    raise SchemaError(f"trace line {i} is not valid JSON: {exc}") from exc
                raw.pop("prev_line_hash", None)
                events.append(Event.from_dict(raw))
        return events

    def read_last_chain_hash(self) -> Optional[str]:
        """Running content digest of trace.jsonl written into the manifest.
        implements "did this file change since I last looked" cheaply: on
        read, re-digesting the file must reproduce it."""
        if not os.path.exists(self.trace_path):
            return None
        h = hashlib.sha256()
        with open(self.trace_path, "rb") as f:
            for block in iter(lambda: f.read(65536), b""):
                h.update(block)
        return h.hexdigest()[:16]

    # -- manifest ------------------------------------------------------------

    def _write_manifest(self, extra: Dict[str, Any]) -> None:
        data: Dict[str, Any] = {
            "schema": "AFR-1",
            "schema_version": 1,
            "created_utc": _now_human(),
            "session_id": self.session_id,
            "trace_content_hash": self.read_last_chain_hash(),
        }
        data.update(extra)
        tmp = self.manifest_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, self.manifest_path)  # atomic on all OSes we care about

    def close(self, summary: Dict[str, Any]) -> None:
        """Appends run.end and marks the manifest complete."""
        self.append("run.end", {"summary": summary, "ok": bool(summary.get("ok"))})
        self.closed = True
        self._write_manifest(extra={
            "events": self._count, "complete": True, "summary": summary,
        })

    # -- verification --------------------------------------------------------

    def verify_integrity(self) -> Dict[str, Any]:
        """Three checks, each catching a differently-shaped attack:
          1. monotonic: events in time order (deletion reinsertion breaks it)
          2. chain: recomputed content digest == manifest value
          3. complete: manifest says the trace closed cleanly
        """
        events = self.read_events()
        monotonic = all(b.ts_ns >= a.ts_ns for a, b in zip(events, events[1:]))
        manifest: Dict[str, Any] = {}
        if os.path.exists(self.manifest_path):
            with open(self.manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        actual = self.read_last_chain_hash()
        expected = manifest.get("trace_content_hash")
        chain_ok = (actual == expected) if (actual and expected) else True
        return {
            "events": len(events),
            "monotonic_ts": monotonic,
            "chain_ok": chain_ok,
            "complete": bool(manifest.get("complete")),
            "ok": monotonic and chain_ok,
            "manifest": manifest,
        }
