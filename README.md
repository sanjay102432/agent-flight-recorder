# Agent Flight Recorder (AFR)

**Receipt-level trace recording, deterministic replay, session diffing, and
predictive prefetch for AI agents.** Zero runtime dependencies. Runs on a
laptop. Tested on Windows, works wherever Python does.

---

## Why

Agent platforms keep logs. Logs are ideas about what happened. A **flight
recorder** is different: it keeps *receipts*, and from those receipts it can
do things logs structurally cannot:

| You want to... | Logs | Flight recorder |
| --- | --- | --- |
| Replay today's bad run step-by-step | tell you "roughly what happened" | walk the exact decision sequence, with every LLM answer verbatim |
| Prove what a tool touched | infer from stdout | read the receipt: which paths, how many bytes, what existed before |
| Diff run A vs run B across model versions | read both by eye | get an alignment table: same plan? which step drifted? |
| Make the *next* run faster | nothing | speculatively pre-run predicted read-only calls, with a published cost ledger of ms saved vs ms wasted |

The recorder is the trust anchor; everything else (replay, diff, prefetch)
is downstream machinery that only ever reads what landed in the trace.

## The four ideas, in one line each

1. **Record outcomes, not opinions.** Every event is a fact with a hash:
   the prompt sent, the model's text response, one tool call's arguments,
   receipt, result, and duration.
2. **Replay is a walk, not a re-run.** Replaying never calls the model or
   re-executes tools. It re-derives the plan from the trace. Records are
   facts, so the flight simulator is deterministic on any machine.
3. **Prefetch is gated by a taxonomy, not by trust.** Only *read-only*
   tools may ever run speculatively, in a disposable copy of the
   workspace. A write tool cannot be prefetched by construction, even if a
   model predicts it with probability 0.99.
4. **The cost ledger is the artifact.** Prefetch claims are numbers:
   hit rate, saved ms (from real recorded durations), wasted ms (real
   shadow-execution time on misses), and net ms. If numbers do not argue
   for prefetch, enable/disable defaults off and show the honest table.

## 30-second quickstart

Requires Python 3.9+. No pip installs needed to *run it* (stdlib only);
install for the `afr` shortcut only if you want one:

```powershell
# from this repo
python -m pytest tests            # 73 tests, ~30 s
python scripts/demo.py            # the 8-step tour: record→replay→verify→integrity→forensics→prefetch×2→diff→report
```

`scripts/demo.py` runs fully offline (a scripted mock provider plays the
model), and finishes by writing `examples/report-<session>.html` — open it
in a browser. It is one file, no server, no network calls.

### Using the recorder in *your* agent

```python
from afr import Recorder, TraceStore, make_tools

store = TraceStore("./trace_store")           # one session = one directory
rec   = Recorder(store, make_tools("./workspace"),
                 agent_meta={"name": "my-agent", "provider": "openai", "model": "gpt-4o"})

rec.open_run()
text = rec.llm(my_llm_function, step=1, prompt="Summarize notes.txt")
tool, args = parse_tool_call(text)            # your decision layer
rec.tool_call(1, tool, args)                  # intent+receipt+result, on disk instantly
rec.close_run(ok=True, reason="done")
```

`my_llm_function(prompt) -> str` can be anything — OpenAI-compatible
endpoint, a local llama.cpp server, a human. The recorder never calls out;
that independence is what makes the trace provider-neutral.

## The trace, visually

Each session is a directory: `trace.jsonl` (the events) + `manifest.json`
(metadata + running content hash). Every event is one JSON line:

```json
{"type": "tool.result", "ts_ns": 1753570227, "eid": "e000009", "depth": 0,
 "payload": {"step": 2, "tool": "fs.read", "args": {"rel_path": "notes.txt"},
  "receipt": {"kind": "fs_read", "read": ["notes.txt"], "n_bytes": 111},
  "result": "q3 dates: release candidate on 14 Aug...",
  "result_sha": "9f8b23c91a04c7d2", "duration_ns": 1234567, "ok": true}}
```

Every line also carries `prev_line_hash` — a running SHA-256 digest — so
deleting or editing an event is detectable (`afr verify`). Prompts and
responses are hashed identically, which is what the diff machinery aligns on.

## The six commands

| Command | What it does | Exit code ≠ 0 means |
| --- | --- | --- |
| `afr record [WORKSPACE]` | runs the demo agent, writes the trace | agent run ended not ok |
| `afr replay SESSION` | deterministic walkthrough; prints the plan | plan has breaks (intent without result) |
| `afr replay SESSION --verify --workspace W` | also re-runs **read-only** steps and compares now | something drifted |
| `afr verify SESSION` | mononicity + chain-hash tamper check | trace was edited |
| `afr prefetch SESSION --workspace W` | prices prefetch; writes the ledger JSON | no session / bad workspace |
| `afr diff A B` | aligns two plans; reports arg/result drift, extra steps | — (report-only) |
| `afr view SESSION` | writes a self-contained HTML timeline report | — |

Session ids are directory names (`20261023-022514-2626`); prefixes are
accepted when unambiguous (`afr list` prints them all).

## How prefetch works

```
apply loop:
  ┌─ LLM turn ──▶ predictor(history so far) ──▶ guess: "fs.read x" (p=0.83 …)
  │                    │
  │                    └── taxonomically safe? ──▶ shadow-copy workspace,
  │                                                  run guess there,
  │                                                  timer on.
  ▼
  agent calls tool
  ├── guess matched ─▶ HIT: shadow result *is* the answer; save_ms += the
  │                        real call's own recorded duration
  └── guess missed ──▶ MISS: wasted_ms += shadow timer; agent proceeds as usual
```

The safety gate in code, not prose (`afr/prefetch.py`):

- the allowlist is **frozen at engine construction** from the taxonomy;
- `speculate(tool)` re-checks the class at call time and raises
  `SpeculationRefused` for anything outside *read-only* — there is no
  override flag, on purpose;
- the shadow registry is minted by a **tool factory bound to the shadow
  root**, and refuses to run if any tool's class disagrees with the live
  registry. Same name must mean same safety class, or no speculation;
- the file tools bind to a tool *factory*, so "run in the shadow" actually
  reads and writes the copy. Writes are discarded on close; nothing in the
  shadow sandbox leaves it. These tests live in
  `tests/test_prefetch.py::test_shadow_registry_rebinds_tools` and
  `::test_engine_refuses_to_speculate_writes`.

Measured on this repo's own demo (see `results/ledger-offline.json`,
`results/ledger-live.json`): a planner trace trained on itself yields
**100% hit rate on the second read call** with ~1–3 ms saved per hit — a
tiny workload, reported honestly; the interesting regime starts with real
multi-tool agent loops. Scoring formula lives in
`docs/cost-model.md` (saved/wasted/net per tool class, hit rate vs p_min).

## The safety taxonomy

| Class | Examples | May be speculated? |
| --- | --- | --- |
| `read-only` | `fs.list`, `fs.read` | **yes** |
| `pure` | deterministic helpers, no I/O | yes |
| `write` | `fs.write`, `shell` | **never** |
| `network` | fetch APIs, webhooks | never |
| `dangerous` | `fs.delete` | never; alert on prediction attempt |

Registering the same tool name with a *different* class is an error at
`register()` time, not a race the recorder has to catch later.

## Honest limits (read before trusting)

- **Shell receipts are coarse.** The `shell` tool records command, cwd,
  returncode — not every file the command touched. Run-level environment
  diffing catches material changes, but not all. Deeper tracing needs
  OS-level hooks (e.g. eBPF on Linux; on Windows, ETW), planned next.
- **Tools must be registered in AFR to be recorded.** The `mock` provider
  records what you pass `rec.llm`, so a provider doing its own I/O inside
  a model call is invisible to tool lanes (model responses are still
  recorded verbatim).
- **The predictor is order-2.** It is deliberately tiny: microsecond
  decision cost. For workloads with more tool diversity (IDE agents,
  multi-tool MCP sessions), a learned classifier or small LLM runs at the
  same `predict/observe` interface and gets graded by the same ledger.
- **Not an audit-log** under any regulator's definition yet: no epochs,
  no iterators, no monotonic counter. `afr verify` catches casual edits;
  a real audit-log needs a release of the receipt chain.
- **Workspace snapshots are copytree.** Cheap agent workspaces are files;
  a huge tree makes snapshots slow. Replace `ShadowWorkspace._copytree`
  for Btrfs ref-links or overlay mounts — the rest of the engine
  doesn't change.

## Why not just use `--verbose` logs?

Because the property that powers replay, diff, prefetch, and tamper
checks is *the shape of the trace*, and verbose logs do not have a shape.
If your log format needs a parser to read, it isn't a flight recorder; a
flight recorder is a format you can grep at 2 a.m. and validate with a
10-line script. AFR-1 is deliberately boring JSON so any reader code is
10 lines.

## Repository tour

```
afr/                  the package (schema, recorder, replay, prefetch, diff, report, cli)
tests/                73 tests: schema, store, recorder, replay, prefetch, diff, report, cli
scripts/demo.py       the 8-step offline tour (~5 s)
examples/trace/       two recorded sessions from the tour + their manifests
examples/report-*.html  the generated flight report (open in any browser)
results/              the two prefetch ledgers + the trained predictor
docs/ARCHITECTURE.md  module-by-module design and dataflow
docs/cost-model.md    the accounting behind prefetch, with measured demo numbers
```

## Roadmap

- provider shims: OpenAI-compatible + an MCP *tool* recorder so real
  agent SDKs land in AFR-1 with no code changes (schema is already
  reentrant: events are append-only, one agent's payload is opaque to
  the store).
- shell *receipts v2*: strace/ETW wrappers to name everything a command
  actually touched, including reads.
- real-LLM prediction heads behind the same `predict/observe` interface
  (costs get compared in the ledger, not vibes).
- pg + parquet writer options for teams that want queue/DB backends
  instead of directories.
- the `verify` head grows into *trace-CI*: assert that a recorded
  financial-transaction agent produced the same plan twice.

## Contributing / developing

```powershell
pip install -e ".[dev]"     # only for dev deps (pytest)
pytest tests
```

Everything is stdlib-only at runtime so the recorder can be vendored into
fragile agent hosts (a subprocess with 3 vendored files can emit traces).

## License

MIT — see [LICENSE](LICENSE).
