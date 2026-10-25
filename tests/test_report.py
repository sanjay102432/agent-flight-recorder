"""HTML + markdown + terminal reports: self-contained, no server needed."""
import json
import re

import pytest

from afr.demo_agent import DemoAgent, MockProvider, failing_script, build_workspace
from afr.recorder import Recorder
from afr.report import (
    build_markdown_report, build_report, build_terminal_report, write_report,
)
from afr.store import TraceStore
from afr.tools import make_tools


@pytest.fixture
def recorded_failure(tmp_path):
    """A recorded session whose tool call fails on step 1 (ghost.txt)."""
    ws = str(tmp_path / "ws")
    build_workspace(ws)
    store = TraceStore(str(tmp_path / "store"))
    rec = Recorder(store, make_tools(ws),
                   agent_meta={"name": "demo", "provider": "mock-scripted",
                               "model": "mock-mini-1", "scenario": "failing"})
    DemoAgent(rec, MockProvider(failing_script())).run("break")
    return store, ws


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


def test_empty_trace_markdown_and_terminal_are_stable():
    md = build_markdown_report([])
    assert "empty trace" in md.lower()
    term = build_terminal_report([])
    assert "(empty trace" in term


# -- markdown report ---------------------------------------------------------

def test_markdown_report_shows_plan_receipts_and_commands(recorded_session):
    store, _rec, _ws = recorded_session
    md = build_markdown_report(store.read_events(), trace_path="trace.jsonl")
    assert md.startswith("# Agent Flight Report")
    assert "## At a glance" in md
    assert "step by step" in md                   # the plan section header
    assert "`fs.list` (read-only)" in md          # tool + safety class per step
    assert "111 bytes" in md                      # receipt detail (fs.read)
    assert "**created**" in md or "created" in md # fs.write side effect
    assert "python -m afr verify" in md           # the check-it-yourself tail


def test_markdown_report_of_failed_session_shows_the_error(recorded_failure):
    store, _ws = recorded_failure
    md = build_markdown_report(store.read_events(), trace_path="trace.jsonl")
    assert "**FAILED**" in md
    assert "FileNotFoundError" in md
    assert "## Errors" in md


# -- terminal report ---------------------------------------------------------

def test_terminal_report_show_steps_and_receipts(recorded_session):
    store, _rec, _ws = recorded_session
    term = build_terminal_report(store.read_events())
    assert "AGENT FLIGHT REPORT" in term
    assert "step 1" in term and "step 3" in term
    assert "fs.list" in term and "fs.write" in term
    assert "SIDE EFFECTS" in term                 # aggregate is visible
    assert "created" in term
    assert "ERRORS              none" in term


def test_terminal_report_of_failed_session_shows_the_error(recorded_failure):
    store, _ws = recorded_failure
    term = build_terminal_report(store.read_events())
    assert "FAILED" in term
    assert "[ERROR   ]" in term
    assert "FileNotFoundError" in term
    assert "ERRORS" in term
