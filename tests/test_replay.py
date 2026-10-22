"""Deterministic replay: the walk works, the drift checks bite."""
import os

from afr.replay import extract_plan, replay
from afr.schema import TOOL_RESULT
from afr.tools import make_tools


def test_recorded_replay_flags_zero_deviations(recorded_session):
    store, _rec, _ws = recorded_session
    report = replay(store.read_events(), mode="recorded")           # no registry
    assert report.ok
    assert report.tool_calls == 3        # list, read, write
    assert report.steps == 4             # incl. the DONE step


def test_plan_extracts_llm_and_receipts(recorded_session):
    store, _rec, _ws = recorded_session
    plan = extract_plan(store.read_events())
    assert plan[0].tool == "fs.list"
    assert plan[0].receipt["kind"] == "fs_read"
    assert plan[2].llm_response.startswith("Plan time")


def test_verify_flags_workspace_drift(recorded_session):
    """The test that shows why verify-mode exists: re-running the read
    steps against a workspace that changed after the run reports drift.
    TWO honest drifts here: fs.list (PLAN.md now exists) and fs.read
    (notes gained a line). The tool result that contrast is the product."""
    store, _rec, ws = recorded_session
    with open(os.path.join(ws, "notes.txt"), "a", encoding="utf-8") as f:
        f.write("late-breaking line added after the run\n")
    registry = make_tools(ws)
    report = replay(store.read_events(), registry=registry, mode="verify", workspace=ws)
    assert not report.ok
    assert report.deviations == 2                                        # fs.list + fs.read
    assert any("'q3 dates:" in n for n in report.notes)                  # old value quoted


def test_verify_skips_unsafe_tools_by_policy(recorded_session):
    store, _rec, ws = recorded_session
    registry = make_tools(ws)
    report = replay(store.read_events(), registry=registry, mode="verify", workspace=ws)
    assert any("skipped verification by policy" in n and "fs.write" in n
               for n in report.notes)


def test_replay_bad_mode_rejected(recorded_session):
    store, _rec, _ws = recorded_session
    import pytest

    with pytest.raises(ValueError):
        replay(store.read_events(), mode="wingardium")


def test_verify_without_registry_rejected(recorded_session):
    store, _rec, _ws = recorded_session
    import pytest

    with pytest.raises(ValueError):
        replay(store.read_events(), mode="verify")
