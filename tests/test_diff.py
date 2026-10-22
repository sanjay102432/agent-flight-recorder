"""Session diffing: align on plan identity, then explain the drift."""
import os

from afr.diff import diff_sessions, digest_steps
from afr.demo_agent import DemoAgent, MockProvider, auditor_script, planner_script
from afr.recorder import Recorder
from afr.store import TraceStore
from afr.tools import make_tools


def _record(ws, root, sid, script):
    registry = make_tools(ws)
    provider = MockProvider(script())
    store = TraceStore(root, sid)
    rec = Recorder(store, registry, agent_meta={"name": "t", "provider": provider.name,
                                                "model": provider.model})
    DemoAgent(rec, provider).run("task")
    return store.read_events()


def test_digest_reduces_to_tool_sequence(ws, tmp_path):
    events = _record(ws, str(tmp_path / "s"), "one", planner_script)
    steps = digest_steps(events)
    assert [s.tool for s in steps] == ["fs.list", "fs.read", "fs.write"]


def test_identical_prefix_diffs_with_workspace_change(ws, tmp_path):
    """Run 1 and run 2 share fs.list+fs.read; run 1 also writes PLAN.md.
    Between runs, PLAN.md *enters the workspace* -- so fs.list legitimately
    returns different results. The diff must align the shared prefix and
    report the drift inside the match, not fabricate a plan change."""
    a = _record(ws, str(tmp_path / "s"), "a", planner_script)
    b = _record(ws, str(tmp_path / "s"), "b", auditor_script)
    td = diff_sessions(a, b, label_a="a", label_b="b")
    assert td.similarity >= 0.5
    assert td.matches >= 2                     # the aligned fs.list + fs.read
    assert any(d["tool"] == "fs.list" for d in td.result_drifts)  # listing changed: honest
    # session a kept going: its unaligned fs.write appears as an a-only row
    assert any((xa is not None and xb is None and xa.tool == "fs.write")
               for xa, xb in td.alignment)


def test_kept_going_marks_extra_steps(ws, tmp_path):
    a = _record(ws, str(tmp_path / "s"), "a", auditor_script)   # 2 tools, stops
    b = _record(ws, str(tmp_path / "s"), "b", planner_script)   # 3 tools
    td = diff_sessions(b, a, label_b="-stop")  # session that stopped is 'b'
    assert td.kept_going == []                 # no extra steps in b


def test_identical_sessions_full_similarity(ws, tmp_path):
    a = _record(ws, str(tmp_path / "s"), "a", auditor_script)
    b = _record(ws, str(tmp_path / "s"), "b", auditor_script)
    td = diff_sessions(a, b, label_a="a", label_b="b")
    assert td.similarity == 1.0                # same plan even if listings drift
    assert td.arg_drifts == []


def test_dict_output_schema(ws, tmp_path):
    a = _record(ws, str(tmp_path / "s"), "a", planner_script)
    b = _record(ws, str(tmp_path / "s"), "b", auditor_script)
    d = diff_sessions(a, b).to_dict()
    for key in ("similarity", "matches", "arg_drifts", "result_drifts", "alignment"):
        assert key in d
    assert all(set(x) >= {"a", "b", "tool"} for x in d["alignment"])
