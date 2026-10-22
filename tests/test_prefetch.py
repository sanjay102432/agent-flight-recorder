"""Prefetch engine tests: safe-by-construction, honest ledger, shadow isolation."""
import os

import pytest

from afr.prefetch import (
    NGramPredictor, PrefetchEngine, ShadowWorkspace, SpeculationRefused,
)
from afr.recorder import Recorder
from afr.tools import make_tools
from afr.demo_agent import DemoAgent, MockProvider, planner_script


# ------------------------------------------------------------------ predictor

def test_predictor_learns_two_step_pattern():
    p = NGramPredictor()
    for _ in range(20):
        p.fit_tool_sequence(["fs.list", "fs.read", "fs.write"])
    guesses = dict(p.predict(["fs.list", "fs.read"]))
    assert guesses["fs.write"] > 0.99


def test_predictor_unigram_fallback():
    p = NGramPredictor()
    for _ in range(10):
        p.fit_tool_sequence(["fs.read", "fs.list"])
    guesses = dict(p.predict(["fs.read"]))           # bigram key never seen
    assert guesses["fs.list"] > 0.99


def test_predictor_round_trips_through_disk(tmp_path):
    p = NGramPredictor()
    p.fit_tool_sequence(["fs.list", "fs.read", "fs.list", "fs.read"])
    path = tmp_path / "model.json"
    p.save(str(path))
    q = NGramPredictor.load(str(path))
    assert dict(q.predict(["fs.read"])) == dict(p.predict(["fs.read"]))


# ------------------------------------------------------------------ shadow FS

def test_shadow_writes_are_discarded(ws):
    before = set(os.listdir(ws))
    with ShadowWorkspace(ws) as shadow:
        with open(os.path.join(shadow.root, "notes.txt"), "w", encoding="utf-8") as f:
            f.write("shadow only")
        with open(os.path.join(shadow.root, "new.md"), "w", encoding="utf-8") as f:
            f.write("more shadow")
    assert set(os.listdir(ws)) == before


def test_shadow_registry_rebinds_tools(ws):
    """THE key safety property: same tool name, same class, DIFFERENT root.
    A tool executed "in the shadow" must read/write shadow files only."""
    live = make_tools(ws)
    with ShadowWorkspace(ws) as sh:
        shadow = make_tools(sh.root)
        assert shadow.tools["fs.read"].safety == live.tools["fs.read"].safety
        # write through the shadow root: live workspace must not change
        result, receipt = shadow.tools["fs.write"].fn(rel_path="made-in-shadow.md", text="x")
        assert receipt["kind"] == "fs_write"
    assert not os.path.exists(os.path.join(ws, "made-in-shadow.md"))


def test_shadow_no_merge_even_on_error(ws):
    before = set(os.listdir(ws))
    shadow = ShadowWorkspace(ws)
    try:
        with open(os.path.join(shadow.root, "scratch.md"), "w", encoding="utf-8") as f:
            f.write("will vanish")
    finally:
        shadow.cleanup()
    assert set(os.listdir(ws)) == before


# ------------------------------------------------------------------- gate

def test_engine_refuses_to_speculate_writes(recorded_session):
    store, _rec, ws = recorded_session
    engine = PrefetchEngine(NGramPredictor(), make_tools(ws), ws)
    with pytest.raises(SpeculationRefused):
        engine.speculate("fs.write", {"rel_path": "booby.txt", "text": "no"})
    # and the file really does not exist (refusal did not partially act!)
    assert not os.path.exists(os.path.join(ws, "booby.txt"))


def test_engine_refuses_unregistered_tools(recorded_session):
    store, _rec, ws = recorded_session
    engine = PrefetchEngine(NGramPredictor(), make_tools(ws), ws)
    with pytest.raises(SpeculationRefused):
        engine.speculate("network.http_get")


def test_engine_allowlist_excludes_unsafe(recorded_session):
    store, _rec, ws = recorded_session
    engine = PrefetchEngine(NGramPredictor(), make_tools(ws), ws)
    assert "fs.write" not in engine.allowed
    assert "shell" not in engine.allowed
    assert {"fs.read", "fs.list"} <= set(engine.allowed)


def test_engine_predict_next_never_returns_write(recorded_session):
    store, _rec, ws = recorded_session
    predictor = NGramPredictor()
    for _ in range(20):
        predictor.fit_tool_sequence(["fs.read", "fs.write"])   # models a write-heavy agent
    engine = PrefetchEngine(predictor, make_tools(ws), ws)
    guess = engine.predict_next(["fs.read"])
    assert guess is None  # top guess is fs.write -> refused, nothing suggested


def test_engine_requires_positive_p_min(recorded_session):
    store, _rec, ws = recorded_session
    with pytest.raises(ValueError):
        PrefetchEngine(NGramPredictor(), make_tools(ws), ws, p_min=0.0)


# -------------------------------------------------------------------- cost model

def test_offline_ledger_is_perfect_assembly(recorded_session):
    """Trained on the trace itself, the planner script is a repeating
    prefix chain -- the ledger should see real hits on the second call on."""
    store, _rec, ws = recorded_session
    events = store.read_events()
    predictor = NGramPredictor.from_trace(events)
    engine = PrefetchEngine(predictor, make_tools(ws), ws)
    ledger = engine.evaluate(events)
    assert ledger["mode"] == "offline"
    assert ledger["hits"] >= 1
    assert ledger["saved_ms"] > 0


def test_live_ledger_counts_real_waste(ws, tmp_path):
    """On a trace whose pattern the predictor CANNOT know (fresh model),
    every prediction attempt is a skip; train on the trace and the exact
    same data is all hits. The gap between 'fresh predictor' and
    'trained predictor' is the prefetch story in two numbers."""
    from afr.store import TraceStore

    registry = make_tools(ws)
    provider = MockProvider(planner_script())
    store = TraceStore(str(tmp_path / "store"), "ledger-test")
    rec = Recorder(store, registry, agent_meta={"name": "t", "provider": "mock", "model": "m"})
    DemoAgent(rec, provider).run("plan")
    events = store.read_events()

    fresh = NGramPredictor()                       # knows nothing
    ledger = PrefetchEngine(fresh, registry, ws).evaluate(events)
    assert ledger["hits"] == 0
    assert ledger["skipped"] >= 1

    trained = PrefetchEngine(NGramPredictor.from_trace(events), registry, ws).evaluate(events)
    assert trained["hits"] >= 1
    assert trained["saved_ms"] > 0


def test_ledger_is_serializable(recorded_session, tmp_path):
    store, _rec, ws = recorded_session
    events = store.read_events()
    engine = PrefetchEngine(NGramPredictor.from_trace(events), make_tools(ws), ws)
    ledger = engine.evaluate(events)
    out = tmp_path / "ledger.json"
    engine.save_ledger(str(out), ledger)
    import json

    clone = json.load(open(out, encoding="utf-8"))
    assert clone["net_ms"] == round(ledger["saved_ms"] - ledger["wasted_ms"], 2)
