"""Additive-only agent runner: `afr record` on scenario builder must show
that the taxonomy is enforced in the ledger (a write prediction is
refused, not executed)."""
import json
import os

from afr.cli import main
from afr.demo_agent import DemoAgent, MockProvider, builder_script
from afr.prefetch import NGramPredictor, PrefetchEngine
from afr.recorder import Recorder
from afr.store import TraceStore
from afr.tools import make_tools, READ_ONLY, WRITE


def test_builder_scenario_records_three_reads_one_write(ws, tmp_path):
    store = TraceStore(str(tmp_path / "s"), "builder")
    rec = Recorder(store, make_tools(ws), agent_meta={"name": "t", "provider": "mock", "model": "m"})
    outcome = DemoAgent(rec, MockProvider(builder_script())).run("builder")
    assert outcome["ok"]
    tools = [e.payload["tool"] for e in store.read_events() if e.type == "tool.result"]
    assert tools == ["fs.list", "fs.read", "fs.read", "fs.write"]


def test_predictor_prefers_read_bias_after_training(ws, tmp_path):
    """The builder pattern is list -> read -> read -> write. Trained on it,
    predictions for early steps favour fs.read -- but the write is NEVER the
    engine's suggestion because it isn't in the allowlist."""
    store = TraceStore(str(tmp_path / "s"), "b2")
    rec = Recorder(store, make_tools(ws), agent_meta={"name": "t", "provider": "mock", "model": "m"})
    DemoAgent(rec, MockProvider(builder_script())).run("b2")
    events = store.read_events()

    predictor = NGramPredictor.from_trace(events)
    engine = PrefetchEngine(predictor, make_tools(ws), ws)
    ledger = engine.evaluate(events)
    # hits land under the actual tool name; nothing fs.write-shaped may
    # appear anywhere -- refusal is silent, and the ledger proves it
    assert any(ledger["per_tool"].get("fs.read", {}).get("hit", 0) >= 1 for _ in [0])
    assert all("fs.write" not in k for k in ledger["per_tool"])
