# Observability — Solomon Harness v1.22.0+

> **Phase 4.1–4.12 — Production observability** для Solomon Harness. Structured JSONL logs, OpenTelemetry-compatible traces, Prometheus `/metrics` endpoint, deep health checks (liveness/readiness/deep + 8 subsystem probes), per-task cost tracking (per-model breakdown), per-tool latency histograms, **admin JSON endpoints** (Phase 4.11, RBAC-gated). Построено поверх Phase 4.0 hooks framework.

---

## Содержание

1. [Что такое observability](#1-что-такое-observability)
2. [5 компонентов](#2-5-компонентов)
3. [JsonlLogger — structured logs](#3-jsonllogger--structured-logs)
4. [PrometheusMetrics — `/metrics` endpoint](#4-prometheusmetrics--metrics-endpoint)
5. [OTelTracer — distributed tracing](#5-oteltracer--distributed-tracing)
6. [HealthChecker — live/ready/deep + 8 probes (Phase 4.9)](#6-healthchecker--liverreadydeep--8-probes-phase-49)
7. [CostTracker — per-task cost (per-model breakdown, Phase 4.9)](#7-costtracker--per-task-cost-per-model-breakdown-phase-49)
8. [Конфигурация](#8-конфигурация)
9. [Admin endpoints (Phase 4.11)](#9-admin-endpoints-phase-411)
10. [Примеры](#10-примеры)
11. [Troubleshooting](#11-troubleshooting)
12. [См. также](#12-см-также)

---

## 1. Что такое observability

**Observability** = три «столпа» production visibility:

| Столп | Инструмент | Что даёт |
|-------|------------|----------|
| **Logs** | `JsonlLogger` → `data/logs/harness-YYYY-MM-DD.jsonl` | Per-event структурированные записи с trace_id/span_id. Grep-friendly, NDJSON. |
| **Metrics** | `PrometheusMetrics` → `GET /metrics` | Счётчики (requests, llm_calls), гистограммы (latency), датчики (active_sessions, queue_depth). |
| **Traces** | `OTelTracer` → OTLP collector | W3C `traceparent` header, span tree: HTTP request → agent loop → LLM call → tool call → hook dispatch. |

**Дополнительно:**

- **Health checks** (`HealthChecker`) — три эндпоинта: `/health/live` (liveness), `/health/ready` (readiness с probe'ами), `/health/deep` (полная диагностика).
- **Cost tracking** (`CostTracker`) — USD стоимость LLM-вызовов по model × tier. Видна в metrics (`harness_llm_cost_total_usd`) и logs.

**Trust boundary:** `harness/observability/*` НЕ импортирует `harness.agents`, `harness.server`, или `harness.hooks` (AST test enforced — `tests/test_observability_trust_boundary.py`).

**Graceful degradation:** Если `prometheus_client` или `opentelemetry-api` не установлены — модули автоматически становятся no-op (`metrics.render() = b""`, `tracer.start_span() → NoOpSpan`). Zero overhead в dev, opt-in в production.

---

## 2. 5 компонентов

| Модуль | Назначение | Default | Opt-in |
|--------|------------|---------|--------|
| `JsonlLogger` | Structured NDJSON logs | **ON** (always) | Off через `observability_jsonl_enabled=False` |
| `PrometheusMetrics` | `/metrics` Prometheus endpoint | OFF | On через `observability_prometheus_enabled=True` (+ `pip install prometheus-client`) |
| `OTelTracer` | OTel-compatible spans | OFF | On через `observability_otlp_enabled=True` (+ `pip install opentelemetry-api opentelemetry-sdk`) |
| `HealthChecker` | `/health/live` `/health/ready` `/health/deep` | **ON** (always) | — |
| `CostTracker` | Per-task USD cost | **ON** (always) | Off через `observability_cost_enabled=False` |

**0 new required deps.** Все опциональные зависимости в `[observability]` extras в `pyproject.toml`.

---

## 3. JsonlLogger — structured logs

`JsonlLogger` пишет per-line JSON в `<observability_log_dir>/harness-YYYY-MM-DD.jsonl` (ротация по дням).

### 3.1. LogEvent schema

```python
@dataclass(frozen=True)
class LogEvent:
    event: str                # "llm_call", "tool_call", "hook_dispatch", ...
    payload: dict[str, Any]   # Event-specific data
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    session_id: str = ""      # Current session UUID
    agent_id: str = ""        # Current agent id
    request_id: str = ""      # Short unique id (cross-trace)
    trace_id: str = ""        # 32-char hex (W3C); "" if no active span
    span_id: str = ""         # 16-char hex; "" if no active span
    latency_ms: float | None = None
    status: Literal["ok", "error", "timeout", "cancelled"] = "ok"
    error: str | None = None
    ts: float                  # Unix epoch (auto)
```

### 3.2. Формат строки (NDJSON)

```json
{"event": "llm_call", "payload": {"model": "gpt-4o", "tokens": 1234, "cost_usd": 0.005}, "level": "INFO", "session_id": "abc-123", "agent_id": "main", "request_id": "f4e8a1b2", "trace_id": "a1b2c3...", "span_id": "d4e5f6...", "latency_ms": 245.3, "status": "ok", "ts": 1718543210.789}
```

### 3.3. Гарантии

- **Thread-safe** (single `threading.Lock` на file handle).
- **Crash-safe** (open/write/close per line — no half-line state в kernel buffer).
- **Fail-open** (write failure → stdlib logger.warning, не raise).
- **Unicode-safe** (`ensure_ascii=False`).
- **Daily rotation** (file suffix `-YYYY-MM-DD.jsonl`).
- **Auto-cleanup** (`observability_log_max_files=30` → старые файлы удаляются при `cleanup()`).

### 3.4. Чтение tail

```python
from harness.observability import JsonlLogger
logger = JsonlLogger(Path("data/logs"))
recent = logger.tail(n=20)  # last 20 lines, parsed as dicts
```

---

## 4. PrometheusMetrics — `/metrics` endpoint

### 4.1. Endpoints

| Path | Format | Описание |
|------|--------|----------|
| `GET /metrics` | `text/plain; version=0.0.4` | Prometheus scrape format |

Активация: `observability_prometheus_enabled=True` + `pip install prometheus-client`.

### 4.2. Метрики (28 total)

**HTTP (2):**

| Name | Type | Labels | Описание |
|------|------|--------|----------|
| `harness_http_requests_total` | Counter | route, method, status | Total HTTP requests |
| `harness_http_request_duration_seconds` | Histogram | route, method | Request latency |

**LLM (5, +3 in Phase 4.9):**

| Name | Type | Labels | Описание |
|------|------|--------|----------|
| `harness_llm_calls_total` | Counter | model, tier, status | Total LLM completions |
| `harness_llm_latency_seconds` | Histogram | model, tier | LLM latency |
| `harness_llm_cost_total_usd` | Counter | model, tier | Cumulative USD cost (aggregate) |
| `harness_llm_cost_total_usd_by_model` | Counter | model_id | Per-LLM-model cost breakdown (Phase 4.9) |
| `harness_llm_tokens_total` | Counter | model_id, type=input\|output | Per-LLM-model token breakdown (Phase 4.9) |

**Hooks (4, +2 in Phase 4.8):**

| Name | Type | Labels | Описание |
|------|------|--------|----------|
| `harness_hook_dispatches_total` | Counter | event, decision | Total hook dispatches |
| `harness_hook_duration_seconds` | Histogram | event | Hook latency |
| `harness_hook_rate_limited_total` | Counter | hook_id | Rate-limited skips (Phase 4.8) |
| `harness_hook_circuit_skip_total` | Counter | hook_id, state | Circuit breaker skips (Phase 4.8) |

**Tools (3, +1 in Phase 4.9):**

| Name | Type | Labels | Описание |
|------|------|--------|----------|
| `harness_tool_calls_total` | Counter | tool_name, status | Total tool calls |
| `harness_tool_duration_seconds` | Histogram | tool_name | Tool latency (aggregate) |
| `harness_tool_duration_seconds_by_tool` | Histogram | tool_name | Per-tool latency (12 buckets, Phase 4.9) |

**Compaction (2):**

| Name | Type | Labels | Описание |
|------|------|--------|----------|
| `harness_compaction_total` | Counter | mode, cache_hit | Total compactions |
| `harness_compaction_duration_seconds` | Histogram | mode | Compaction latency |

**Queue (2):**

| Name | Type | Labels | Описание |
|------|------|--------|----------|
| `harness_merge_queue_events_total` | Counter | kind, status | Total queue events |
| `harness_queue_depth` | Gauge | — | Current queue depth |

**Outbound / Privacy / Webhook / Notify / Elicitation (5, +3 in Phase 4.3–4.8):**

| Name | Type | Labels |
|------|------|--------|
| `harness_outbound_deliveries_total` | Counter | kind, status_code |
| `harness_privacy_zone_total` | Counter | action |
| `harness_webhook_inbound_total` | Counter | event_type, status |
| `harness_notify_dlq_total` | Counter | severity, channel, terminal (Phase 4.8) |
| `harness_elicitation_total` | Counter | decision (Phase 4.3) |
| `harness_notification_total` | Counter | severity, channel (Phase 4.3) |

**Sessions (2):**

| Name | Type | Labels |
|------|------|--------|
| `harness_active_sessions` | Gauge | — |
| `harness_last_compact_age_seconds` | Gauge | — |

### 4.3. Cardinality safeguard (B4)

**НИКОГДА** не используйте `session_id`, `agent_id`, `request_id` как Prometheus label — это приведёт к cardinality explosion (10k+ active sessions → 10k+ time series). Используйте только high-cardinality-bounded labels: `route`, `method`, `status`, `model`, `tier`, `event`, `decision`, `tool_name`, `action`, `kind`.

### 4.4. Render

```python
from harness.observability import PrometheusMetrics
m = PrometheusMetrics(namespace="harness")
text = m.render().decode("utf-8")  # bytes
content_type = m.content_type       # "text/plain; version=0.0.4; charset=utf-8"
```

---

## 5. OTelTracer — distributed tracing

### 5.1. Span hierarchy

```
harness.request.GET_/api/chat/stream     (HTTP middleware)
  └── harness.agent_loop.run              (AgentLoop.run)
        ├── harness.llm_call              (per LLM call)
        ├── harness.tool_call.read_file   (per tool)
        │     └── harness.hook.PreToolUse (per hook dispatch)
        ├── harness.llm_call
        └── harness.compaction.run        (if triggered)
```

### 5.2. W3C trace context

Каждый span имеет `trace_id` (32 hex) + `span_id` (16 hex). Передаются через `traceparent` HTTP header (RFC `trace-context`).

```python
from harness.observability import OTelTracer
tracer = OTelTracer(name="harness")
with tracer.start_span("llm_call", model="gpt-4o", tier="T3") as span:
    span.set_attribute("latency_ms", 245.3)
    # ... do work ...
```

Получение текущего trace_id (для логирования):

```python
trace_id = tracer.get_current_trace_id()  # 32 hex chars or ""
span_id = tracer.get_current_span_id()    # 16 hex chars or ""
```

### 5.3. OTLP export

Для отправки спанов в collector (Jaeger/Tempo/Honeycomb) активируйте:

```python
# settings.py
observability_otlp_enabled = True
observability_otlp_endpoint = "http://localhost:4317"
observability_trace_sample_ratio = 0.1  # 10% sampling
```

+ `pip install opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp`.

### 5.4. No-op fallback

Если OTel SDK не установлен, `tracer.start_span()` yields `NoOpSpan` (все методы — no-op). Zero overhead.

---

## 6. HealthChecker — live/ready/deep + 8 probes (Phase 4.9)

### 6.1. Три эндпоинта

| Endpoint | Status code | Что проверяет |
|----------|-------------|---------------|
| `GET /health/live` | always 200 | Процесс жив |
| `GET /health/ready` | 200 / 503 | Critical deps (Qdrant/SQLite/Neo4j) reachable |
| `GET /health/deep` | 200 / 200 (degraded) | Все probes + диагностика (8 subsystem probes, Phase 4.9) |

**Backward compat:** `GET /api/health` (Phase 0) → alias для `/health/deep?minimal=true`.

### 6.2. Deep probes (Phase 4.9)

`HealthChecker` расширен 9 optional kwargs: `db_path`, `qdrant_url`, `opensearch_url`, `job_store`, `merge_queue`, `elicitation_broker`, `notify_channels`, `rate_limiter`, `circuit_breaker` (reserved, no probe yet).

8 probe methods:
1. DB (SQLite)
2. Qdrant
3. OpenSearch
4. JobStore
5. MergeQueue
6. ElicitationBroker
7. NotifyChannels
8. RateLimiter

CircuitBreaker probe зарезервирован без реализации (forward-compat kwarg принимается).

**Execution:** `asyncio.gather` всех probes в parallel + `asyncio.wait_for(2.0)` per-probe timeout.
**Status:** `"ok"` (all pass) | `"degraded"` (non-critical fail) | `"down"` (critical fail).
**Output:** `ProbeResult` dataclass + `ProbeStatus` enum (exported from `harness/observability/__init__.py`).

### 6.3. Probe registration (DI, Phase 4.1 Plan B1)

```python
from harness.observability import HealthChecker

async def qdrant_probe() -> tuple[dict, bool]:
    try:
        client = QdrantClient(...)
        client.get_collections()  # raises on failure
        return ({"status": "ok", "collections": 5}, True)
    except Exception as e:
        return ({"status": "error", "error": str(e)}, False)

hc = HealthChecker(version="1.22.0", project_root="/srv/harness")
hc.configure(
    ready_timeout_s=2.0,    # B7: per-probe timeout
    deep_timeout_s=5.0,
    require_qdrant=True,    # B7: if True, qdrant failure → 503
)
hc.register_probe("qdrant", qdrant_probe)
hc.register_probe("sqlite", sqlite_probe)
```

**Plan B1:** Probes DI'ятся через `register_probe(name, probe)`. Модуль НЕ импортирует Qdrant/Neo4j/SQLite напрямую (trust boundary preserved).

### 6.4. Aggregation logic

| Condition | Status | HTTP code |
|-----------|--------|-----------|
| Все probes pass | `ok` | 200 |
| Non-required probe fails | `degraded` | 200 |
| Required probe fails (`require_qdrant=True`, qdrant fails) | `unhealthy` | 503 |
| Probe timeout (>`ready_timeout_s`) | `timeout` | 200 (degraded) |

---

## 7. CostTracker — per-task cost (per-model breakdown, Phase 4.9)

### 7.1. Cost table (12 моделей)

`DEFAULT_COSTS` — hardcoded словарь в `harness/observability/cost.py`:

```python
{
    "claude-3-5-sonnet": (0.003, 0.015),     # input/output per 1k tokens
    "claude-3-opus":     (0.015, 0.075),
    "claude-3-haiku":    (0.00025, 0.00125),
    "gpt-4o":            (0.0025, 0.01),
    "gpt-4o-mini":       (0.00015, 0.0006),
    "gpt-4-turbo":       (0.01, 0.03),
    "MiniMax-M2.7":       (0.001, 0.002),
    "MiniMax-M3":         (0.002, 0.004),
    "glm-4.5":           (0.0007, 0.0007),
    "glm-4.7":           (0.001, 0.002),
    "moonshot-v1-128k":  (0.001, 0.002),
    "kimi-k2.6":         (0.001, 0.002),
}
```

**R1 mitigation:** Prices as of 2026-06-16. Override через `observability_cost_overrides` (JSON) или дополняйте `DEFAULT_COSTS` при drift.

### 7.2. Per-model breakdown (Phase 4.9)

Помимо aggregate counter (`harness_llm_cost_total_usd{model, tier}`), Phase 4.9 v1.19.0 добавил:

- `harness_llm_cost_total_usd_by_model{model_id}` — per-model Counter для breakdown по конкретным провайдерам.
- `harness_llm_tokens_total{model_id, type=input|output}` — token count breakdown.

Wire points в `LLMRouter` (2 call sites: error + success paths). Backward compat: extended kwargs `model_id: str | None = None` в `emit_llm_call`.

### 7.3. Формула

```
cost_usd = (prompt_tokens * input_cost + completion_tokens * output_cost) / 1000
```

### 7.4. API

```python
from harness.observability import CostTracker

ct = CostTracker()
cost1 = ct.record_call("gpt-4o", prompt_tokens=1000, completion_tokens=500)
# → 0.0075 USD
ct.record_call("claude-3-5-sonnet", prompt_tokens=2000, completion_tokens=1000)
# → 0.021 USD

print(ct.total())          # 0.0285
print(ct.calls())          # 2
print(ct.by_model())       # {"gpt-4o": {...}, "claude-3-5-sonnet": {...}}
print(ct.to_dict())        # JSON-serialisable breakdown
```

### 7.5. Override

```python
# Settings: observability_cost_overrides = '{"gpt-4o": [3.00, 12.00]}'
from harness.observability.cost import parse_cost_overrides
overrides = parse_cost_overrides('{"gpt-4o": [3.00, 12.00]}')
# → {"gpt-4o": (3.0, 12.0)}

# Then use as cost table in record_call.
ct.record_call("gpt-4o", 1000, 500, costs={**DEFAULT_COSTS, **overrides})
```

---

## 8. Конфигурация

Все в `harness/config.py`, секция "Phase 4.1: Observability". **Master switch:** `observability_enabled: bool = True` — False = вся framework отключена.

### Master switches (4)
`observability_enabled=True`, `observability_jsonl_enabled=True`, `observability_prometheus_enabled=False` (opt-in), `observability_otlp_enabled=False` (opt-in).

### JSONL logger (3)
`observability_log_dir=<project_root>/data/logs`, `observability_log_max_files=30`, `observability_log_max_file_size_mb=100`.

### Prometheus (2)
`observability_metrics_path="/metrics"`, `observability_metrics_namespace="harness"`.

### OpenTelemetry (3)
`observability_otlp_endpoint=""`, `observability_otlp_headers=""`, `observability_trace_sample_ratio=1.0`.

### Deep health (4)
`observability_health_ready_timeout_s=2.0`, `observability_health_deep_timeout_s=5.0`, `observability_health_require_qdrant=False`, `observability_health_require_neo4j=False`.

### Cost tracking (2)
`observability_cost_enabled=True`, `observability_cost_overrides=""`.

### Admin endpoints (Phase 4.11, 3)
`hooks_observability_admin_enabled=True`, `hooks_observability_admin_audit_max_limit=500`, `hooks_observability_admin_metrics_filter=""`.

### Per-event enable (8)
`observability_log_http_requests=True`, `observability_log_llm_calls`, `observability_log_tool_calls`, `observability_log_hook_dispatches`, `observability_log_compactions`, `observability_log_merge_queue_events`, `observability_log_outbound_deliveries`, `observability_log_privacy_decisions`.

**Total: ~29 settings** (26 framework + 3 admin).

---

## 9. Admin endpoints (Phase 4.11)

**Phase 4.11 v1.21.0** добавила 3 admin JSON endpoints для operator dashboards (Grafana JSON panels, custom alerting). Все под `/api/v1/observability/*`, RBAC-gated через `Scope.OBSERVABILITY_READ`.

### 9.1. Endpoints

| Endpoint | Scope | Что возвращает |
|----------|-------|----------------|
| `GET /api/v1/observability/metrics` | `observability.read` | JSON snapshot всех Prometheus counters + gauges (опц. `?filter=<regex>` на metric names). Histograms excluded (use Prometheus text `/metrics`). |
| `GET /api/v1/observability/health/deep` | `observability.read` | JSON deep health report (8 subsystem probes from Phase 4.9): `{status, version, project_root, checks, probes, ts}`. |
| `GET /api/v1/observability/audit/recent?limit=N` | `observability.read` | Последние N `HookAuditSink` entries (default 50, max `hooks_observability_admin_audit_max_limit=500`). |

### 9.2. DLQ endpoints (Phase 4.13B v1.23.0)

Webhook DLQ — отдельный namespace под observability admin (read-only listing) + webhooks admin (mutation):

| Endpoint | Scope | Что делает |
|----------|-------|------------|
| `GET /api/v1/observability/webhooks/dlq?limit=N&include_replayed=bool` | `observability.read` | Список DLQ entries (default 100, max 1000). |
| `POST /api/v1/observability/webhooks/dlq/{dlq_id}/replay` | `webhooks.admin` | Re-send DLQ entry с CURRENT signing secret. |

### 9.3. PII safety

`_strip_pii()` regex на known PII fields перед JSON serialization: `question_preview`, `arguments_preview`, `prompt_preview`, `answer`, `raw_payload`. Operator dashboards видят aggregates, НЕ user-specific data.

### 9.4. Settings

- `hooks_observability_admin_enabled=True` (default) — монтирует router. False → 404 (не 403).
- `hooks_observability_admin_audit_max_limit=500` — cap на `limit` query param.
- `hooks_observability_admin_metrics_filter=""` — server-wide regex filter (overridable per-request).

### 9.5. RBAC и trust boundary

- All 3 endpoints требуют `Scope.OBSERVABILITY_READ` (Phase 4.11). DLQ replay требует `Scope.WEBHOOK_ADMIN` (Phase 4.13B).
- В open dev mode (`settings.auth_required=False`) scope check bypassed.
- AST-enforced: `observability_admin.py` импортирует только stdlib + FastAPI + `harness.config` + `harness.observability`. NO `harness.agents` imports.

---

## 10. Примеры

### 10.1. Минимальный (logs only)

```python
from harness.observability import JsonlLogger, LogEvent
from pathlib import Path

logger = JsonlLogger(Path("data/logs"))
logger.emit(LogEvent(
    event="llm_call",
    payload={"model": "gpt-4o", "tokens": 1234, "cost_usd": 0.005},
    session_id="abc-123",
    agent_id="main",
    latency_ms=245.3,
))
```

### 10.2. Metrics (требует `prometheus-client`)

```python
from harness.observability import PrometheusMetrics
m = PrometheusMetrics(namespace="harness")
m.llm_calls_total.labels(model="gpt-4o", tier="T3", status="ok").inc()
m.llm_latency_seconds.labels(model="gpt-4o", tier="T3").observe(1.456)
m.llm_cost_total_usd.labels(model="gpt-4o", tier="T3").inc(0.005)

# Render for /metrics endpoint.
output = m.render()  # bytes
```

### 10.3. Tracer (требует `opentelemetry-api`)

```python
from harness.observability import OTelTracer
tracer = OTelTracer(name="harness")
with tracer.start_span("llm_call", model="gpt-4o") as span:
    span.set_attribute("latency_ms", 245.3)
    # ... do LLM call ...
    # When span exits, OTel SDK records it.
```

### 10.4. HealthChecker с 2 probes

```python
from harness.observability import HealthChecker

async def sqlite_probe() -> tuple[dict, bool]:
    try:
        async with aiosqlite.connect("harness.db") as db:
            await db.execute("SELECT 1")
        return ({"status": "ok"}, True)
    except Exception as e:
        return ({"status": "error", "error": str(e)}, False)

async def qdrant_probe() -> tuple[dict, bool]:
    try:
        client = QdrantClient(...)
        client.get_collections()
        return ({"status": "ok", "collections": 5}, True)
    except Exception:
        return ({"status": "error"}, False)

hc = HealthChecker(version="1.7.0")
hc.configure(ready_timeout_s=2.0, require_qdrant=True)
hc.register_probe("sqlite", sqlite_probe)
hc.register_probe("qdrant", qdrant_probe)

report = await hc.readiness()
# report.status: "ok" | "degraded" | "unhealthy"
# report.to_dict() for JSON response.
```

### 10.5. CostTracker

```python
from harness.observability import CostTracker, compute_cost, DEFAULT_COSTS

# Single call.
cost = compute_cost("gpt-4o", prompt_tokens=1000, completion_tokens=500)
# → 0.0075 USD

# Aggregator.
ct = CostTracker()
for model, p_tok, c_tok in [("gpt-4o", 1000, 500), ("claude-3-5-sonnet", 2000, 1000)]:
    ct.record_call(model, p_tok, c_tok)
print(f"Total: ${ct.total():.4f} across {ct.calls()} calls")
```

---

## 11. Troubleshooting

### 11.1. `/metrics` returns 404

`observability_prometheus_enabled=True` AND `prometheus-client` installed. Проверьте:

```bash
pip show prometheus-client
```

Если не установлен: `pip install prometheus-client` (или `pip install -e ".[observability]"`).

### 11.2. `tracer.start_span()` yields `NoOpSpan`

`opentelemetry-api` не установлен. Установите:

```bash
pip install opentelemetry-api opentelemetry-sdk
```

### 11.3. JSONL log file растёт бесконечно

- Включите `observability_log_max_files=30` + периодический вызов `logger.cleanup(max_files=30)`.
- Или используйте внешний logrotate (`/etc/logrotate.d/harness`).

### 11.4. `/health/ready` возвращает 503 хотя всё работает

Один из required probes (`require_qdrant=True`, `require_neo4j=True`) не зарегистрирован или возвращает `ok=False`. Проверьте probes через `await hc.readiness()` и смотрите `report.checks`.

### 11.5. Cardinality explosion в Prometheus

**Не используйте** `session_id`, `agent_id`, `request_id` как label — только high-cardinality-bounded: `route`, `method`, `status`, `model`, `tier`, `event`, `decision`, `tool_name`, `action`, `kind`. Plan B4.

### 11.6. Trust boundary violation

`tests/test_observability_trust_boundary.py` (3 проверки) валит CI если `harness/observability/*` начнёт импортить `harness.agents`, `harness.server`, или `harness.hooks`. Это **by design** — observability framework не должен зависеть от production кода.

### 11.7. Cost не считается (всегда 0.0)

- `observability_cost_enabled=True` (default).
- Model name есть в `DEFAULT_COSTS` или `observability_cost_overrides`. Unknown model = 0.0 (R1 mitigation).

### 11.8. OTLP export не работает

- `observability_otlp_enabled=True`.
- `observability_otlp_endpoint` указывает на collector (e.g. `http://localhost:4317`).
- `pip install opentelemetry-exporter-otlp` (отдельно от API).

---

## 12. См. также

- [`docs/PHASE4-OBSERVABILITY-PLAN.md`](PHASE4-OBSERVABILITY-PLAN.md) — maintainer reference (Phase 4.1 plan)
- [`docs/CHANGELOG.md`](CHANGELOG.md) — v1.7.0 → v1.22.0 history
- [`docs/hooks.md`](hooks.md) — Phase 4.0+ hooks framework (16 events, 12 builtins)
- [`docs/api.md`](api.md) — `/api/v1/observability/*` admin endpoints reference
- [`docs/scope-api.md`](scope-api.md) — `observability.read` scope
- [`docs/cli.md`](cli.md) — `harness observability <log|metrics|health|stats>` CLI
- [`docs/roadmap.md`](roadmap.md) — Phase 4 статус
- `harness/observability/` — исходный код (5 модулей, ~1100 LoC)
- `tests/test_observability_*.py` + `tests/test_*_by_*.py` + `tests/test_health_deep_probes.py` + `tests/test_observability_admin.py` — 100+ tests
- [Prometheus naming best practices](https://prometheus.io/docs/practices/naming/)
- [OpenTelemetry Python docs](https://opentelemetry.io/docs/languages/python/)

---

**Версия документа:** v1.22.0 (2026-06-19)
**Phase:** 4.1–4.12 — Observability (framework + wiring + per-tool/per-model metrics + deep probes + admin endpoints)
