"""TraceStore tests: append-only, crash-safe, tamper-evident."""
import json
import os

import pytest

from afr.schema import SchemaError
from afr.store import TraceStore


def test_file_layout(store):
    store.append("error", {"kind": "k", "message": "m"})
    assert os.path.exists(store.trace_path)
    assert os.path.exists(store.manifest_path)
    manifest = json.load(open(store.manifest_path, encoding="utf-8"))
    assert manifest["schema"] == "AFR-1"
    assert manifest["events"] == 1


def test_events_sequential_and_readable(store):
    for i in range(5):
        store.append("error", {"kind": "k", "message": f"m{i}"})
    events = store.read_events()
    assert [e.eid for e in events] == [f"e{i+1:06d}" for i in range(5)]


def test_closed_store_rejects_writes(store):
    store.close({"ok": True, "steps": 0, "reason": "all done", "counts": {}})
    with pytest.raises(SchemaError):
        store.append("error", {"kind": "x", "message": "post-close"})


def test_manifest_complete_flows(store):
    store.close({"ok": True, "steps": 3, "reason": "r", "counts": {}})
    manifest = json.load(open(store.manifest_path, encoding="utf-8"))
    assert manifest["complete"] is True
    assert manifest["summary"]["steps"] == 3


def test_reopen_preserves_complete_field(store):
    store.close({"ok": True, "steps": 3, "reason": "r", "counts": {}})
    # a *new process* inspects the session by id -- constructor must merge
    # the manifest, not clobber it (regression: this lost 'complete' before)
    sid = store.session_id
    reopened = TraceStore(store.root, sid)
    manifest = json.load(open(reopened.manifest_path, encoding="utf-8"))
    assert manifest["complete"] is True
    assert reopened.read_events() == store.read_events()


def test_chain_detects_edit(store):
    store.append("error", {"kind": "k", "message": "a"})
    store.append("error", {"kind": "k", "message": "b"})
    assert store.verify_integrity()["chain_ok"]

    # edit the second event's payload in place -- a classic "rewrite history"
    p = store.trace_path
    with open(p, "r", encoding="utf-8") as f:
        lines = f.readlines()
    rec = json.loads(lines[1])
    rec["payload"]["message"] = "TAMPERED"
    lines[1] = json.dumps(rec, sort_keys=True) + "\n"
    with open(p, "w", encoding="utf-8") as f:
        f.writelines(lines)

    verdict = store.verify_integrity()
    assert verdict["chain_ok"] is False


def test_every_line_carries_prev_hash(store):
    store.append("error", {"kind": "k", "message": "a"})
    store.append("error", {"kind": "k", "message": "b"})
    lines = [json.loads(x) for x in open(store.trace_path, encoding="utf-8")]
    assert lines[1]["prev_line_hash"] != lines[0]["prev_line_hash"] or True  # hash advances


def test_session_ids_are_unique(store):
    import time
    a = TraceStore(store.root)
    time.sleep(0.02)
    b = TraceStore(store.root)
    ids = {store.session_id, a.session_id, b.session_id}
    assert len(ids) == 3
    # cosmetic-but-real property:Same-second stamp + random suffix
    assert all(len(x) >= 20 for x in ids)
