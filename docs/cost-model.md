# The cost model

Prefetch is a bet: spend some compute *now* on a guess, in exchange for
skipping latency *later* if the guess was right. This document gives the
accounting in full, so the ledger can be audited without reading a line
of code.

## The quantities

For each predicted call, the engine produces two numbers:

* **saved_ms** *(on a HIT only)* — the *actual* duration of the real tool
  call, as recorded by the trace (`tool.result.duration_ns`). It is what
  the call cost in reality, so the claimed saving has provenance rather
  than a model estimate; if the shadow result is reused, that duration is
  the latency the agent would have waited without prefetch.
* **wasted_ms** *(on a MISS, live mode only)* — the *actual* duration of
  the shadow execution of the wrong predicted tool, timed for real in the
  sandbox. In offline mode we cannot know a shadow's cost for a call that
  was never made, so we report 0 and say so (`"mode": "offline"`), with
  the field left in so the schema never silently changes meaning.

Derived per evaluation:

```
hit_rate = hits / (hits + misses)
net_ms   = saved_ms − wasted_ms
```

## The ledger file

`results/ledger-offline.json` and `results/ledger-live.json` (both
produced by `scripts/demo.py`):

```json
{
  "mode": "live", "predictor": "ngram-2", "p_min": 0.5,
  "predicted_chance": 2, "hits": 1, "misses": 0,
  "hit_rate": 1.0, "saved_ms": 2.86, "wasted_ms": 0.0, "net_ms": 2.86,
  "per_tool": { "fs.read": {"hit": 1.0, "miss": 0.0,
                            "saved_ms": 2.86, "wasted_ms": 0.0} },
  "refusals": [], "skipped": 1
}
```

Field notes:

* `predicted_chance` — number of real tool calls the predictor could
  have tried to front-run (i.e. every recorded tool step);
* `skipped` — steps where the predictor made no confident-enough *legal*
  guess (below `p_min`, or top guess was an unsafe class);
* `refusals` — names of unsafe tools the predictor top-ranked, recorded
  so the safety gate's relevance is measurable, not anecdotal;
* `per_tool` — hits stored under the *actual* tool name; misses stored
  under `(predicted) <tool>` to make the distinction grep-visible.

## Measured demo numbers (their honest scale)

From the demo run (Oct 2026, this repo, i5-12450H laptop, fresh workspaces):

| Mode | hit_rate | saved_ms | wasted_ms | net_ms |
| --- | --- | --- | --- | --- |
| offline (self-trained planner trace) | 1.00 | 1.89 | 0.0 | 1.89 |
| live (planner-trained, run on auditor trace) | 1.00 | 2.63 | 0.0 | 2.63 |

Read these as *plumbing validation*, not as the headline: the demo agent
makes 2–3 tool calls per turnout, so at most one prediction is possible
per run. The interesting regimes are real agents with 20–200 tool calls
per loop (IDE agents, MCP sessions), where the same accounting produces
the workloads the pitch is about. The engine is unchanged; only the
workload scale differs.

## The governing formula

A prefetched call is *cheap* when the shadow cost is low and the real
call is slow. For a tool mix m:

```
net(m) = Σ η_i − Σ (1−η_i) · w_j        where η_i ∈ {hit, miss}
       = hit_rate · avg_real_duration − miss_rate · avg_shadow_cost
```

So prefetch pays when:

```
hit_rate / (1 − hit_rate) > avg_shadow_cost / avg_real_duration
```

The ledger exposes every variable of that inequality per workload —
that's the artifact. Defaults stay OFF until a workload's own ledger
says its ratio clears the bar; the `p_min` threshold is the main dial
(lower = more speculative works, more waste).

## Safety taxonomy pricing note

Because only `read-only`/`pure` tools can be speculated:

* **a write-tool top-guess costs zero compute and zero risk** — it is a
  refusal recorded in `refusals`;
* shadow writes (in the copy) always vanish with the sandbox. There is
  no merge step, so a right-guess *on a wrong variant of state* is
  impossible by construction: shadow state is a snapshot taken *at
  prediction time*, exactly the state the real call would have seen.

## Tuning knobs, in order of influence

1. **p_min** (0.5 default) — lower invites more speculations at more waste.
2. **workload shape** — read-heavy loops (search, comprehension, review)
   benefit most; write-heavy loops mostly produce refusals.
3. **predictor quality** — swapped via the `predict/observe` interface;
   every candidate is graded by the same ledger, so comparisons are
   apples-to-apples.
