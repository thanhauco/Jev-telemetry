-- Jev enrichment tables. Raw logs live in otel.otel_logs (created by the OTel ClickHouse exporter);
-- these tables hold one row per cluster per run, so triage queries stay small and fast.

CREATE DATABASE IF NOT EXISTS otel;

CREATE DATABASE IF NOT EXISTS jev;

CREATE TABLE IF NOT EXISTS jev.log_clusters
(
    run_id         String,
    window_start   DateTime64(3, 'UTC'),
    window_end     DateTime64(3, 'UTC'),
    template_id    String,
    service        LowCardinality(String),
    template       String,
    level          LowCardinality(String),
    count          UInt64,
    first_seen     DateTime64(3, 'UTC'),
    last_seen      DateTime64(3, 'UTC'),
    examples       Array(String),
    trace_ids      Array(String),
    level_counts   Map(String, UInt64),
    sensitive_hits Map(String, UInt64)
)
ENGINE = MergeTree
PARTITION BY toDate(window_start)
ORDER BY (service, template_id, window_start)
TTL toDateTime(window_start) + INTERVAL 90 DAY;

-- One row per (cluster, question). Pin model_version: a version bump means re-running the golden set.
CREATE TABLE IF NOT EXISTS jev.cluster_judgments
(
    run_id        String,
    judged_at     DateTime64(3, 'UTC'),
    template_id   String,
    model_version LowCardinality(String),
    question_key  LowCardinality(String),
    question_type LowCardinality(String),
    value         String,
    p_yes         Nullable(Float64),
    probabilities Map(String, Float64),
    confidence    Nullable(Float64),
    source        LowCardinality(String)
)
ENGINE = MergeTree
PARTITION BY toDate(judged_at)
ORDER BY (template_id, question_key, judged_at)
TTL toDateTime(judged_at) + INTERVAL 90 DAY;

CREATE TABLE IF NOT EXISTS jev.routing_decisions
(
    run_id               String,
    window_start         DateTime64(3, 'UTC'),
    template_id          String,
    service              LowCardinality(String),
    level                LowCardinality(String),
    count                UInt64,
    decision             LowCardinality(String),
    reasons              Array(String),
    flags                Array(String),
    priority             Float64,
    category             LowCardinality(Nullable(String)),
    component            LowCardinality(Nullable(String)),
    expected_severity    Nullable(Float64),
    expected_user_impact Nullable(Float64),
    p_actionable         Float64,
    p_benign             Float64,
    p_root_cause         Float64,
    p_novel              Float64,
    p_sensitive          Float64
)
ENGINE = MergeTree
PARTITION BY toDate(window_start)
ORDER BY (window_start, decision, service)
TTL toDateTime(window_start) + INTERVAL 90 DAY;

CREATE TABLE IF NOT EXISTS jev.incidents
(
    run_id                 String,
    incident_id            String,
    start                  DateTime64(3, 'UTC'),
    end                    DateTime64(3, 'UTC'),
    services               Array(String),
    template_ids           Array(String),
    lines                  UInt64,
    priority               Float64,
    root_cause_template_id String,
    linked_changes         Array(String),
    top_hypothesis         String,
    narrative              String
)
ENGINE = MergeTree
ORDER BY (start, incident_id);

-- Latest label per template: the "semantic enrichment columns" to join and group by.
CREATE VIEW IF NOT EXISTS jev.template_labels AS
SELECT
    template_id,
    argMax(service, window_start)           AS service,
    argMax(decision, window_start)          AS decision,
    argMax(category, window_start)          AS category,
    argMax(component, window_start)         AS component,
    argMax(expected_severity, window_start) AS expected_severity,
    argMax(p_actionable, window_start)      AS p_actionable,
    argMax(p_benign, window_start)          AS p_benign,
    max(window_start)                       AS last_judged
FROM jev.routing_decisions
GROUP BY template_id;
