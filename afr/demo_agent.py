"""MockProvider + DemoAgent: a working agent that runs *entirely offline*.

Why a scripted model instead of "real" GPT calls?
  * zero infra: this demo runs on a no-GPU, no-credits laptop (the author's)
  * determinism: tests must be reproducible to be honest
  * the recorder doesn't care what produced the text -- llm() records
    provider/model/generation facts transparently either way, and when
    real providers are added later (OpenAI-compatible endpoints in
    providers.py) they drop into the same seam without schema changes.

The scripted provider plays an agent solving the tiny task: "summarize the
workspace and write PLAN.md".  It "decides" on tool calls through a frozen
response table, exactly the way a recorded model would.

The DemoAgent loop itself is deliberately braindead:

    for step in range(max_steps):
        prompt = build_prompt(...)
        text   = recorder.llm(provider, step, prompt)
        tool, args = parse_tool_call(text)
        if tool is None:  break-and-count
        result = recorder.tool_call(...)  # may raise ToolExecutionError
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .recorder import Recorder, ToolExecutionError, parse_tool_call


# ---------------------------------------------------------------- provider


class MockProvider:
    """A canned model. Responds with the first response whose 'match' rules
    all agree with the prompt. Responses are recorded verbatim afterwards --
    so later replay never re-runs Python for them, it just reads the trace.

    Because every response is emitted the *same way* a live LLM would emit
    it (one string), the agent cannot tell this from a real provider --
    which is the property that makes the trace schema provider-neutral.
    """

    name = "mock-scripted"

    def __init__(self, responses: List[Dict[str, Any]], model: str = "mock-mini-1") -> None:
        self.responses = responses
        self.model = model

    def respond_to(self, prompt: str, step: int) -> str:
        for rule in self.responses:
            if rule.get("step") is not None and rule["step"] != step:
                continue
            if rule.get("contains") and rule["contains"] not in prompt:
                continue
            return rule["reply"]
        return '{"tool": null, "args": {}}  # no canned response matched'


# ---------------------------------------------------------------- agent


SYSTEM_INSTRUCTIONS = (
    "You are a file-housekeeping agent. Work ONLY with the provided tools. "
    "Every reply must end with one tool call in this JSON form:\n"
    '```json {"tool": "fs.read", "args": {"rel_path": "notes.txt"}} ```\n'
    "When you are done, do not call a tool and write DONE."
)


class DemoAgent:
    """Simple loop: prompt -> llm -> parse tool call -> call -> repeat."""

    def __init__(self, recorder: Recorder, provider: MockProvider, max_steps: int = 8) -> None:
        self.recorder = recorder
        self.provider = provider
        self.max_steps = max_steps
        self.history: List[Tuple[int, str]] = []
        self.safety_lock: Dict[str, List[str]] = {}

    def run(self, task: str) -> Dict[str, Any]:
        rec = self.recorder
        rec.open_run()
        outcome: Dict[str, Any] = {"ok": True, "steps": 0, "final": ""}
        for step in range(1, self.max_steps + 1):
            rec.start_step(step)
            prompt = self._build_prompt(task, step)
            try:
                text = rec.llm(lambda pr: self.provider.respond_to(pr, step), step, prompt)
            except Exception as exc:                       # llm errors end the run
                rec.error("llm_error", str(exc), step=step)
                rec.end_step(step, status="error")
                outcome.update(ok=False, final=str(exc))
                rec.close_run(ok=False, reason="llm_error")
                return outcome
            self.history.append((step, text))

            tool, args = parse_tool_call(text)
            if tool is None:
                rec.end_step(step, status="done")
                outcome.update(final=text.strip(), steps=step)
                rec.close_run(ok=True, reason="agent declared done")
                return outcome

            try:
                result, _raw, ok, receipt = rec.tool_call(step, tool, args)
            except ToolExecutionError as exc:
                rec.end_step(step, status="tool_error")
                # after one failed tool call, stop -- demo policy shows the
                # behaviour a sane default loop needs and the trace shows it.
                outcome.update(ok=False, final=exc.detail, steps=step)
                rec.close_run(ok=False, reason=f"tool '{exc.tool}' failed")
                return outcome
            rec.end_step(step, status="ok")
            outcome["steps"] = step

        outcome["final"] = "(max_steps reached)"
        rec.close_run(ok=True, reason="max_steps reached")
        return outcome

    def _build_prompt(self, task: str, step: int) -> str:
        trace_excerpt = "\n".join(
            f"step {s}: {t[:120]}" for s, t in self.history[-3:]
        )
        return (
            f"{SYSTEM_INSTRUCTIONS}\n\nTASK: {task}\nTOOLS: {self.recorder.registry.names()}\n"
            f"RECENT STEPS:\n{trace_excerpt or '(none yet)'}\nSTEP #{step}: respond with a tool call or DONE."
        )


# ------------------------------------------------------------------ scenarios


def planner_script() -> List[Dict[str, Any]]:
    """Scenario A: read a file then write a plan. Produces read-only traffic
    that prefetch wants to hit, plus one write so the safety taxonomy shows.
    """
    return [
        {"step": 1, "reply": 'Looking at the workspace files first.\n'
                             '```json {"tool": "fs.list", "args": {"rel_dir": "."}} ```'},
        {"step": 2, "reply": 'Reading the notes.\n'
                             '```json {"tool": "fs.read", "args": {"rel_path": "notes.txt"}} ```'},
        {"step": 3, "reply": 'Plan time.\n'
                             '```json {"tool": "fs.write", "args": {"rel_path": "PLAN.md", '
                             '"text": "# Plan\\n- read notes\\n- summarize\\n- file plan\\n"}} ```'},
        {"step": 4, "reply": "DONE"},
    ]


def auditor_script() -> List[Dict[str, Any]]:
    """Scenario B: reads two files, then desists. Purely read-only: ideal to
    diff against scenario A (same prefix, then diverges)."""
    return [
        {"step": 1, "reply": '>(fs.list first, same as always)\n'
                             '```json {"tool": "fs.list", "args": {"rel_dir": "."}} ```'},
        {"step": 2, "reply": 'Reading nuance.\n'
                             '```json {"tool": "fs.read", "args": {"rel_path": "notes.txt"}} ```'},
        {"step": 3, "reply": "DONE"},
    ]


def builder_script() -> List[Dict[str, Any]]:
    """Scenario D: three reads (list → a → b) then one write. The read-run
    prefix is what the prefetch cost table measures; the write at the end
    proves the engine refuses to speculate on it (ledger shows a refusal)."""
    return [
        {"step": 1, "reply": 'Mapping the workspace.\n'
                             '```json {"tool": "fs.list", "args": {"rel_dir": "."}} ```'},
        {"step": 2, "reply": 'Reading notes.\n'
                             '```json {"tool": "fs.read", "args": {"rel_path": "notes.txt"}} ```'},
        {"step": 3, "reply": 'Reading changelog.\n'
                             '```json {"tool": "fs.read", "args": {"rel_path": "changelog.md"}} ```'},
        {"step": 4, "reply": 'Archiving what I found.\n'
                             '```json {"tool": "fs.write", "args": {"rel_path": "digest\\\\report.md", '
                             '"text": "# Report copied digest\\\\blank\\\\yes\\n- q3 dates confirmed\\n"}} ```'},
        {"step": 5, "reply": "DONE"},
    ]


def failing_script() -> List[Dict[str, Any]]:
    """Scenario C: intents to read a file that does not exist. Proves tool
    errors are recorded as facts and the agent loop degrades cleanly."""
    return [
        {"step": 1, "reply": 'Reading missing file.\n'
                             '```json {"tool": "fs.read", "args": {"rel_path": "ghost.txt"}} ```'},
    ]


def build_workspace(path: str) -> None:
    """Seeds the sandbox with the files the demo expects."""
    import os

    os.makedirs(path, exist_ok=True)
    with open(_join(path, "notes.txt"), "w", encoding="utf-8") as f:
        f.write("q3 dates: release candidate on 14 Aug, retro on 21 Aug.\n"
                "Owner: aasha. Reviewer: reserved for the storage team.\n")
    with open(_join(path, "changelog.md"), "w", encoding="utf-8") as f:
        f.write("## 0.9\n - started using the flight recorder\n")


def _join(a: str, b: str) -> str:
    import os

    return os.path.join(a, b)
