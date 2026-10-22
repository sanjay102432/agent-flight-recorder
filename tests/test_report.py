"""HTML report: self-contained, valid JSON payload, no server needed."""
import json
import re

from afr.report import build_report, write_report


def test_report_embeds_parseable_trace(recorded_session, tmp_path):
    store, _rec, _ws = recorded_session
    events = store.read_events()
    html = build_report(events)
    m = re.search(r"const TRACE = (\{.*?\});", html, re.S)
    assert m, "template must embed a JSON payload"
    payload = json.loads(m.group(1))
    assert payload["t0_NS"] > 0
    tool_events = [e for e in payload["events"] if e["kind"] == "tool"]
    assert len(tool_events) >= 6            # intent + result for 3 tool steps
    assert len(payload["plan"]) == 3


def test_report_carries_raw_event_json(recorded_session):
    store, _rec, _ws = recorded_session
    html = build_report(store.read_events())
    assert "receipt" in html                # receipts are visible to auditors
    assert "fs_read" in html or "fs_write" in html


def test_report_tags_unsafe_tools(recorded_session):
    store, _rec, _ws = recorded_session
    html = build_report(store.read_events())
    assert "unsafe" in html                 # the CSS/markup marker exists


def test_write_report_lands_on_disk(recorded_session, tmp_path):
    store, _rec, _ws = recorded_session
    out = write_report(store.read_events(), str(tmp_path / "r.html"))
    import os

    assert os.path.exists(out)
    body = open(out, encoding="utf-8").read()
    assert "<!doctype html>" in body.lower()


def test_empty_trace_report_is_stable():
    html = build_report([])
    assert "(empty)" in html
