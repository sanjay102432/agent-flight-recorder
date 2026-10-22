# Architecture

This document explains every module in the order data flows through them,
what invariant each is responsible for, and why the seams are where they
are. Read `README.md` first if you just want the tour.

```
what the agent does                 what lands on disk
────────────────────                ─────────────────────
agent loop (any code)
      │  prompt
      ▼
Recorder.llm()          ─────────▶  llm.request   → prompt + sha + provider
      │  response                     llm.response  → text + sha + tokens (FACT)
      │  tool call                    tool.intent   → tool + args + args_sha + safety
      ▼
Recorder.tool_call()    ─────────▶  tool.result   → receipt + result + duration (FACT)
                                           │
                                           ▼
                                    TraceStore        trace.jsonl   (one dir per session)
                                    (append-only)     manifest.json (hashes, completeness)
                                           │
                    ┌──────────────────────┼───────────────────────┐
                    ▼                      ▼                        ▼
              replay.py               diff.py                prefetch.py
            (plan walk,               (alignment of         (NGramPredictor →
             verify mode)              digests per step)     ShadowWorkspace →
                    │                                          cost ledger)
                    └──────────────────────┴───────────────────────┘
                                           ▼
                                      report.py → one-file HTML
```

## Module map

### `afr/schema.py` — the contract (no dependencies)

Defines **AFR-1**: the closed set of event types, their required payload
fields, the validation rules, and read/write helpers. Everything imports
this module; it must never import anything else (no cycles).

Invariants owned here:

- event envelope: `v`, `type`, `ts_ns`, `eid`, `run_id`, `session_id`,
  `depth`, `payload`;
- `validate()` is the single gate — unknown types, non-int timestamps,
  missing required fields → `SchemaError` *at write time*;
- `text_sha256()` is the 16-hex-truncation used everywhere else
  (`result_sha`, `args_sha`, `prompt_sha`, `response_sha`);
- `check_monotonic()` only as a helper; the store re-verifies on load.

### `afr/store.py` — the append-only file layer

One session per directory, `trace.jsonl` + `manifest.json`.

- `append()` validates, hashes, and writes **immediately** (no batch,
  no flush interval): crash mid-run leaves a readable prefix;
- every line embeds `prev_line_hash`; the manifest records a running
  content digest — `verify_integrity()` recomputes both and reports
  `chain_ok`;
- reopening an existing session (e.g. after a crash) runs `read_events()`
  to get the true count, then *merges* the old manifest instead of
  clobbering it (`complete: true` survives; a regression test covers it);
- `close()` appends `run.end`, flips the manifest's `complete` bit, and
  embeds a `summary`.

Why JSONL: append-only safety, grep/jq ergonomics, trivial hash-chaining.
SQLite would be safer for trillion-event corpora; agent sessions top out
at thousands of events, and grep-ability beats indexes at this scale.

### `afr/tools.py` — the safety taxonomy + sandboxed filesystem tools

The taxonomy is the system's load-bearing safety element:

- every tool registered with exactly one class
  (`read-only`/`pure`/`write`/`network`/`dangerous`);
- `SPECULATIVE_CLASSES = {read-only, pure}` is the only gate the prefetch
  engine obeys — re-checked at call time, not just at construction;
- re-registering the same name with a changed class **raises**;
- the `resolve_under_root()` helper forbids absolute paths and `..`
  escapes; `fs.*` tools are all bound to a workspace directory.

### `afr/recorder.py` — the recording seam

`Recorder` wraps the two side-effectful surfaces of an agent loop:

- `llm(fn, step, prompt)` records `llm.request` + `llm.response` before
  returning. The Recorder **still allows the test to see the model in
  offline tests**: responses are plain text, produced by *your* `fn`
  (a real provider, a mock, a human). No network knowledge inside AFR.
- `tool_call(step, name, args)` records `tool.intent` → executes →
  records `tool.result`. Exceptions are recorded as results with
  `ok=false` and re-raised as `ToolExecutionError` (tool errors are
  facts, and the loop must be able to react).
- `start_step` / `end_step` bracket each iteration; `open_run` /
  `close_run` produce the run envelope.

Receipt sources: filesystem tools return dedicated receipts (`fs_read`,
`fs_write` with `existed_before`, byte counts); the `shell` tool returns
`shell_execution` with cwd/command/returncode — coarse by design and
documented as such (the *whole* FS diff catches material side effects).

### `afr/replay.py` — the deterministic walk

`extract_plan()` compresses the flat event list into one `StepPlan` per
loop step (tool, args, receipt, result, response). `replay()` walks the
plan in two modes:

- **recorded (default)**: executes nothing. Breaks (intent with no
  result) are reported, never papered over.
- **verify**: additionally re-executes *only* tools whose live class is
  in `SPECULATIVE_CLASSES`, compares canonical results to the recorded
  ones, and counts each mismatch as a deviation with a note quoting both
  values. Write/network tools are skipped with an explanatory note —
  a policy decision you want to *see* in the notes, not absorb silently.

The report's headline number is `deviations` (0 = plan replays clean) and
`verified_calls` (how many read steps were re-checked).

### `afr/diff.py` — the plan-diff machinery

- `digest_steps()` reduces each step to `(step, tool, args_sha,
  result_sha, response_sha)`;
- `diff_sessions()` aligns two digest sequences on the **tool name**
  (SequenceMatcher), then reports inside each aligned match:
  `arg_drifts` (same tool, different args) and `result_drifts` (same
  tool+args, different result);
- `similarity` = matched steps / max(lenA, lenB).

Aligning *only* on tool names is deliberate: if the alignment signal
included result hashes, any workspace change (a file existed, a listing
grew) would register as a plan change and make every review noisy.

### `afr/prefetch.py` — the speculative engine (see docs/cost-model.md)

- `NGramPredictor`: order-2 Markov counts over tool names with an
  order-1 fallback; `predict/observe` interface so heavier predictors
  slot in behind the same contract; disk-loadable;
- `ShadowWorkspace`: per-speculation copytree; deleted on exit; the only
  way shadow code touches "the workspace" is through a *shadow registry*;
- `PrefetchEngine`:
  - `allowed` = frozen frozenset from the taxonomy at construction;
  - `predict_next(history)` silently refuses unsafe top guesses
    (returns None) but will consider lower-ranked *safe* tools;
  - `speculate(tool,args)` = shadow **registry minted inside the
    sandbox**, class re-checked, timer on, result or error returned;
    – on SpeculationRefused for unsafe candidates: counted as a
    `refusal` for observability;
  - `evaluate()` walks events (or replays live) and produces the
    **cost ledger**: `hits/misses/skipped`, `saved_ms` (recorded
    durations of hit calls), `wasted_ms` (only in live mode real shadow
    timers; offline mode: 0 for misses), `net_ms`, plus per-tool
    breakdowns and `refusals`.
- The ledger is JSON, saved with `save_ledger()`, and is exactly the
  artifact the cost model doc publishes.

Failure bookkeeping: refusals (the predictor guessed an unsafe tool) are
recorded per evaluation, so "how often did the safety gate matter?" is a
number, not an anecdote.

### `afr/report.py` — the flight report (one HTML file, no server)

- `_digest()` projects events into (a) timeline dots and (b) per-step
  plan rows; both carry `eid`s so the UI cross-animates on click;
- template is self-contained: no CDN, no fetch, no build step; the
  payload is embedded JSON;
- color mapping: LLM turns = blue lane; tool results colored by safety
  (`unsafe` class when safety ≠ read-only), errors red; plan rows list
  receipt kinds beneath each step.

### `afr/cli.py` — commands and identity plumbing

`record`, `replay [--verify]`, `verify`, `list`, `prefetch
[--live] [--train] [--out]`, `diff [--json]`, `view [-o] [--open]`.
Session references accept exact ids or unambiguous prefixes. `main()`
maps command exit codes (0 ok, 1 verdict-was-negative/drift, 2 bad input)
so shell scripts can branch on the forensic verdict.

## Design decisions and their price

| Decision | Reason | Paid cost |
| --- | --- | --- |
| JSONL over SQLite | append-only + grep-able + hash-chain simple | no random access; index scanning is O(N) |
| Inlined receipts inline results | self-contained steps for replay/diff | big tool outputs bloat lines; truncate heads at copy-time reads |
| 16-hex hashes | short, stable, grep-able | theoretical collisions; fine per-machine-scale workloads |
| Toolkit register-time class lock | registration bug, not runtime race | tool surface must be declared per workspace |
| Tool *factory* for shadow execution | sandboxing correctness | implementors must register tools through `make_tools`-style factory |
| Predictor = n-gram | decision cost in µs, honest train-from-trace story | misses complex, tool-rich loops |
| HTML report over dashboard app | one file, no server, no live deps | no live tailing; sampling via CLI export only |

## Test layout (what is enforced where)

| Test file | Enforces |
| --- | --- |
| `test_schema.py` | AFR-1 contract: required fields, types, round-trips, ASCII JSON |
| `test_store.py` | append-only, eid sequencing, manifest merge on reopen, chain tamper detection |
| `test_recorder.py` | intent/result pairing, receipts (existed_before), error-as-fact, path sandbox |
| `test_replay.py` | recorded walk clean, verify drift bites (two honest drifts), unsafe skips visible |
| `test_prefetch.py` | predictor math + persistence, shadow isolation (files vanish), refusal of write/unregistered, ledger math, serializable ledger |
| `test_diff.py` | tool-name alignment, arg/result drift, similarity, kept-going |
| `test_report.py` | self-contained HTML, parseable embedded JSON, unsafe tagging |
| `test_cli.py` | command matrix exit codes incl. tamper → non-zero |
| `test_scenarios.py` | the builder scenario's read bias and the write refusal that follows |
