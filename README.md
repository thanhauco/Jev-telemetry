# Jev-telemetry

Jev as a parallel judgment layer for logs and telemetry.

[Jev](https://openrouter.ai/docs/guides/community/jev) (TypeSafe's System One model) answers typed questions, Choice, Score and Noul (yes/no), and returns calibrated probabilities instead of prose. It runs those questions in parallel and in isolation against the same state. This repo puts Jev between cheap deterministic reduction and expensive reasoning:

```
Raw logs / traces (OTel)
   │
   ▼
[L0] Deterministic reduction      scrub PII/secrets → Drain template mining → counts, levels, trace ids
   │   131k lines → 16 clusters on the sample data
   ▼
[L1] Jev fan-out                  9-question pack per cluster, one parallel request each, cached by template
   │
   ▼
[L2] Deterministic router         thresholds + rules check + noisy-OR → escalate / triage / review / monitor / suppress
   │
   ├─► suppress → hidden, counted
   ├─► known issue → runbook / ticket
   └─► escalate ▼
[L3] Correlation                  time+topology-pruned pairs → same-issue collapse, root-cause rank,
   │                              change correlation, hypothesis scoring, timeline
   ▼
[L4] Narrative (top-K only)       deterministic template, or Claude
   │
   ▼
[L5] Eval loop                    golden set → precision/recall, false-suppression rate, ECE, reliability curves
```

Jev never sees the raw firehose. It is not a log reader, and its outputs are probabilities, not explanations.

## Quickstart

No API key needed: the offline `heuristic` judge stands in for Jev.

```bash
make install           # uv venv + editable install
make demo              # generate sample logs with an injected incident, run L0-L4
make eval              # golden-set eval: quality, calibration, false suppression
make test
```

Output from `make demo` on the bundled scenario. A payments deploy shrinks the DB pool, which cascades to checkout and the gateway, and an unrelated crash happens in inventory:

```
L0  131,422 lines -> 16 clusters (8214x), scrubbed 5,285 lines {'EMAIL': 5280, 'CARD': 120, 'API_KEY': 5}
L2  {'escalate': 2, 'triage': 4, 'review': 1, 'monitor': 1, 'suppress': 8}  suppressed 126,056 lines

decision    service      level    count category     sev  P(act)  template
escalate    payments     ERROR     1000 capacity     2.8    0.79  db pool exhausted: <NUM> requests waiting (max_connections=<NUM>)
triage      checkout     ERROR     1500 timeout      2.3    0.73  timeout calling payments: deadline exceeded after <DUR>
...
L3  inc-2aa0931779: 4 clusters across ['api-gateway', 'checkout', 'payments'], 4,875 lines
      rc-score   0.81  [payments] db pool exhausted: <NUM> requests waiting (max_connections=<NUM>)
      change     0.98  [payments] payments v2.14.0: reduce db pool max_connections from 50 to 5
```

A second run hits the cache for every cluster (`cache hit 100%`, 0 requests).

### With Jev

```bash
export TYPESAFE_API_KEY=...            # early access; join the waitlist
jev-telemetry estimate --input samples/logs.jsonl      # dry run: tokens, cost, wall time
jev-telemetry eval --golden samples/golden.jsonl --backend jev
jev-telemetry run  --input samples/logs.jsonl --changes samples/changes.jsonl --backend jev
```

The REST client ([jev_http.py](src/jev_telemetry/judge/jev_http.py)) targets `POST https://api.typesafe.ai/v1/systemone` with a pinned model (`jev-1.13.0`). The request and response shapes come from launch-week docs and tutorials, so check them against the current API reference before relying on them. If Jev fails, the call retries with jitter, then falls back to the heuristic judge. Fallback answers are flagged, never cached, and never allowed to suppress anything.

### Full stack (OTel → ClickHouse → Grafana)

```bash
make up                # ClickHouse, OTel Collector, Grafana, jev-worker (micro-batch every 60 s)
make replay            # send the sample logs over OTLP with timestamps rebased to now
open http://localhost:3000   # Grafana admin/admin → Jev → "Jev triage"
```

The worker reads `otel.otel_logs` window by window and writes four enrichment tables: `jev.log_clusters`, `jev.cluster_judgments`, `jev.routing_decisions` and `jev.incidents`. It also maintains a `jev.template_labels` view. Because these are plain SQL tables, you can group, join and build dashboards on Jev's labels. See [docs/architecture.md](docs/architecture.md) for example queries.

## Commands

| Command | What it does |
|---|---|
| `generate-sample` | Synthetic logs, deploy changes and a 48-row starter golden set |
| `run` | L0–L4 on a JSONL or plain-text log file; writes JSONL tables and/or ClickHouse |
| `stream` | Micro-batch worker over ClickHouse `otel_logs` |
| `estimate` | Dry-run tokens, cost and wall time, accounting for the cache |
| `eval` | Golden-set report; `--max-false-suppression 0` fails CI if a real issue gets suppressed |
| `export-golden` | Dump clusters as unlabeled golden rows for hand-labeling |
| `replay` | Push a JSONL log file to an OTLP/HTTP collector |
| `init-clickhouse` | Apply `deploy/clickhouse/init.sql` to an existing cluster |

## Layout

```
config/question_pack.yaml      L1 starter pack: category, component, severity, user_impact, actionable,
                               benign, root_cause, novel, sensitive
config/jev-telemetry.yaml      judge, cache, router thresholds, topology, narrator, pricing, sink
src/jev_telemetry/
  reduce/                      L0: scrub.py, drain.py, reducer.py
  judge/                       L1: base.py (interface), jev_http.py, heuristic.py, cache.py, fanout.py
  router.py                    L2: decisions, guardrails, noisy-OR window summary
  correlate.py                 L3: pair pruning, alert collapse, root cause, changes, hypotheses, timeline
  narrate.py                   L4: template narrator, Claude narrator
  eval/                        L5: golden set, metrics (precision/recall, ECE, Brier, reliability), runner
  pipeline.py, cli.py, sinks.py, sources.py, clickhouse.py, otlp.py, estimate.py, sample_data.py
deploy/                        docker-compose, OTel Collector config, ClickHouse schema, Grafana
```

## Guardrails built in

- **No single-probability suppression.** Suppression requires high P(benign), low P(actionable) and a deterministic rules check (allowlisted pattern or repetitive chatter). It never applies to ERROR/FATAL, fallback answers or clusters flagged sensitive. Suppressed lines are counted, not dropped.
- **Uncertainty goes to the cascade.** Probabilities within the band around a threshold route to `review`, the LLM or human tier.
- **Leaks are bugs.** If the scrubber caught a card, key or token, the cluster is triaged so the logging call gets fixed at source.
- **Privacy in three layers.** The collector redacts bodies, the worker's scrubber runs before clustering, and the `sensitive` Noul checks what is left. Clusters flagged sensitive never send example lines to the narrator.
- **Vendor isolation.** Everything above L1 talks to the `Judge` protocol, so Jev, a local classifier or an LLM can be swapped in without touching the pipeline.
- **Version pinning.** Cache keys include the model version and question fingerprint. A bump invalidates the cache, and the golden set should be re-run.

## Known limits

- **The heuristic judge is a fallback and a demo tool, not a model.** It is keyword-based and uncalibrated, so don't tune thresholds against it. In the demo it suppresses the free-text address leak (`printing shipping label for <name>, <street>`), which no regex catches. Telling those apart is the job of the `sensitive` Noul on real Jev, and the golden set includes that case so `eval` reports the gap (sensitive recall 0.0 on the heuristic).
- **Jev is early access.** Pricing ($0.042 per million input tokens), the 1,200 requests per minute limit and latency figures are self-reported launch-week numbers. Measure on your own logs: `estimate` first, then `eval`.
- **Calibration holds across groups, not per row.** Read the reliability curves in `eval --json`, not single probabilities.
- **Not real-time.** Micro-batching runs with a 60-second window plus lag. Keep it out of control loops.
- Template ids are stable within a process and across identical inputs. A long-running worker keeps its miner across windows, but templates can still drift as new variants merge.

## Sizing for about 10k users

Typical load is 10k daily active users × about 200 requests × 10–20 lines, which comes to about 20M lines and 5–10 GB raw per day, or 0.5–1 GB compressed in ClickHouse. One 8 vCPU / 32 GB node covers it. After template dedup, Jev sees a few thousand to tens of thousands of distinct clusters a day, which at launch pricing is a few dollars a day or less. Kafka isn't needed below about 100 GB per day.

## Roadmap

| Phase | Use this repo for | Exit criteria |
|---|---|---|
| 0. Setup | `export-golden` on 2–4 weeks of incident logs, then hand-label 300–1000 rows | Taxonomy agreed and golden set labeled |
| 1. Offline eval | `estimate`, then `eval --backend jev --json`; tune `router:` thresholds | Precision on actionable/noise at target, and a calibration curve you trust |
| 2. Shadow | `make up` with `JEV_BACKEND=jev`; tables and dashboard only, on-call untouched | Shadow output matches or beats human triage on a sample |
| 3. Assist | Triage board, incident collapse and hypotheses shown to on-call | Time to first hypothesis drops, false suppression stays low |
| 4. Guarded automation | Act on `suppress` and `known_issue` decisions, with a kill switch | Override rate stays low and the audit trail is complete |
