"""Recorder tests: intent/result pairing, receipts, error-as-fact."""
import pytest

from afr.recorder import ToolExecutionError, parse_tool_call
from afr.schema import (
    LLM_RESPONSE, LLM_REQUEST, RUN_END, RUN_START,
    TOOL_INTENT, TOOL_RESULT,
)


# -- the happy path ---------------------------------------------------------

def test_llm_turn_records_request_and_response(recorder):
    recorder.llm(lambda prompt: "a model answer", step=1, prompt="a question")
    events = recorder.store.read_events()
    kinds = [e.type for e in events]
    assert LLM_REQUEST in kinds and LLM_RESPONSE in kinds
    resp = next(e for e in events if e.type == LLM_RESPONSE)
    assert resp.payload["response"] == "a model answer"
    assert "tokens_in" in resp.payload  # fallback counts present even if caller didn't know them


def test_tool_call_records_intent_and_result(recorder):
    result, _raw, ok, receipt = recorder.tool_call(1, "fs.read", {"rel_path": "notes.txt"})
    events = recorder.store.read_events()
    intent = events[0] if events[0].type == TOOL_INTENT else events[1]
    done = next(e for e in events if e.type == TOOL_RESULT)
    assert intent.payload["tool"] == "fs.read"
    assert done.payload["result"].startswith("q3")
    assert done.payload["args"] == {"rel_path": "notes.txt"}  # result repeats args: self-contained
    assert receipt["kind"] == "fs_read"
    assert ok is True


def test_open_close_recorded(recorder):
    recorder.open_run()
    recorder.close_run(ok=True, reason="nothing to do")
    kinds = [e.type for e in recorder.store.read_events()]
    assert kinds[0] == RUN_START and kinds[-1] == RUN_END


# -- the error path ---------------------------------------------------------

def test_tool_failure_recorded_then_raised(recorder):
    with pytest.raises(ToolExecutionError) as excinfo:
        recorder.tool_call(1, "fs.read", {"rel_path": "ghost.txt"})
    done = [e for e in recorder.store.read_events() if e.type == TOOL_RESULT][-1]
    assert done.payload["ok"] is False
    assert "FileNotFoundError" in done.payload["result"]
    assert excinfo.value.tool == "fs.read"


def test_unknown_tool_rejected(recorder):
    with pytest.raises(KeyError):
        recorder.tool_call(1, "not_a_tool", {})


# -- sandboxing ------------------------------------------------------------

def test_paths_cannot_escape_workspace(recorder, ws):
    for evil in ["..", "subdir/../../x.txt", "C:/Windows/evil.txt", "/abs/path"]:
        with pytest.raises((ToolExecutionError, ValueError, KeyError)):
            recorder.tool_call(1, "fs.read", {"rel_path": evil})


def test_write_records_existed_before(recorder, ws, tmp_path):
    result, _r, _ok, receipt = recorder.tool_call(1, "fs.write",
                                                  {"rel_path": "new.md", "text": "hi"})
    assert receipt["existed_before"] is False
    result2, _r2, _ok2, receipt2 = recorder.tool_call(2, "fs.write",
                                                      {"rel_path": "new.md", "text": "hi again"})
    assert receipt2["existed_before"] is True
    assert (tmp_path / "ws" / "new.md").read_text(encoding="utf-8") == "hi again"


# -- the protocol parser -----------------------------------------------------

def test_parse_tool_call_basic():
    text = 'thought...\n```json {"tool": "fs.read", "args": {"rel_path": "x"}} ```'
    assert parse_tool_call(text) == ("fs.read", {"rel_path": "x"})


def test_parse_tool_call_rejects_junk():
    assert parse_tool_call("justtalk") == (None, None)
    assert parse_tool_call('```json {"tool": 42}```') == (None, None)


def test_parse_tool_call_accepts_null_tools():
    assert parse_tool_call('{"tool": null, "args": {}}') == (None, None)
