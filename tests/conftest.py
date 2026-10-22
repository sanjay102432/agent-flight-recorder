"""Shared fixtures: disposable workspaces + a recorded session per test."""
import os

import pytest

from afr.recorder import Recorder
from afr.store import TraceStore
from afr.tools import make_tools
from afr.demo_agent import DemoAgent, MockProvider, planner_script, build_workspace


@pytest.fixture
def ws(tmp_path):
    workspace = tmp_path / "ws"
    build_workspace(str(workspace))
    return str(workspace)


@pytest.fixture
def store(tmp_path):
    return TraceStore(str(tmp_path / "store"))


@pytest.fixture
def recorder(store, ws):
    return Recorder(store, make_tools(ws),
                    agent_meta={"name": "test", "provider": "mock", "model": "test-1"})


@pytest.fixture
def recorded_session(store, ws):
    """A real recorded planner session, returned as (store, recorder)."""
    registry = make_tools(ws)
    provider = MockProvider(planner_script())
    rec = Recorder(store, registry, agent_meta={"name": "demo", "provider": provider.name,
                                                "model": provider.model})
    agent = DemoAgent(rec, provider)
    outcome = agent.run("plan the workspace")
    assert outcome["ok"]
    return store, rec, ws


def file_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def exists(path: str) -> bool:
    return os.path.exists(path)
