"""Reports. Three forms, one truth source (the trace alone):

  terminal   build_terminal_report()  plain-text summary for the console,
                                      printed by ``afr view`` with no flags.
  markdown   build_markdown_report()  a .md file: At-a-glance, the plan as a
                                      step table, side effects, errors, and
                                      the exact commands to check every claim.
  html       build_report()          one self-contained page with a scrub-able
                                      timeline; everything else is here too.

Every form is generated *from the trace alone* -- if the trace can't prove
it, the report can't show it. That's what makes them audit-grade rather
than pretty dashboards. Markdown / terminal / HTML never disagree, because
all three render the same ``_assemble()`` digest.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import webbrowser
from typing import Any, Dict, List, Optional, Tuple

from .schema import (
    ERROR, LLM_RESPONSE, LLM_REQUEST, RUN_END, RUN_START, STEP_END,
    TOOL_INTENT, TOOL_RESULT, Event,
)

_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>AFR report - {session}</title>
<style>
  :root {{ --bg:#0d1117; --fg:#e6edf3; --mut:#8b949e; --acc:#79c0ff;
           --warn:#f0883e; --ok:#3fb950; --err:#f85149; }}
  body {{ font: 14px/1.55 ui-sans-serif, system-ui, sans-serif;
          background:var(--bg); color:var(--fg); margin:0; }}
  .wrap {{ max-width: 980px; margin: 0 auto; padding: 24px; }}
  h1 {{ font-size: 20px }} h2 {{ font-size: 15px; color: var(--mut); margin-top: 32px }}
  pre {{ background:#161b22; border:1px solid #30363d; border-radius:8px;
         padding:14px; overflow-x:auto; font-size:13px; min-height:60px }}
  code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace }}
  .timeline {{ position: relative; height: 150px; border: 1px solid #30363d;
               border-radius: 8px; background:#161b22; margin-top:8px }}
  .ev {{ position:absolute; height:16px; border-radius:3px; cursor:pointer;
         min-width:3px; box-sizing:border-box }}
  .ev:hover, .ev.sel {{ outline:1px solid var(--fg) }}
  .ev.llm {{ background: var(--acc) }} .ev.tool {{ background: var(--ok) }}
  .ev.unsafe {{ background: var(--warn) }}
  .ev.toolerr {{ background: var(--err) }}
  .ev.other {{ background:#30363d }}
  .lawes {{ color: var(--mut); font-size:12px; margin-top:6px }}
  .row {{ padding:6px 10px; border:1px solid #30363d; border-radius:8px;
          margin-top:8px; cursor:pointer }}
  .row:hover {{ border-color: var(--mut) }}
  .row.sel {{ border-color: var(--acc) }}
</style></head><body><div class="wrap">
<h1>Agent flight report</h1>
<p class="lawes">session <code>{session}</code> &middot; {created} &middot;
  {nevents} events &middot; integrity: {verdict}</p>
<h2>Timeline <span class="lawes">(click an event to inspect)</span></h2>
<div class="timeline" id="tl"></div>
<p class="lawes">top lane: LLM turns &middot; bottom lane: tool calls &middot;
  blue llm &middot; green read-only tool ok &middot; orange tool (write/network class) &middot;
  red tool error &middot; width ~ duration</p>
<h2>Plan (what actually happened)</h2>
<div id="plan"></div>
<h2>Inspector</h2>
<pre id="insp" class="lawes">click any event above</pre>
<script>
const TRACE = {payload};
const T0 = TRACE.t0_NS;
function ms(e) {{ return (e.t0 - T0) / 1e6; }}
</script>
<script>
const tl = document.getElementById("tl");
const width = tl.clientWidth || 948;
const durs = TRACE.events.map(e => ms(e) + (e.dur_ms || 0));
const maxT = Math.max(1, Math.max(...durs)) * 1.05;
const laneFor = k => (k === "llm" ? 18 : 64);
TRACE.events.filter(e => e.kind !== "other").forEach(ev => {{
  const d = document.createElement("div");
  d.className = "ev " + ev.kind
             + (ev.ok === false ? " toolerr" : "")
             + (ev.safety && ev.safety !== "read-only" ? " unsafe" : "");
  d.style.left = ((ms(ev) / maxT) * width) + "px";
  d.style.width = Math.max(4, ((ev.dur_ms || 3) / maxT) * width) + "px";
  d.style.top = laneFor(ev.kind) + "px";
  d.title = (ev.step ? ("step " + ev.step + " ") : "") + ev.kind;
  d.onclick = () => inspect(ev, d);
  tl.appendChild(d);
}});
function inspect(ev, div) {{
  document.querySelectorAll(".ev.sel,.row.sel").forEach(n => n.classList.remove("sel"));
  div.classList.add("sel");
  document.getElementById("insp").textContent = JSON.stringify(ev.json, null, 2);
  window.location.hash = ev.eid;
}}
const planEl = document.getElementById("plan");
TRACE.plan.forEach(p => {{
  const row = document.createElement("div");
  row.className = "row";
  row.innerHTML = "<b>step " + p.step + "</b> &nbsp; <code>" + p.tool
    + "</code> &nbsp; " + (p.ok ? "" : '<span style="color:#f85149">tool error</span> ')
    + '<span class="lawes">' + (p.receipt && p.receipt.kind ? p.receipt.kind : "") + "</span>";
  row.onclick = () => {{
    const target = TRACE.events.find(e => e.step === p.step && e.kind === "tool" &&
                                          e.ok === p.ok);
    if (target) {{ window.location.hash = target.eid;
      document.getElementById("insp").textContent = JSON.stringify(target.json, null, 2); }}
  }};
  planEl.appendChild(row);
}});
</script>
</div></body></html>
"""


def build_report(events: List[Event], session_id: Optional[str] = None) -> str:
    """Render a session to a self-contained HTML string."""
    plan, raw_t, t0 = _digest(events)
    payload = json.dumps({"t0_NS": t0, "events": raw_t, "plan": plan}, ensure_ascii=True)
    return _TEMPLATE.format(
        session=session_id or _session(events),
        created=_created(events),
        nevents=len(events),
        verdict=_integrity(events),
        payload=payload,
    )


def write_report(events: List[Event], out_path: str, open_browser: bool = False) -> str:
    html = build_report(events)
    out = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    if open_browser:
        webbrowser.open("file://" + out.replace("\\", "/"))
    return out


# ---------------------------------------------------------------- internals


def _session(events: List[Event]) -> str:
    return events[0].session_id if events else "(empty)"


def _created(events: List[Event]) -> str:
    if not events:
        return "(empty)"
    return _dt.datetime.fromtimestamp(events[0].ts_ns / 1e9).isoformat(timespec="seconds")


def _integrity(events: List[Event]) -> str:
    started = any(e.type == RUN_START for e in events)
    closed = any(e.type == RUN_END for e in events)
    if started and closed:
        return "run.start + run.end present"
    if not started:
        return "no run.start (writer bug?)"
    return "no run.end (interrupted session)"


def _digest(events: List[Event]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
    """Project the trace into:
       raw_t   -- timeline events (t0/dur/tool/safety/eid + full json)
       plan    -- one row per TOOL_RESULT step
       t0      -- absolute start ns, so JS can translate to relative ms
    """
    t0 = events[0].ts_ns if events else 0
    raw_t: List[Dict[str, Any]] = []
    plan: List[Dict[str, Any]] = []
    for ev in events:
        kind = _kind_of(ev)
        step = ev.payload.get("step")
        entry: Dict[str, Any] = {
            "eid": ev.eid,
            "kind": kind,
            "step": int(step) if step is not None else None,
            "t0": ev.ts_ns,
            "dur_ms": None,
            "tool": ev.payload.get("tool"),
            "safety": ev.payload.get("safety"),
            "ok": ev.payload.get("ok"),
            "json": ev.to_dict(),
        }
        if ev.type == TOOL_RESULT and ev.payload.get("duration_ns") is not None:
            entry["dur_ms"] = round(ev.payload["duration_ns"] / 1e6, 2)
        raw_t.append(entry)
        if ev.type == TOOL_RESULT:
            plan.append({
                "step": int(ev.payload["step"]),
                "tool": ev.payload["tool"],
                "receipt": ev.payload.get("receipt"),
                "ok": ev.payload.get("ok"),
            })
    return plan, raw_t, t0


def _kind_of(ev: Event) -> str:
    if ev.type in (LLM_REQUEST, LLM_RESPONSE):
        return "llm"
    if ev.type in (TOOL_INTENT, TOOL_RESULT):
        return "tool"
    if ev.type == ERROR:
        return "tool"        # painted onto the tool lane (colored red)
    return "other"


# ------------------------------------------------------------------------
# Shared digest for the terminal + markdown reports.
#
# One pass over the events produces every fact the two reports show, so a
# terminal line and its markdown row are always the same fact (and match
# what grep on trace.jsonl would return).
# ------------------------------------------------------------------------

def _assemble(events: List[Event]) -> Dict[str, Any]:
    """Reduce a trace to a plain dict of display facts."""
    if not events:
        return {"empty": True, "session": "(empty)", "steps": [],
                "n_events": 0, "llm_turns": 0, "tool_calls": 0,
                "tool_time_ms": 0.0, "files_read": [], "files_written": [],
                "commands": [], "errors": [], "complete": False,
                "outcome": None, "reason": ""}

    t0 = events[0].ts_ns
    tend = events[-1].ts_ns
    start_ev = events[0].payload if events[0].type == RUN_START else {}
    agent = dict(start_ev.get("agent", {}))
    env = dict(start_ev.get("env_snapshot", {}))
    run_end = next((e.payload for e in events if e.type == RUN_END), None)
    summary = (run_end or {}).get("summary") or {}

    err_events = [e for e in events if e.type == ERROR]
    intents: Dict[int, Dict[str, Any]] = {}
    requests: Dict[int, Dict[str, Any]] = {}
    responses: Dict[int, Dict[str, Any]] = {}
    statuses: Dict[int, str] = {}
    results: Dict[int, Dict[str, Any]] = {}
    files_read: List[str] = []
    files_written: List[Dict[str, Any]] = []
    commands: List[Dict[str, Any]] = []
    tool_time = 0.0
    tool_errors: List[Dict[str, Any]] = []

    for ev in events:
        st = ev.payload.get("step")
        st = int(st) if st is not None else None
        if ev.type == LLM_REQUEST:
            requests[st] = ev.payload
        elif ev.type == LLM_RESPONSE:
            responses[st] = ev.payload
        elif ev.type == TOOL_INTENT:
            intents[st] = ev.payload
        elif ev.type == STEP_END:
            statuses[st] = ev.payload.get("status", "")
        elif ev.type == TOOL_RESULT:
            results[st] = ev.payload
            tool_time += ev.payload.get("duration_ns", 0) / 1e6
            if ev.payload.get("ok") is False:
                tool_errors.append({
                    "step": st,
                    "kind": f"tool error ({ev.payload.get('tool')})",
                    "message": _short(ev.payload.get("result"), 200),
                })
            rcpt = ev.payload.get("receipt") or {}
            kind = rcpt.get("kind", "opaque")
            if kind == "fs_read":
                files_read.extend(rcpt.get("read") or [])
            elif kind == "fs_write":
                for w in rcpt.get("written") or []:
                    files_written.append({
                        "path": w,
                        "existed_before": bool(rcpt.get("existed_before", False)),
                        "n_bytes": rcpt.get("n_bytes"),
                    })
            elif kind == "shell_execution":
                commands.append({
                    "command": rcpt.get("command"),
                    "cwd": rcpt.get("cwd"),
                    "returncode": rcpt.get("returncode"),
                })

    errors = ([{"step": e.payload.get("step"), "kind": e.payload.get("kind"),
                "message": e.payload.get("message")} for e in err_events] + tool_errors)

    steps: List[Dict[str, Any]] = []
    for st in sorted(set(requests) | set(responses) | set(intents) | set(results) | set(statuses)):
        if st is None:
            continue
        res = results.get(st) or {}
        intent = intents.get(st) or {}
        resp = responses.get(st) or {}
        req = requests.get(st) or {}
        steps.append({
            "step": st,
            "status": statuses.get(st, "interrupted" if st not in results and st in intents else "no-step-end"),
            "tool": res.get("tool") or intent.get("tool"),
            "args": res.get("args") or intent.get("args") or {},
            "safety": intent.get("safety") or res.get("receipt", {}).get("safety"),
            "ok": res.get("ok"),
            "receipt": res.get("receipt"),
            "result_kind": res.get("result_kind"),
            "result": res.get("result"),
            "result_sha": res.get("result_sha"),
            "dur_ms": round(res.get("duration_ns", 0) / 1e6, 2) if res else None,
            "think": resp.get("response"),
            "prompt_sha": req.get("prompt_sha"),
            "provider": req.get("provider"),
            "model": req.get("model"),
            "tokens_in": resp.get("tokens_in"),
            "tokens_out": resp.get("tokens_out"),
            "intent_without_result": bool(intent) and not bool(res),
        })

    uniq_read = []
    for p in files_read:
        if p not in uniq_read:
            uniq_read.append(p)

    return {
        "empty": False,
        "session": events[0].session_id,
        "agent": agent,
        "workspace": env.get("workspace", ""),
        "platform": env.get("platform", ""),
        "python_version": env.get("python", ""),
        "created_iso": _created(events),
        "duration_s": max(0.0, (tend - t0) / 1e9),
        "n_events": len(events),
        "complete": run_end is not None,
        "outcome": summary.get("ok"),
        "reason": summary.get("reason", ""),
        "steps": steps,
        "llm_turns": len(responses),
        "tool_calls": len(results),
        "tool_time_ms": round(tool_time, 2),
        "files_read": uniq_read,
        "files_written": files_written,
        "commands": commands,
        "errors": errors,
    }


def _short(text: Any, n: int = 64) -> str:
    """One-line, truncated preview of any value (for tables/terminals)."""
    if text is None:
        return ""
    s = text if isinstance(text, str) else repr(text)
    s = s.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "").replace("\t", " ")
    if len(s) > n:
        s = s[: n - 3].rstrip() + "..."
    return s


def _receipt_phrase(receipt: Optional[Dict[str, Any]]) -> str:
    """Receipt -> one human sentence ('created PLAN.md (44 bytes)')."""
    if not receipt:
        return "(no receipt recorded)"
    kind = receipt.get("kind", "opaque")
    if kind == "fs_read":
        paths = receipt.get("read") or []
        if "n_bytes" in receipt:
            return f"read {paths[0] if paths else '?'} ({receipt['n_bytes']} bytes)"
        return f"read {receipt.get('n_read', len(paths))} entries: " + ", ".join(paths)
    if kind == "fs_write":
        verb = "overwrote" if receipt.get("existed_before") else "created"
        n = receipt.get("n_bytes")
        return f"{verb} {(receipt.get('written') or ['?'])[0]} ({n} bytes)"
    if kind == "shell_execution":
        return f"ran [{receipt.get('command')}] (exit {receipt.get('returncode')})"
    if kind == "error":
        return f"tool failed (raised {receipt.get('raising')})"
    return _short(_flatten(receipt), 70)


def _flatten(obj: Any) -> str:
    if isinstance(obj, dict):
        inner = "; ".join(f"{k}={_flatten(v)}" for k, v in obj.items())
        return "{ " + inner + " }" if inner else "{}"
    if isinstance(obj, list):
        return "[" + ", ".join(_flatten(v) for v in obj) + "]"
    return str(obj)


# ------------------------------------------------------------------------
# Terminal report (plain ASCII so it survives every Windows console)
# ------------------------------------------------------------------------

BAR = "=" * 74
SUB = "-" * 74


def build_terminal_report(events: List[Event], session_id: Optional[str] = None) -> str:
    """A receipt table a human can audit in <60 seconds, for the console."""
    d = _assemble(events)
    L: List[str] = []
    L.append(BAR)
    L.append(f" AGENT FLIGHT REPORT   session {d['session']}")
    L.append(BAR)
    if d["empty"]:
        L.append(" (empty trace -- nothing recorded yet)")
        L.append(BAR)
        return "\n".join(L)

    agent = d["agent"]
    meta = agent.get("name", "unknown")
    if agent.get("provider") or agent.get("model"):
        meta += f"   ({agent.get('provider', '?')} / {agent.get('model', '?')})"
    if agent.get("scenario"):
        meta += f"   scenario: {agent['scenario']}"
    L.append(f" agent      {meta}")
    L.append(f" recorded   {d['created_iso']}   (wall time {d['duration_s']:.2f} s)")

    ws = d["workspace"]
    if ws:
        L.append(f" workspace  {ws}")
    outcome = d["outcome"]
    if outcome is None:
        verdict = "INTERRUPTED (no run.end recorded)"
    elif outcome:
        verdict = f'OK   --  "{d["reason"]}"'
    else:
        verdict = f'FAILED  --  "{d["reason"]}"'
    L.append(f" outcome    {verdict}")
    complete = "closed cleanly (run.end present)" if d["complete"] else "no run.end -- interrupted"
    L.append(f" trace      {d['n_events']} events, {complete}")

    L.append(SUB)
    L.append(" THE PLAN (what actually happened, with the receipts)")
    L.append(SUB)
    for s in d["steps"]:
        if s["tool"] is None:
            L.append(f" step {s['step']}  [DONE]     no tool call -- model reply: "
                     f"{_short(s['think'] or '', 60)!r}")
            continue
        if s.get("intent_without_result"):
            L.append(f" step {s['step']}  [BROKEN]   {s['tool']} -- intent recorded, result missing")
            if s["args"]:
                L.append(f"             args     {_short(_flatten(s['args']), 64)}")
            continue
        safety = s.get("safety") or "?"
        tag = {"read-only": "OK      ", "pure": "OK      "}.get(safety, "WRITE   " if safety == "write"
                                                               else "NETWORK " if safety == "network"
                                                               else "RISKY   ")
        if s["ok"] is False:
            tag = "ERROR   "
        dur = f"{s['dur_ms']:.1f} ms" if s["dur_ms"] is not None else "n/a"
        L.append(f" step {s['step']}  [{tag}] {s['tool']}   ({safety}, {dur})")
        if s["args"]:
            L.append(f"             args     {_short(_flatten(s['args']), 62)}")
        if s["ok"] is False:
            L.append(f"             error    {_short(s['result'] or '(no detail)', 62)}")
        else:
            L.append(f"             touched  {_receipt_phrase(s['receipt'])}")
            L.append(f"             result   {_short(s['result'], 62)}")

    if d["files_read"] or d["files_written"] or d["commands"]:
        L.append(SUB)
        L.append(" SIDE EFFECTS")
        if d["files_read"]:
            L.append(f"   {'read':<10}  " + ", ".join(d["files_read"]))
        for w in d["files_written"]:
            verb = "overwrote" if w["existed_before"] else "created"
            n = f" ({w['n_bytes']} bytes)" if w.get("n_bytes") is not None else ""
            L.append(f"   {verb:<10}  {w['path']}{n}")
        for c in d["commands"]:
            L.append(f"   {'ran':<10}  [{c['command']}] exit {c['returncode']}")
    errs = d["errors"]
    L.append(SUB)
    if errs:
        L.append(" ERRORS")
        for e in errs:
            step_lbl = e["step"] if e["step"] is not None else "-"
            L.append(f"   step {step_lbl}  {e['kind']}: {_short(e['message'], 56)}")
    else:
        L.append(" ERRORS              none")
    L.append(f" TOOL TIME           {d['tool_time_ms']} ms across {d['tool_calls']} calls"
             f"   |   llm turns {d['llm_turns']}")
    L.append(BAR)
    L.append(' check it:  afr verify  ' + d["session"] + '    # tamper-evidence')
    L.append('            afr diff <other-id> ' + d["session"] + '    # compare two runs')
    L.append(BAR)
    return "\n".join(L)


# ------------------------------------------------------------------------
# Markdown report (.md)
# ------------------------------------------------------------------------

def _md_cell(text: Any, n: int = 72) -> str:
    s = _flatten(text) if not isinstance(text, str) else str(text)
    s = s.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").replace("\r", "")
    if len(s) > n:
        s = s[: n - 3].rstrip() + "..."
    return s


def build_markdown_report(events: List[Event], session_id: Optional[str] = None,
                          trace_path: Optional[str] = None) -> str:
    """Render a session to an easy-to-understand .md report."""
    d = _assemble(events)
    sid = session_id or d["session"]
    L: List[str] = []
    L.append(f"# Agent Flight Report -- `{sid}`")
    L.append("")
    L.append("> Every claim below is generated from the trace alone "
             "(`trace.jsonl`). Open the raw events to verify any line.")
    L.append("")
    if d["empty"]:
        L.append("(empty trace -- run `afr record` first, then generate again.)")
        return "\n".join(L) + "\n"

    agent = d["agent"]
    L.append("## At a glance")
    L.append("")
    L.append("| What | Value |")
    L.append("| --- | --- |")
    L.append(f"| Agent | `{agent.get('name', '?')}` -- `{agent.get('provider', '?')}` / "
             f"`{agent.get('model', '?')}`" + (f", scenario `{agent.get('scenario')}`"
                                              if agent.get("scenario") else "") + " |")
    L.append(f"| Recorded | {d['created_iso']} (wall time {d['duration_s']:.2f} s) |")
    if d["workspace"]:
        L.append(f"| Workspace | `{_md_cell(d['workspace'])}` |")
    outcome = d["outcome"]
    if outcome is None:
        oc = "**INTERRUPTED** -- no run.end recorded"
    elif outcome:
        oc = f"**OK** -- \"{d['reason']}\" ({len(d['steps'])} steps, {d['tool_calls']} tool calls)"
    else:
        oc = f"**FAILED** -- \"{d['reason']}\""
    L.append(f"| Outcome | {oc} |")
    integrity = ("closed cleanly -- run.start + run.end present"
                 if d["complete"] else "**no run.end -- interrupted session**")
    L.append(f"| Integrity | {integrity} ({d['n_events']} events) |")
    L.append(f"| Trace file | `{trace_path or '<store>/<session-id>/trace.jsonl'}` |")
    L.append("")

    L.append("## What the agent did, step by step")
    L.append("")
    L.append("| Step | Tool (safety class) | Call arguments | Receipt -- what it touched | Result | Took | Verdict |")
    L.append("| --- | --- | --- | --- | --- | --- | --- |")
    for s in d["steps"]:
        if s["tool"] is None:
            L.append(f"| {s['step']} | -- | -- | -- | model replied "
                     f"`{_md_cell(s['think'] or '', 40)}` | -- | DONE |")
            continue
        if s.get("intent_without_result"):
            L.append(f"| {s['step']} | `{_md_cell(s['tool'])}` | {_md_cell(_flatten(s['args']))} "
                     f"| -- | -- | -- | **BROKEN** (intent without result) |")
            continue
        cls = f"`{_md_cell(s['tool'])}` ({s.get('safety') or '?'})"
        took = f"{s['dur_ms']:.1f} ms" if s["dur_ms"] is not None else "n/a"
        if s["ok"] is False:
            verdict = "**ERROR**"
            touched = _md_cell(_receipt_phrase(s["receipt"]), 48)
            result = f"`{_md_cell(s['result'], 56)}`"
        else:
            verdict = "WRITE" if (s.get("safety") or "") == "write" else "OK"
            touched = _md_cell(_receipt_phrase(s["receipt"]))
            result = f"`{_md_cell(s['result'], 56)}`"
        L.append(f"| {s['step']} | {cls} | {_md_cell(_flatten(s['args']))} | {touched} "
                 f"| {result} | {took} | {verdict} |")
    L.append("")

    L.append("## Side effects on the workspace")
    L.append("")
    if d["files_written"]:
        for w in d["files_written"]:
            verb = "**overwrote**" if w["existed_before"] else "**created**"
            n = f" ({w['n_bytes']} bytes)" if w.get("n_bytes") is not None else ""
            L.append(f"- {verb} `{w['path']}`{n}")
    if d["files_read"]:
        L.append(f"- read: {', '.join('`%s`' % p for p in d['files_read'])}")
    if not d["files_written"] and not d["files_read"] and not d["commands"]:
        L.append("- none recorded (the agent wrote nothing)")
    L.append("")

    if d["errors"]:
        L.append("## Errors")
        L.append("")
        L.append("| Step | Kind | Message |")
        L.append("| --- | --- | --- |")
        for e in d["errors"]:
            L.append(f"| {e['step'] if e['step'] is not None else '--'} "
                     f"| `{e['kind']}` | {_md_cell(e['message'], 90)} |")
        L.append("")

    L.append("## How to check this yourself")
    L.append("")
    tp = trace_path or "<store>/<session-id>/trace.jsonl"
    L.append(f"- raw facts: `{tp}` (one JSON event per line, hash-chained -- grep it)")
    L.append(f"- tamper check: `python -m afr verify {sid}`")
    L.append(f"- re-walk the plan: `python -m afr replay {sid}`")
    L.append(f"- compare two runs: `python -m afr diff <other-id> {sid}`")
    L.append("")
    return "\n".join(L) + "\n"


def write_markdown_report(events: List[Event], out_path: str,
                          session_id: Optional[str] = None,
                          trace_path: Optional[str] = None) -> str:
    """Write the markdown report; returns the absolute path."""
    md = build_markdown_report(events, session_id=session_id, trace_path=trace_path)
    out = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    return out
