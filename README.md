# ⛵ Agent Flight Recorder

**Record your agent as facts, not logs — then replay it, diff it, verify it, and speed it up.**

Receipt-level tracing for AI agents. One append-only file per session.
Zero dependencies. No server. No SDK lock-in. Runs on a plain laptop —
because that's who needed it first.

> Your agent ran for 40 minutes while you were away. It says it finished.
> Someone asks: *which files did it touch, what did the model say at step 12,
> and did the plan change after the model update?*
>
> Logs answer approximately. **A flight recorder answers with receipts.**

---

## What's in the box

| Capability | Command | What you get |
| --- | --- | --- |
| Record | `afr record` | a replayable, hash-chained trace of one agent run |
| Report | `afr view` | a human-readable flight report — **on the terminal + as a `.md` file** (+ optional HTML) |
| Replay | `afr replay` | re-walk the recorded plan with zero I/O |
| Verify | `afr verify` | prove the trace file wasn't edited after the fact |
| Drift-check | `afr replay --verify` | re-run read-only steps against the live workspace; fail if results differ |
| Diff | `afr diff` | align two runs and show exactly where the plan changed |
| Prefetch | `afr prefetch` | an honest cost ledger: ms saved vs ms wasted for anticipatory pre-reading |

---

## How to use it — from zero to a full flight report

Everything below runs **fully offline**: the demo agent uses a scripted
mock model, so no API keys and no network are needed. It takes about
two minutes.

### Step 0 — one-time setup

Requirements: **Python 3.9+** and nothing else (stdlib only — no pip install needed).

```powershell
git clone <this-repo>
cd agent-flight-recorder
python -m pytest tests          # sanity check: 80 tests should pass in ~30 s
```

All commands work either as `python -m afr ...` (no install) or as
`afr ...` (after `pip install -e .` if you installed the package).

### Step 1 — Record an agent (one command)

```powershell
python -m afr record my_ws --scenario planner
```

`my_ws` is a tiny sandbox the recorder seeds with two files
(`notes.txt`, `changelog.md`); the demo agent then *works in it*. The
`planner` scenario is a scripted "model" that behaves like a real
agent: it lists the folder, reads the notes, writes a `PLAN.md`, and
says DONE. Real output:

```
recorded session 20261025-111404-a00f
  scenario planner  ok=True  steps=4
  agent said: DONE
  trace: D:\...\agent-flight-recorder\trace_store\20261025-111404-a00f\trace.jsonl
  next steps:
    python -m afr view    20261025-111404-a00f    # flight report on the terminal + a .md file
    python -m afr replay  20261025-111404-a00f    # re-walk the plan without re-running it
    python -m afr verify  20261025-111404-a00f    # tamper-evidence check
```

> **Your session id will differ** — it looks like `20261026-103012-9c1a`.
> Copy the one *your* command prints and use it below (a short prefix,
> like `20261025-111404`, works too, as long as it's unambiguous).

On disk you now have:

```
trace_store/
└── 20261025-111404-a00f/
    ├── trace.jsonl      24 JSON events, one per line, hash-chained
    └── manifest.json    content digest + summary (the tamper-evidence anchor)
```

Two more ready-made scenarios: `python -m afr record my_ws --scenario auditor`
(reads only), `--scenario failing` (intends to read a file that does not
exist — shows how errors are recorded), and `--scenario builder`
(three reads + one write).

### Step 2 — Read the flight report (the main artifact)

```powershell
python -m afr view 20261025-111404-a00f
```

This prints the **terminal report** and writes the **markdown report**
to `results\report-20261025-111404-a00f.md`. Real output:

```
==========================================================================
 AGENT FLIGHT REPORT   session 20261025-111404-a00f
==========================================================================
 agent      demo   (mock-scripted / mock-mini-1)   scenario: planner
 recorded   2026-10-25T11:14:04   (wall time 0.55 s)
 workspace  D:\...\agent-flight-recorder\my_ws
 outcome    OK   --  "agent declared done"
 trace      24 events, closed cleanly (run.end present)
--------------------------------------------------------------------------
 THE PLAN (what actually happened, with the receipts)
--------------------------------------------------------------------------
 step 1  [OK      ] fs.list   (read-only, 0.8 ms)
             args     { rel_dir=. }
             touched  read 2 entries: .\changelog.md, .\notes.txt
             result   changelog.md\nnotes.txt
 step 2  [OK      ] fs.read   (read-only, 6.9 ms)
             args     { rel_path=notes.txt }
             touched  read notes.txt (111 bytes)
             result   q3 dates: release candidate on 14 Aug, retro on 21 Au...
 step 3  [WRITE   ] fs.write   (write, 1.1 ms)
             args     { rel_path=PLAN.md; text=# Plan\n- read notes\n- summari...
             touched  created PLAN.md (44 bytes)
             result   wrote 44 chars to PLAN.md
 step 4  [DONE]     no tool call -- model reply: 'DONE'
--------------------------------------------------------------------------
 SIDE EFFECTS
   read        .\changelog.md, .\notes.txt, notes.txt
   created     PLAN.md (44 bytes)
--------------------------------------------------------------------------
 ERRORS              none
 TOOL TIME           8.74 ms across 3 calls   |   llm turns 4
==========================================================================
 check it:  afr verify  20261025-111404-a00f    # tamper-evidence
            afr diff <other-id> 20261025-111404-a00f    # compare two runs
==========================================================================
markdown report written: D:\...\results\report-20261025-111404-a00f.md
```

**How to read it — every line is a fact from the trace:**

- the **header block** is the who/when/where: agent name, model, workspace;
- `[OK]`, `[WRITE]`, `[ERROR]` are the tool's **safety class** and result,
  so unsafe traffic is visible at a glance;
- **touched** is the receipt — what the tool *physically did* (files read,
  bytes written, `created` vs `overwrote`), not just what it claimed;
- **result** is what the tool returned (truncated to one line);
- **SIDE EFFECTS** aggregates the whole run: everything read, everything
  written, shell commands fired;
- **ERRORS** and **TOOL TIME** close the report; the bottom lines hand you
  the exact commands to double-check the report's own claims.

Want the page version too?

```powershell
python -m afr view 20261025-111404-a00f --html    # + report-....html (next to the .md)
python -m afr view 20261025-111404-a00f --open    # ...and open it in your browser
```

### Step 3 — What the markdown report looks like

The `.md` file is the same facts, formatted for GitHub, PRs, and email.
Here is the committed example `examples/report-20261025-111756-5911.md`
(shortened — read the file itself for the full one):

```markdown
# Agent Flight Report -- `20261025-111756-5911`

## At a glance

| What | Value |
| --- | --- |
| Agent | `demo` -- `mock-scripted` / `mock-mini-1`, scenario `planner` |
| Recorded | 2026-10-25T11:17:56 (wall time 0.47 s) |
| Workspace | `...\examples\ws-run-a` |
| Outcome | **OK** -- "agent declared done" (4 steps, 3 tool calls) |
| Integrity | closed cleanly -- run.start + run.end present (24 events) |
| Trace file | `...\examples\trace\20261025-111756-5911\trace.jsonl` |

## What the agent did, step by step

| Step | Tool (safety class) | Call arguments | Receipt | Result | Took | Verdict |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `fs.list` (read-only) | { rel_dir=. } | read 2 entries: .\changelog.md, .\notes.txt | `changelog.md notes.txt` | 0.4 ms | OK |
| 2 | `fs.read` (read-only) | { rel_path=notes.txt } | read notes.txt (111 bytes) | `q3 dates: ...` | 4.6 ms | OK |
| 3 | `fs.write` (write) | { rel_path=PLAN.md; ... } | created PLAN.md (44 bytes) | `wrote 44 chars to PLAN.md` | 1.1 ms | WRITE |
| 4 | -- | -- | -- | model replied `DONE` | -- | DONE |

## Side effects on the workspace

- **created** `PLAN.md` (44 bytes)
- read: `.\changelog.md`, `.\notes.txt`, `notes.txt`

## How to check this yourself

- tamper check: `python -m afr verify 20261025-111756-5911`
- re-walk the plan: `python -m afr replay 20261025-111756-5911`
```

### Step 4 — Replay the run (zero I/O)

```powershell
python -m afr replay 20261025-111404
```

Rebuilds the recorded plan without calling a model or running a tool:

```
replay 20261025-111404-a00f   (mode=recorded)
  steps 4 | tool calls 3 | re-checked 0 | deviations 0
  verdict: CLEAN - the recorded plan replays exactly

{ ...full replay report as JSON ... }
```

The human verdict line is above; the JSON below is the machine-readable
same thing (feed it to a script; its details: which step, which tool,
which args, what came back).

### Step 5 — Prove the record wasn't tampered with

```powershell
python -m afr verify 20261025-111404
```

```
verify 20261025-111404-a00f
  events 24 | timestamps in order: True | content hash matches manifest: True
  verdict: CLEAN - the trace file is intact as recorded
```

Three checks: events are monotonic in time, the file's content digest
matches the one in the manifest (out-of-band), and the session closed
cleanly. To see a failure first-hand, append a junk line to `trace.jsonl`
and run it again — the verdict flips to `TAMPERED` and the CLI exits 1.

### Step 6 — Check the old claims against your workspace *today*

Verify mode re-executes only **read-only** tools and compares the
answers to what was recorded:

```powershell
python -m afr replay 20261025-111404 --verify --workspace my_ws
```

```
replay 20261025-111404-a00f   (mode=verify)
  steps 4 | tool calls 3 | re-checked 1 | deviations 1
  verdict: DRIFT - 1 step(s) did not replay as recorded
  note: step 1: fs.list result drifted recorded='changelog.md\nnotes.txt' now='PLAN.md\nchangelog.md\nnotes.txt'
```

That drift is *correct*: in Step 1 the agent itself created `PLAN.md`,
and now the folder listing includes it — `fs.list` returns one more entry,
so the trace's step 1 no longer holds. Writes are never re-executed
(`note: fs.write is write; skipped verification by policy`). This is the
command that answers *can I still trust that recorded result?* — and it
exits non-zero, so you can fail a CI job on any drift.

### Step 7 — Does prefetch pay for *this* trace? (the cost ledger)

The predictor learns tool habits from the trace and grades itself with
real measured milliseconds:

```powershell
python -m afr prefetch 20261025-111404 --workspace my_ws
```

```json
{
  "mode": "offline",
  "hit_rate": 1.0,
  "hits": 1,
  "misses": 0,
  "saved_ms": 2.82,
  "wasted_ms": 0.0,
  "net_ms": 2.82,
  "per_tool": { "fs.read": { "hit": 1.0, "miss": 0.0,
                             "saved_ms": 2.82, "wasted_ms": 0.0 } }
}
```

How to read each field:

| Field | Meaning |
| --- | --- |
| `hits` / `misses` | the predictor guessed the next tool correctly / not |
| `saved_ms` | real recorded latency of the call the agent actually made, on hits only |
| `wasted_ms` | cost of *wrong* speculations (offline: 0 by definition; with `--live` it shadow-runs misses and reports the honest number) |
| `net_ms` | saved − wasted: the only number that decides whether prefetch pays on your workload |

```powershell
python -m afr prefetch 20261025-111404 --workspace my_ws --live   # honest wasted_ms, shadow-executed
```

Writes and network are structurally refused — see the next sections.

### Step 8 — Diff two runs (what changed between them)

Record a second, different agent on the **same workspace**, then align:

```powershell
python -m afr record my_ws --scenario auditor
python -m afr diff 20261025-111404-a00f 20261025-111405-c13a
```

```
diff 20261025-111404-a00f  vs  20261025-111405-c13a
plan similarity 67%  (2 aligned steps)
  result drift step 1 vs 1
```

Reading it: both runs started the same (list the folder), then the
planner diverged (read more, wrote a plan). `result drift` pinpoints
the step where the *same tool with the same args returned different
data* — precisely the question "what moved under my agent?".

### Step 9 — What a failed run looks like

```powershell
python -m afr record my_ws_fail --scenario failing    # exits with code 1 on purpose
python -m afr view 20261025-111010-7e73
```

```
 outcome    FAILED  --  "tool 'fs.read' failed"
 trace      8 events, closed cleanly (run.end present)
--------------------------------------------------------------------------
 THE PLAN (what actually happened, with the receipts)
--------------------------------------------------------------------------
 step 1  [ERROR   ] fs.read   (read-only, 0.4 ms)
             args     { rel_path=ghost.txt }
             error    FileNotFoundError: no such file: ghost.txt
--------------------------------------------------------------------------
 ERRORS
   step 1  tool error (fs.read): FileNotFoundError: no such file: ghost.txt
```

The failure is recorded as a fact with an exception name and the exact
args that caused it — not as a stack trace soup.

### The two-minute cheat sheet

```powershell
python -m afr record my_ws --scenario planner        # record (planner|auditor|builder|failing)
python -m afr list                                   # all sessions: id, completeness, agent
python -m afr view    <id>                           # flight report -- terminal + .md file
python -m afr view    <id> --html                    # + self-contained .html for the browser
python -m afr replay  <id>                           # re-walk the plan (no I/O)
python -m afr replay  <id> --verify --workspace W    # re-run read-only steps, detect drift
python -m afr verify  <id>                           # tamper-evidence check
python -m afr prefetch <id> --workspace W            # prefetch cost ledger (add --live)
python -m afr diff    <idA> <idB>                    # plan-diff two runs
```

Exit codes: **0** clean verdict · **1** forensic failure (drift, tamper,
failed run) · **2** bad input (unknown session, missing flag argument) —
so the CLI slots straight into CI.

---

## How to test it

The test suite needs **no pytest plugins, no network, no API keys** — every
fixture records into a disposable `tmp_path` directory. Three ways to test,
fastest first:

### 1. Run everything (~30 s)

```powershell
python -m pytest tests
```

Expected last line:

```
80 passed in ~30s
```

(If you haven't, install the dev extra once: `pip install -e ".[dev]"` —
it pulls in only pytest. Or run `python -m pytest` directly; same thing.)

### 2. Run one area, or one single test

```powershell
python -m pytest tests/test_report.py -q          # one file
python -m pytest tests/test_prefetch.py -q        # safety gate + ledger
python -m pytest tests/test_replay.py::test_verify_flags_workspace_drift -v   # literally one test
```

What each file pins down:

| Test file | What it proves | # tests |
| --- | --- | --- |
| `tests/test_schema.py` | event envelope validation, hash linkage AFR-1 contract | 13 |
| `tests/test_store.py` | append-only JSONL store, manifest, reopen, chain detects edits | 8 |
| `tests/test_recorder.py` | the two recording seams (`llm`, `tool_call`) and receipts | 10 |
| `tests/test_replay.py` | deterministic walker + verify mode drift detection | 6 |
| `tests/test_prefetch.py` | predictor, shadow-safety gate (refuses writes!), cost ledger | 14 |
| `tests/test_diff.py` | plan alignment, arg/result drift decomposition | 5 |
| `tests/test_report.py` | terminal + markdown + HTML reports from trace alone | 10 |
| `tests/test_cli.py` | every command's exit codes and human-readable output | 12 |
| `tests/test_scenarios.py` | the four demo agent storylines run and close cleanly | 2 |
| | **total** | **80** |

### 3. The 8-step guided tour (~5 s)

```powershell
python scripts/demo.py
```

Records two sessions into `examples/trace/`, replays them, verifies
integrity, prices prefetch offline and live, diffs the two plans, and
writes the flight report as `examples/report-<session>.md` **and**
`.html`. It's idempotent — it wipes and recreates its own example
outputs, so run it as often as you like.

Fingers-on-keyboard checklist after editing any code:

```powershell
python -m pytest tests                       # the suite
python scripts/demo.py                       # the tour, end to end
python -m afr record my_ws --scenario failing; echo $LASTEXITCODE   # must be 1
python -m afr view  <that-session-id>        # reports must render
```

---

## Recording *your* agent (the integration contract)

AFR doesn't own your loop. It sells you two recording seams, 3 lines of
integration:

```python
from afr import Recorder, TraceStore, make_tools

store = TraceStore("./trace_store")                    # one session = one directory
rec   = Recorder(store, make_tools("./my_project"),    # tools registered + sandboxed
                 agent_meta={"name": "my-agent", "provider": "openai", "model": "gpt-4o"})

rec.open_run()
for step in range(1, N):
    rec.start_step(step)
    # seam 1: the model call -- fn is ANY prompt->text callable
    text = rec.llm(my_llm_function, step=step, prompt=prompt)
    # seam 2: the tool call -- intent, receipt, result, all on disk instantly
    result, raw, ok, receipt = rec.tool_call(step, "fs.read", {"rel_path": "notes.txt"})
    rec.end_step(step, status="ok")
rec.close_run(ok=True, reason="done")
```

`my_llm_function` can hit a real provider, a local llama.cpp server, or a
human. **The recorder never calls out** — that independence is what makes
every trace provider-neutral: the same file format whether the model is
GPT, GLM, or a colleague typing into a terminal.

`make_tools()` gives you the standard sandboxed set (`fs.list`, `fs.read`,
`fs.write`, `shell`) rooted at your workspace, or register your own:

```python
reg = make_tools("./my_project")
reg.register("db.query", my_query_fn, safety="network",
             blurb="query the metrics DB", arg_hint="sql='...'")

```

The `safety` class you declare is a *contract*: `read-only`/`pure` tools
may be speculatively pre-run by prefetch inside a throwaway copy of the
workspace; `write`/`network`/`dangerous` never are — there is no override
flag, by design.

### What every recorded event contains

| Event | Records |
| --- | --- |
| `run.start` / `run.end` | agent identity, environment snapshot, final outcome + reason |
| `llm.request` / `llm.response` | verbatim prompt and answer (hashed), provider, model, token estimate, duration |
| `tool.intent` | the tool the agent said it would call, exact args, safety class |
| `tool.result` | what actually ran: receipt (files touched, `existed_before`, byte counts), result + its hash, duration, ok |
| `error` | anything that interrupted the loop |

### On-disk format (boring on purpose — so you can `grep` it)

```json
{"type":"tool.result","ts_ns":1769138733123000000,"eid":"e000009",
 "payload":{"step":2,"tool":"fs.read","args":{"rel_path":"notes.txt"},
  "receipt":{"kind":"fs_read","read":["notes.txt"],"n_bytes":111},
  "result":"q3 dates...","result_sha":"9f8b23c91a04c7d2",
  "duration_ns":213900,"ok":true}}
```

```
Select-String '"ok": false' trace_store\*\trace.jsonl   # Windows PowerShell
grep '"ok": false' trace_store/*/trace.jsonl            # bash / git bash
```

Any reader you write is ten lines. That portability is a *feature*.

---

## The safety taxonomy (the load-bearing wall)

Prefetch is safe *by construction*, not by promise:

| Class | Meaning | Speculation |
| --- | --- | --- |
| `read-only` | reads state, returns text | **allowed** — in the shadow copy |
| `pure` | deterministic, no I/O | allowed |
| `write` | mutates the world | **refused — no override flag exists** |
| `network` | talks outside | refused |
| `dangerous` | obviously destructive | refused + recorded in ledger's `refusals` |

The gate is structural: the allowlist is frozen at engine construction
from the taxonomy; `speculate()` re-checks the class at call time; the
shadow registry is minted *inside a copied workspace* and refuses to run
if any tool's class disagrees with the live registry. Try breaking it:
`tests/test_prefetch.py::test_engine_refuses_to_speculate_writes` asserts
the file **does not exist** after the attempted speculation.

## The cost model (one formula)

```
net per workload  =  hit_rate × avg_real_ms   −   miss_rate × avg_shadow_ms
prefetch pays  ⇔  hit_rate / (1 − hit_rate)  >   avg_shadow_ms / avg_real_ms
```

Every variable is in the ledger — compare predictors by the *same* math,
keep speculative mode off until a workload's ratio clears the bar.
Full accounting: [`docs/cost-model.md`](docs/cost-model.md).

## Why "receipt" instead of "log"

| | Log output | AFR trace |
| --- | --- | --- |
| What it stores | narrative text | typed events: prompt · verbatim response · tool intent · tool receipt |
| Reproducible replay? | no | **yes** — deterministic walk, zero I/O |
| What a tool touched | guess from stdout | **the receipt**: paths, byte counts, `existed_before` |
| Diff two runs | eyeball both outputs | **alignment table**: same plan? which step drifted? |
| Prove the trace wasn't edited | no | **hash chain** per line + file digest in the manifest |

The mental model: an airplane's flight recorder doesn't narrate the flight.
It records control inputs and sensor values at every tick — so afterwards
you can *re-fly the sequence without the sky*.

## Honest limits (writing them down before someone asks)

- **Shell receipts are coarse.** We record command, cwd, returncode — not
  every file a shell one-liner touched. Run-level environment diffing
  catches material side effects; syscall-level wrappers (eBPF/ETW) are
  the roadmap.
- **What the recorder doesn't see:** a *model* provider doing its own I/O
  inside `llm()` is visible only as the response text. Tracing belongs to
  the tools seam.
- **The workspace snapshot is `copytree`.** Demos are tiny; huge trees
  should swap in reflinks/overlay mounts (seam is `ShadowWorkspace`).
- **`afr verify` catches casual edits** (chain + monotonicity) — it is
  not yet a formal audit-log (no WORM storage, no TPM anchoring).
- **`--live` misses pay measured shadow cost only when plausible args
  are known**; with none, the ledger says `wasted_ms: 0` — an honest
  lower bound, flagged as such.

## Repo tour

```
afr/                  the package (schema · store · recorder · replay · prefetch · diff · report · cli)
tests/                80 tests: schema, store, recorder, replay, prefetch, diff, report, cli, scenarios
scripts/demo.py       the 8-step offline tour (idempotent)
examples/trace/       two committed recorded sessions + manifests
examples/report-*.md  the generated markdown flight report (read it in any editor / GitHub)
examples/report-*.html  the generated flight report (open in any browser)
results/              both cost ledgers + the trained predictor
docs/ARCHITECTURE.md  module-by-module design, invariants, and their prices
docs/cost-model.md    the accounting, with measured demo numbers
```

## FAQ for skeptical engineers

**Why not Slack it into LangSmith / AgentOps / Langfuse?**
Those are hosted *telemetry* platforms: dashboards over emitted spans. AFR
is a local-first *forensic format* — you own the file, `grep` wins,
replaying never hits the network, and 80 tests run in CI on your machine
instead of an SDK contract in a vendor changelog. (Also: it's a library,
not a purchase.)

**Why only n-gram prediction?**
Deliberate. The decision to prefetch must cost microseconds, or the
savings never clear the self-inflicted tax. Smarter heads — embeddings,
tiny LLM classifiers — slot into the same `predict/observe` interface and
get graded by the same ledger. Swap one file, run two `evaluate()` calls,
publish both numbers.

**Why Python 3.9+ and zero dependencies?**
Because recorders get vendored into fragile agent hosts. Anywhere the
agent runs, the files of `afr/` will record. (Verified on 3.9 and 3.12.)

## Roadmap

1. **Provider shims** — OpenAI-compatible + MCP tool recorder so real agent
   SDKs emit AFR-1 (schema is already reentrant).
2. **Shell receipts v2** — strace/ETW wrappers to name every file a
   command actually touched, reads included.
3. **Prediction head v2** — same interface, small learned model; ledger
   decides which wins.
4. **Trace-CI action** — GitHub Action that replays a session's plan and
   fails the PR on shape change.

## Developing

```powershell
pip install -e ".[dev]"    # only pytest; runtime stays stdlib-only
python -m pytest tests     # 80 tests
python scripts/demo.py     # fresh artifacts every run
```

## License

MIT. Built because agents deserve black boxes too.
