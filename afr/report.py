"""Static HTML report: a scrub-able timeline of a recorded session.

Zero runtime, zero dependencies: one self-contained HTML file with the
trace embedded as JSON and a small vanilla-JS walker. Open it from disk
or attach it to an email; nothing phones home, which matters because
traces are sensitive by nature.

What it shows, top to bottom:
  * header               session, agent, timestamps, integrity verdict
  * the timeline         one lane per source: LLM turns, tool calls
  * the plan             the agent's actual decisions, in order
  * the inspector        the raw events of whatever you clicked, verbatim

The report is generated *from the trace alone* -- if the trace can't
prove it, the report can't show it. That's what makes it audit-grade
rather than a pretty dashboard.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import webbrowser
from typing import Any, Dict, List, Optional, Tuple

from .schema import (
    ERROR, LLM_RESPONSE, LLM_REQUEST, RUN_END, RUN_START,
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
