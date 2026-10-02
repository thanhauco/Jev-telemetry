# Architecture notes

## Layer contracts

| Layer | Input | Output | Code |
|---|---|---|---|
| L0 reduce | `LogRecord` stream | `Cluster` list + `ReduceStats` | `reduce/` |
| L1 judge | `JudgeTask(subject_id, state, questions)` | `{subject_id: {question_key: Answer}}` | `judge/fanout.py` |
| L2 route | `Cluster` + answers | `Routed` (decision, reasons, flags, priority) + `WindowSummary` | `router.py` |
| L3 correlate | surfaced `Routed`, `Change` list | `Incident` list | `correlate.py` |
| L4 narrate | top-K `Incident` | Markdown | `narrate.py` |
| L5 eval | golden JSONL | `EvalReport` | `eval/` |

### L0

- The scrubber runs first. Clusters never hold an unscrubbed line, and the per-kind hit counts are kept on the cluster as `sensitive_hits`.
- Masking (`<NUM>`, `<DUR>`, `<UUID>`, `<IP>`, `<HEX>`) happens before Drain, so variants merge early.
- `template_id = sha1(service, final template)[:16]`. Ids are assigned after the whole batch is mined, because templates keep generalizing while lines arrive.

### L1

- One request per cluster carries the whole pack. Jev runs the questions in parallel and in isolation server-side, so each question in `question_pack.yaml` must stand on its own.
- Cache key: `(subject_id, model_version, question_fingerprint)`. Editing a question's wording or options changes its fingerprint, so only that question is re-asked.
- Retry policy: 429, 5xx, timeouts and transport errors retry with full-jitter exponential backoff. Other 4xx errors go straight to the dead-letter queue and the fallback judge.
- The state carries the occurrence count, but the cache keys on the template. This is deliberate ("judge the template, carry the count"). If you want counts to change answers, add a count bucket to `subject_id`.

### L2 decision order

1. Missing `actionable` or `benign` answers → `review`
2. `known_issues[template_id]`, or a confident `runbook` choice → `known_issue`
3. The scrubber caught high-risk data (card, key, token, SSN) and the cluster isn't already escalation-worthy → `triage`
4. P(benign) ≥ 0.95 and P(actionable) ≤ 0.10, and the rules check passes, and the answers are not from the fallback judge, and the cluster isn't flagged sensitive → `suppress`
5. P(actionable) ≥ 0.6 and (P(novel) ≥ 0.5 or E[severity] ≥ 2.5) → `escalate`; otherwise P(actionable) ≥ 0.6 → `triage`
6. Within ±0.1 of a threshold → `review`
7. Otherwise → `monitor`

Window aggregates: expected actionable clusters = Σ P(actionable), and P(any severe) = noisy-OR over P(severity ≥ 3).

### L3

- Pairs are pruned to clusters first seen within `max_gap_s` of each other on adjacent services in `topology`, capped at 2,000 and ranked by combined priority. The `same_issue` Noul then runs on the survivors and union-find collapses them into incidents.
- The root-cause score is P(root_cause) × (1 + 0.5 / (1 + first-seen rank)). It is a ranking score, not a probability, and can exceed 1.
- Changes are paired with clusters that started 0–60 minutes after the change, and one link is kept per change.
- Hypotheses come from the narrator: Claude when configured, otherwise templates built from the root-cause candidates and changes. Jev scores each one against the incident's evidence bundle.

## Useful queries

```sql
-- Triage view: what needs eyes in the last hour
SELECT decision, service, category, sum(count) AS lines, max(p_actionable) AS p_act, any(template_id)
FROM jev.routing_decisions
WHERE window_start > now() - INTERVAL 1 HOUR AND decision IN ('escalate', 'triage', 'review')
GROUP BY decision, service, category, template_id
ORDER BY max(priority) DESC;

-- Alert reduction ratio per day
SELECT toDate(window_start) AS day,
       count() / greatest(countIf(decision IN ('escalate', 'triage')), 1) AS clusters_per_surfaced
FROM jev.routing_decisions GROUP BY day ORDER BY day;

-- Cost proxy and fallback share per model version
SELECT model_version, countIf(source = 'fallback') / count() AS fallback_share, count() AS answers
FROM jev.cluster_judgments GROUP BY model_version;

-- Raw lines for an escalated template (join back through the examples' trace ids)
SELECT l.Timestamp, l.ServiceName, l.Body
FROM otel.otel_logs l
WHERE l.TraceId IN (SELECT arrayJoin(trace_ids) FROM jev.log_clusters WHERE template_id = '<id>')
ORDER BY l.Timestamp LIMIT 200;
```

## Swapping the judge

Implement the `Judge` protocol in `judge/base.py`: a `name`, a `model_version`, `async ask(state, questions) -> Judgment` and `async aclose()`. Return one `Answer` per question: the option key for a Choice, the level index plus probabilities for a Score, and P(yes) for a Noul. Then register it in `make_judge`. The cache, fan-out, router and eval work unchanged.
