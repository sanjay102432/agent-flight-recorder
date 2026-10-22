"""AFR-1 schema contract tests: what every writer must never break."""
import json

import pytest

from afr.schema import (
    RUN_END, RUN_START, TOOL_RESULT,
    Event, SchemaError, read_trace, text_sha256, validate,
)


def _ev(type_: str, payload: dict, ts: int = 5) -> Event:
    return Event(type=type_, ts_ns=ts, eid="e000001", run_id="r0", session_id="s", payload=payload)


# -- validation -------------------------------------------------------------

def test_unknown_type_rejected():
    with pytest.raises(SchemaError):
        validate(_ev("banana", {}))


def test_required_fields_enforced_per_type():
    # run.start needs agent+mode
    with pytest.raises(SchemaError):
        validate(_ev("run.start", {}))
    ok = validate(_ev("run.start", {"agent": {"a": 1}, "mode": "record"}))
    assert ok.payload["mode"] == "record"


def test_tool_result_requires_duration_and_receipt():
    payload = {"step": 1, "tool": "fs.read", "args": {}, "receipt": {}, "result_kind": "text",
               "result": "x", "duration_ns": 5, "ok": True}
    assert validate(_ev("run.end", {"summary": {}}))
    validate(_ev(TOOL_RESULT, payload))  # must not raise
    bad = dict(payload)
    bad.pop("duration_ns")
    with pytest.raises(SchemaError):
        validate(_ev(TOOL_RESULT, bad))


def test_prefetch_result_requires_correct():
    with pytest.raises(SchemaError):
        validate(_ev("prefetch.result", {"step": 1, "tool": "fs.read",
                                         "correct": "yes", "shadow_result_kind": "text"}))


def test_negative_timestamps_rejected():
    with pytest.raises(SchemaError):
        validate(_ev("error", {"kind": "x", "message": "m"}, ts=-1))


def test_empty_ids_rejected():
    ev = Event(type="error", ts_ns=1, eid="e000001", run_id="", session_id="s", payload={"kind": "k", "message": "m"})
    with pytest.raises(SchemaError):
        validate(ev)


# -- serialization -------------------------------------------------------------

def test_json_round_trip_preserves_payload():
    ev = _ev(TOOL_RESULT, {"step": 2, "tool": "fs.read", "args": {"rel_path": "a.txt"},
                           "receipt": {"kind": "fs_read"}, "result_kind": "text",
                           "result": "hello", "duration_ns": 99, "ok": True})
    ev.type = TOOL_RESULT
    clone = Event.from_json(ev.to_json())
    assert clone == ev


def test_unicode_survives_ascii_json():
    ev = _ev(RUN_END, {"summary": {"reason": "héllo→wörld→ok"}})
    clone = Event.from_json(ev.to_json())
    assert clone.payload["summary"]["reason"] == "héllo→wörld→ok"


def test_version_mismatch_raises():
    raw = {"v": 999, "type": "error", "ts_ns": 1, "eid": "e1", "run_id": "r", "session_id": "s", "payload": {}}
    with pytest.raises(SchemaError):
        Event.from_dict(raw)


# -- read_trace + monotonic ---------------------------------------------------

def test_read_trace_rejects_garbage(tmp_path):
    p = tmp_path / "broken.jsonl"
    p.write_text('{"type": "error"}\nnot json at all\n', encoding="utf-8")
    with pytest.raises(SchemaError):
        read_trace(str(p))


def test_read_trace_skips_blank_lines(tmp_path):
    a = _ev(RUN_START, {"agent": {"a": 1}, "mode": "record"}, ts=1)
    b = _ev(RUN_END, {"summary": {}}, ts=2)
    b.type = RUN_END
    p = tmp_path / "t.jsonl"
    p.write_text(a.to_json() + "\n\n" + b.to_json() + "\n", encoding="utf-8")
    events = read_trace(str(p))
    assert [e.type for e in events] == [RUN_START, RUN_END] == ["run.start", "run.end"]


# -- hashes --------------------------------------------------------------------

def test_text_sha_stable_and_salted():
    h1 = text_sha256("abc")
    assert h1 == text_sha256("abc")
    assert h1 != text_sha256("abd")
    assert len(h1) == 16


def test_stable_across_json_round_trips():
    text = "line1\nline2\ttab"
    h = text_sha256(text)
    clone = json.loads(json.dumps({"t": text}))["t"]
    assert text_sha256(clone) == h
