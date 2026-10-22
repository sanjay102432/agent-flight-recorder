# ⛵ Agent Flight Recorder

**Record your agent as facts, not logs — then replay it, diff it, verify it, and speed it up.**

Receipt-level tracing for AI agents. One append-only file per session. Zero dependencies.
No server. No SDK lock-in. Runs on a plain laptop — because that's who needed it first.

---

## The 40-minute question

> Your agent ran for 40 minutes while you were away. It says it finished.
> Someone asks: *which files did it touch, what did the model say at step 12,
> and did the plan change after the model update?*
>
> Logs answer approximately. **A flight recorder answers with receipts.**

That distinction is the whole project. Logs are opinions printed to stdout.
A flight recorder stores the four measurable facts behind every loop step —
and once you have the facts, replay, diff, audit, and *predictive prefetch*
are just programs that read them.

---

## Why "receipt" instead of "log"

| | Log output | AFR trace |
| --- | --- | --- |
| What it stores | narrative text | typed events: prompt · verbatim response · tool intent · tool receipt |
| Reproducible replay? | no | **yes** — deterministic walk, zero I/O |
| What a tool touched | guess from stdout | **the receipt**: paths, byte counts, `existed_before` |
| Diff two runs | eyeball both outputs | **alignment table**: same plan? which step drifted? |
| Prove the trace wasn't edited | no | **hash chain** per line + file digest in the manifest |
| Make the next run faster | no | **prefetch ledger**: ms saved vs ms wasted, per tool class |

The mental model: an airplane's flight recorder doesn't narrate the flight.
It records control inputs, sensor values, and altitude at every tick — so
afterwards you can *re-fly the sequence without the sky*.

---

## 60-second quickstart (fully offline)

```powershell
# clone, then from the repo root:
python -m pytest tests                     # 73 tests, ~30 s
python scripts/demo.py                     # 8-step guided tour, ~5 s
```

The tour writes `examples/report-<session>.html` — **open it in a browser.**
One self-contained file: turn-by-turn timeline, the agent's plan with every
receipt, and the raw events of whatever you click. No server, no CDN, no build step.

Then fly an agent for yourself:

```powershell
python -m afr record my_ws --scenario planner   # planner | auditor | builder | failing
python -m afr list
python -m afr replay <session-id>
python -m afr replay <session-id> --verify --workspace my_ws
python -m afr verify  <session-id>
python -m afr prefetch <session-id> --workspace my_ws --live
python -m afr diff <session-a> <session-b>
python -m afr view  <session-id> --open
```

Session ids are directory names like `20261023-022843-1632`; prefixes are
accepted when unambiguous. Commands exit `0` on clean verdicts and non-zero
when a forensic check fails — so traces slot into CI.

---

## Recording *your* agent (3 lines of integration)

AFR doesn't own your loop. It sells you two recording seams:

```python
from afr import Recorder, TraceStore, make_tools

store = TraceStore("./trace_store")                    # one session = one directory
rec   = Recorder(store, make_tools("./workspace"),      # tools registered + sandboxed
                 agent_meta={"name": "my-agent", "provider": "openai", "model": "gpt-4o"})

# seam 1: the model call — fn is ANY prompt->text callable
text = rec.llm(my_llm_function, step=step, prompt=prompt)

# seam 2: the tool call — intent, receipt, result, all on disk instantly
result, raw, ok, receipt = rec.tool_call(step, "fs.read", {"rel_path": "notes.txt"})
```

`my_llm_function` can hit a real provider, a local llama.cpp server, or a
human. **The recorder never calls out** — that independence is what makes
every trace provider-neutral.

---

## Use cases — who reaches for this and when

**1 · Debugging the run you can't reproduce.**
The trace is the replayable unit. `afr replay` rebuilds the step-by-step
plan (tool, args, result, receipt) without a GLM call, so you can walk a
*past* failure on a *presentation-room laptop*.

**2 · Trust review before authorizing tools.**
Every write records `existed_before`, byte counts, touched paths. Every
shell records cwd + command + returncode. You audit *evidence*, not vibes.

**3 · Model-version diffing.**
Record run A with model X, run B with model Y. `afr diff` aligns the plans
and reports: which steps matched, where args drifted, where results changed,
where one session kept working while the other stopped. This is the answer
to "what changed when the provider redeployed" — asked after every outage.

**4 · Trace-CI for agents.**
Because commands exit non-zero on drift, you can assert: *this agent's plan
must replay clean*, *this financial-tool plan may not change shape* — the
reverse of prompt-fiddling CI chaos.

**5 · Free speed: predictive prefetch.**
The engine predicts the next tool call from recorded traces' n-grams
(microsecond decision cost), speculatively runs only *read-only* tools in a
throwaway copy of the workspace, and publishes a cost ledger:

```json
{"mode": "live", "hits": 1, "misses": 0, "hit_rate": 1.0,
 "saved_ms": 2.63, "wasted_ms": 0.0, "net_ms": 2.63,
 "per_tool": {"fs.read": {"hit": 1, "saved_ms": 2.63}}}
```

On this repo's tiny demo agent, a hit saves ~2–3 ms per call. On real
agents (20–200 tool calls per loop, slow tools per call), the same
accounting shows whether prefetch pays — only when its numbers do.

**6 · Kid-simple corpus contribution.**
Every recorded trace is training data for the predictor (and, one day,
for playback-based agent benchmarks). Record once; teach all future runs.

---

## Architecture at a glance

```
 your agent loop (any code, any provider)
        │ prompt                       tool call
        ▼                                ▼
 ┌──────────────┐              ┌──────────────────┐
 │ Recorder.llm │              │ Recorder.tool_call│
 └──────┬───────┘              └────────┬─────────┘
        │ llm.request + llm.response    │ tool.intent (args + safety class)
        │   (verbatim facts)            │ tool.result (receipt + duration)
        └──────────────┬────────────────┘
                       ▼
              TraceStore  ─ trace.jsonl   (append-only, per-line hash chain)
                          └ manifest.json (digest, completeness, summary)
                                │
        ┌───────────────────────┼──────────────────────────┐
        ▼                       ▼                          ▼
   replay.py               diff.py                   prefetch.py
 "re-fly w/o the sky"    align 2 plans,            NGramPredictor →
 verify mode: re-run      decompose arg/         ShadowWorkspace copy →
 read-only steps          result drift           cost ledger (hits/misses)
        │                       │                          │
        └───────────────────────┼──────────────────────────┘
                                ▼
                       report.py → one HTML file
```

### The safety taxonomy (the load-bearing wall)

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
if any tool's class disagrees with the live registry. And shadow state is
taken at prediction time — the exact state the real call would have seen —
so a "right guess on wrong state" is impossible. Try breaking it:
`tests/test_prefetch.py::test_engine_refuses_to_speculate_writes` asserts
the file **does not exist** after the attempted speculation.

### On-disk format (boring on purpose — so you can `grep` it)

```
trace_store/20261023-022843-1632/
├── trace.jsonl      one JSON event per line, hash-chained
└── manifest.json    digest + completeness flag + run summary
```

```json
{"type":"tool.result","ts_ns":1769138733123000000,"eid":"e000009",
 "payload":{"step":2,"tool":"fs.read","args":{"rel_path":"notes.txt"},
  "receipt":{"kind":"fs_read","read":["notes.txt"],"n_bytes":111},
  "result":"q3 dates...","result_sha":"9f8b23c91a04c7d2",
  "duration_ns":213900,"ok":true}}
```

Any reader you write is ten lines. That portability is a *feature*.

---

## The cost model (how prefetch earns its keep)

For each prediction the ledger stores the *real* numbers:

- **saved_ms** — only on hits: the recorded `duration_ns` of the actual
  call (the latency the agent would have waited; claim has provenance).
- **wasted_ms** — only on misses (live mode): the real shadow-execution
  time, measured, using the last-known args for that tool — so we measure
  a plausible speculation, not a TypeError.

```
net per workload  =  hit_rate × avg_real_ms   −   miss_rate × avg_shadow_ms
prefetch pays  ⇔  hit_rate / (1 − hit_rate)  >   avg_shadow_ms / avg_real_ms
```

Every variable is in the ledger — compare predictors by the *same* math,
keep speculative mode OFF until a workload's ratio clears the bar.

---

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

---

## Repo tour

```
afr/                  the package (schema · recorder · replay · prefetch · diff · report · cli)
tests/                73 tests: schema, store, recorder, replay, prefetch, diff, report, cli
scripts/demo.py       the 8-step offline tour (idempotent — run it as often as you like)
examples/trace/       two committed recorded sessions + manifests
examples/report-*.html  the generated flight report (open in any browser)
results/              both cost ledgers + the trained predictor
docs/ARCHITECTURE.md  module-by-module design, invariants, and their prices
docs/cost-model.md    the accounting, with measured demo numbers
```

---

## FAQ for skeptical engineers

**Why not Slack it into LangSmith / AgentOps / Langfuse?**
Those are hosted *telemetry* platforms: dashboards over emitted spans. AFR
is a local-first *forensic format* — you own the file, `grep` wins,
replaying never hits the network, and 73 tests run in CI on your machine
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
agent runs, the six files of `afr/` will record. (Imports verified on
3.9 and 3.12; CI matrix runs both on Ubuntu and Windows.)

**What would make me star this repo?**
You're an agent-framework author who just spent an evening replaying a
mystery failure by hand. Add AFR, reply with `afr replay <id>` and a
receipt table. Star after it saves you the second evening.

---

## Roadmap

1. **Provider shims** — OpenAI-compatible + MCP tool recorder so real agent
   SDKs emit AFR-1 (schema is already reentrant: events are append-only,
   payloads are opaque to the store).
2. **Shell receipts v2** — strace/ETW wrappers to name every file a
   command actually touched, reads included.
3. **Prediction head v2** — same interface, small learned model; ledger
   decides which wins.
4. **Trace-CI action** — GitHub Action that replays a session's plan and
   fails the PR on shape change.

## Developing

```powershell
pip install -e ".[dev]"    # only pytest; runtime stays stdlib-only
pytest tests               # 73 tests
python scripts/demo.py     # fresh artifacts every run
```

## License

MIT. Built because agents deserve black boxes too.
