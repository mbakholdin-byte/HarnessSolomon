# Solomon Harness — Phase 4.1 Observability Plan

**Version:** v1.7.0 (Phase 4.1)
**Author:** Plan-Research sub-agent (Соломон)
**Date:** 2026-06-16
**Status:** DRAFT — pending Mark approval

---

## § 0. Контекст и решения Марка (2026-06-16)

### Что делаем
Phase 4.1 реализует production-observability для Solomon Harness: structured JSONL логи, OpenTelemetry-compatible traces, Prometheus `/metrics` endpoint, deep health checks (liveness/readiness/deep), per-task cost tracking. Цель — дать Марку visibility в production-harness (16 событий хук-фреймворка Phase 4.0 + LLM calls + tool calls + queue depth), не ломая backward compat с Phase 0 (`GET /api/health` → `{status, version, project_root}` остаётся).

### Scope (Phase 4.1)
- 5 модулей: `harness/observability/{tracer,metrics,health,logger,exporter}.py`
- Structured JSONL logging: per-event JSON в `data/logs/harness-YYYY-MM-DD.jsonl`, rotation по дням, threading.Lock (mirror hooks/audit.py pattern)
- OpenTelemetry traces: spans для HTTP request / agent loop / LLM call / hook dispatch / tool call / compaction. W3C `traceparent` header. Provider = `NoOpTracerProvider` если OTel SDK не установлен (zero-deps по умолчанию).
- Prometheus `/metrics` endpoint: counters (requests_total, llm_calls_total per tier, hook_dispatches_total, compaction_total), histograms (request_duration_seconds, llm_latency_seconds, hook_duration_seconds, tool_duration_seconds), gauges (active_sessions, queue_depth, last_compact_age_seconds). Text format (`prometheus_client.generate_latest`). Opt-in via `settings.observability_prometheus_enabled`.
- Deep health: `GET /health/live` (всегда 200), `GET /health/ready` (Qdrant + SQLite + Neo4j reachable), `GET /health/deep` (full diagnostics: memory, queue, hook registry, last compact age). Backward compat: `GET /api/health` перенаправляет на deep handler.
- Per-task cost: token counts × provider cost (settings table). Surface в метриках (`llm_cost_total_usd`) + structured logs (`cost_usd` field).
- Trust boundary: `harness/observability/` НЕ импортирует `harness.agents` или `harness.server`. Mirror of `harness/hooks/` boundary. Static test `test_observability_trust_boundary.py`.
- Settings: 20-30 новых полей в `harness/config.py:ObservabilitySettings` (опциональная подсекция, но в этом проекте = плоский `Settings`).
- Tag: v1.7.0.

### Явные не-цели (Phase 4.1)
- **Hot-reload** — Phase 4.2 (carryover from Phase 4.0)
- **`/api/* → /api/v1/*` migration** — Phase 4.3 (carryover)
- **Elicitation / Notification / PermissionRequest observability events** — Phase 4.4
- **Hooks CLI (`harness observability ...`)** — Phase 4.5
- **Tracing UI / Grafana / Tempo setup** — внешняя инфраструктура, не код. Документируем integration points в `docs/observability.md`, но НЕ разворачиваем.
- **Log shipping (Fluent Bit / Vector / Promtail sidecar)** — внешний, документируем только OTLP endpoint setting.
- **APM / continuous profiling (Pyroscope)** — Phase 4.6+
- **Distributed tracing в MergeQueue jobs** — Phase 4.1 = in-process spans only; cross-PR trace correlation (через GitHub commit SHA → trace_id) = Phase 4.7+

### Архитектурные решения Марка
- **Trust boundary strict:** `harness/observability/` не импортирует `harness.agents` или `harness.server`. Static test enforces (mirror `tests/test_hooks_trust_boundary.py`).
- **Optional deps через `[observability]` extras:** `prometheus-client>=0.20`, `opentelemetry-api>=1.24`, `opentelemetry-sdk>=1.24`, `opentelemetry-exporter-otlp>=1.24`. **0 new required deps.** Если extras не установлены — `PrometheusMetrics` = no-op, `OTelTracer` = `NoOpTracerProvider`. JSONL logger и deep health работают в любом случае (stdlib only).
- **JSONL logger — sync write с threading.Lock.** Mirror `harness/hooks/audit.py:HookAuditSink`. НЕ asyncio.Queue + background drainer — на crash теряем логи (R5). Sync write дешевле (1-2ms на hot path) и survives SIGKILL.
- **Health probes — bounded timeout.** `GET /health/ready` для Qdrant/Neo4j — `asyncio.wait_for(probe, timeout=2.0)`. Меньше timeout = DOS на database (R3).
- **Prometheus — opt-in, default OFF.** Если `observability_prometheus_enabled=False`, `/metrics` endpoint не зарегистрирован, нет накладных расходов.
- **Plan review обязателен.** 5+ BLOCKERS, 5+ RISKS, 5+ CONCERNS зафиксированы в § 12 до coding.

---

## § 1. Цели и не-цели

### Цели Phase 4.1
1. **Structured JSONL logging** — все hot-path логи (`logger.info("...")` в `runner.py`, `router.py`, `merge_queue.py`, `outbound.py`, `compact.py`, `app.py`) переводятся на `observability.logger.emit(event, payload)` API. Per-line JSON: `{"ts", "level", "event", "session_id", "agent_id", "request_id", "trace_id", "span_id", "payload", "latency_ms", "status"}`.
2. **OTel-compatible traces** — span tree: HTTP request → agent loop → LLM call → tool call / hook dispatch / compaction. W3C context propagation через ASGI middleware.
3. **Prometheus `/metrics`** — opt-in endpoint с 12+ метриками. 4 типа: Counter, Histogram, Gauge, Summary. Standard naming (`<namespace>_<subsystem>_<name>_<unit>`).
4. **Deep health (3 endpoints)** — `/health/live`, `/health/ready`, `/health/deep`. Backward compat: `/api/health` = alias для `/health/deep?minimal=true` (только status + version + project_root).
5. **Per-task cost tracking** — cost считается в `LLMRouter.completion()` (если usage приходит), поверх существующего `RunnerResult.total_cost` поля. Стоимость в USD, hardcoded provider cost table (опционально override через env).
6. **Trust boundary enforced** — `harness/observability/*` import-isolation verified by `tests/test_observability_trust_boundary.py`.
7. **Backward compat** — `GET /api/health` возвращает тот же dict, что в Phase 0. `Logger.info("...")` calls не удаляются, оборачиваются (no breaking change).
8. **Tag v1.7.0** на master с зелёным mock suite (>= 1500 tests, target 1700+).

### Не-цели (Phase 4.1)
- Полный hot-reload (deferred to Phase 4.2)
- `/api/* → /api/v1/*` migration (deferred to Phase 4.3)
- Elicitation / Notification observability events (deferred to Phase 4.4)
- `harness observability` CLI subcommand (deferred to Phase 4.5)
- Tracing UI / Grafana / Tempo provisioning (внешняя инфраструктура)
- Log shipping sidecar configs (документируем, не разворачиваем)
- Pyroscope-style continuous profiling (Phase 4.6+)
- Cross-PR distributed traces (Phase 4.7+)
- Custom metric labels (Phase 4.1 = фиксированный набор labels)

---

## § 2. Архитектура

### § 2.1. Модульная структура

```
harness/observability/                            # NEW: trust-boundary isolated
├── __init__.py                                    # Public API exports (lazy)
├── tracer.py                                      # OTel tracer (NoOp fallback)
├── metrics.py                                     # Prometheus registry (no-op fallback)
├── health.py                                      # DeepHealthChecker (Qdrant/SQLite/Neo4j probes)
├── logger.py                                      # JsonlLogger (stdlib + threading.Lock)
├── cost.py                                        # ProviderCostTable (hardcoded + env override)
└── exporter.py                                    # OTLP exporter setup (optional)
```

**Ключевое отличие от hooks:** `harness/observability/` — это **library-стиль API**, а не framework-стиль (как hooks). То есть: call sites импортируют `from harness.observability import get_logger, get_tracer, get_metrics` и вызывают методы напрямую. Нет реестра, нет dispatcher, нет event flow. Это упрощает trust boundary (stateless) и снижает performance overhead.

### § 2.2. Диаграмма (ASCII)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       TRIGGER POINTS (production code)                       │
├─────────────────────────────────────────────────────────────────────────────┤
│  FastAPI middleware ──► request_id, trace_id (W3C traceparent)              │
│  harness/agents/runner.py:_drive() ──► log+span+metric per agent run        │
│  harness/agents/router.py:classify() ──► log+span+metric per cascade        │
│  harness/hooks/runner.py:fire() ──► log+span+metric per hook dispatch       │
│  harness/agents/merge_queue.py:_emit() ──► log+metric per job event         │
│  harness/agents/outbound.py:_deliver_one() ──► log+metric per webhook       │
│  harness/server/agent/loop.py:AgentLoop.run() ──► span per iteration        │
│  harness/server/agent/runtime.py:ToolRuntime.execute() ──► span per tool    │
│  harness/context/compaction.py:ContextCompactor.maybe_compact() ──► span    │
│  harness/privacy/zone_filter.py:PrivacyZoneFilter.check() ──► log+metric    │
│  harness/server/llm/router.py:completion() ──► log+span+metric per LLM      │
└─────────────────────────────────────────────────────────────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       OBSERVABILITY LIBRARY (harness/observability/)         │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐    │
│  │  JsonlLogger │  │ OTel Tracer  │  │   Metrics    │  │ DeepHealth   │    │
│  │  (sync NDJSON│  │ (NoOp/OTel)  │  │  (NoOp/Prom) │  │  (Qdrant/    │    │
│  │   + Lock)    │  │              │  │              │  │  SQLite/Neo4j│    │
│  │              │  │              │  │              │  │  /queue/...) │    │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘    │
│         │                 │                  │                 │             │
│         │ threading.Lock  │ in-memory +      │ Counter/        │ asyncio.   │
│         │ file rotation   │ OTLP exporter    │ Histogram/      │ wait_for   │
│         │ at midnight     │ (opt-in)         │ Gauge registry  │ (timeout)  │
│         ▼                 ▼                  ▼                 ▼             │
│  ┌──────────────────────────────────────────────────────────────────┐       │
│  │  data/logs/harness-YYYY-MM-DD.jsonl (rotated)                    │       │
│  │  OTLP HTTP/gRPC collector (opt-in)                                │       │
│  │  GET /metrics (text format) — opt-in                              │       │
│  │  GET /health/{live,ready,deep}                                    │       │
│  └──────────────────────────────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       EXTERNAL (operator-configured)                         │
├─────────────────────────────────────────────────────────────────────────────┤
│  Grafana / Tempo / Loki (log/trace/metric dashboards)                        │
│  Prometheus scrape target (text format /metrics)                            │
│  Load balancer (probe /health/live + /health/ready)                         │
└─────────────────────────────────────────────────────────────────────────────┘
```

### § 2.3. Core Types

#### `harness/observability/logger.py`
```python
from __future__ import annotations
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

@dataclass(frozen=True)
class LogEvent:
    """One structured log line.

    All fields except ``event`` and ``payload`` are optional — the
    logger fills in ``ts``, ``trace_id``, ``span_id`` from the
    active OTel context (if any).
    """
    event: str                                  # e.g. "agent_run", "llm_call", "hook_dispatch"
    payload: dict[str, Any]                     # Event-specific data
    level: str = "INFO"                         # DEBUG/INFO/WARNING/ERROR/CRITICAL
    session_id: str = ""
    agent_id: str = ""
    request_id: str = ""
    trace_id: str = ""                          # 32-char hex (W3C); "" if no active span
    span_id: str = ""                           # 16-char hex; "" if no active span
    latency_ms: float | None = None
    status: str = "ok"                          # ok|error|timeout|cancelled
    error: str | None = None

class JsonlLogger:
    """Append-only JSONL writer with daily rotation.

    Mirror of :class:`harness.hooks.audit.HookAuditSink` — sync
    write with ``threading.Lock``. NO asyncio.Queue + background
    drainer (R5: на crash теряем логи).
    """
    def __init__(self, log_dir: Path, *, enabled: bool = True) -> None:
        self._log_dir = log_dir
        self._enabled = enabled
        self._lock = threading.Lock()

    def emit(self, event: LogEvent) -> None:
        """Best-effort write. NEVER raises."""
        if not self._enabled:
            return
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            line = json.dumps(
                {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    **asdict(event),
                },
                ensure_ascii=False,
            )
            path = self._log_dir / f"harness-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.jsonl"
            with self._lock:
                with path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception as e:
            stdlib_logger = logging.getLogger(__name__)
            stdlib_logger.warning("JsonlLogger.emit failed: %s: %s", type(e).__name__, e)

#: Module-level singleton (lazy-init from settings).
_logger: JsonlLogger | None = None
def get_logger() -> JsonlLogger:
    global _logger
    if _logger is None:
        from harness.config import settings
        _logger = JsonlLogger(
            log_dir=settings.observability_log_dir,
            enabled=settings.observability_jsonl_enabled,
        )
    return _logger
```

#### `harness/observability/tracer.py`
```python
from __future__ import annotations
import logging
from contextlib import contextmanager
from typing import Any, Iterator

# OTel imports guarded by try/except. If OTel SDK is not installed,
# we fall back to NoOpTracer (zero-overhead no-op).
try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.trace import Status, StatusCode
    _HAS_OTEL = True
except ImportError:
    _HAS_OTEL = False

@contextmanager
def start_span(name: str, **attrs: Any) -> Iterator[Any]:
    """Start an OTel span (or no-op context if OTel not installed).

    Usage::

        with start_span("llm_call", model=model) as span:
            response = await router.completion(...)
            span.set_attribute("tokens", response.usage.total_tokens)

    Trust boundary: NEVER imports harness.agents or harness.server.
    OTel context propagation via W3C traceparent header handled by
    FastAPI middleware (see § 5.3).
    """
    if not _HAS_OTEL:
        yield _NoOpSpan()
        return
    tracer = trace.get_tracer("harness")
    with tracer.start_as_current_span(name) as span:
        for k, v in attrs.items():
            span.set_attribute(k, v)
        yield span

class _NoOpSpan:
    """Stand-in when OTel SDK is not installed."""
    def set_attribute(self, k: str, v: Any) -> None: pass
    def record_exception(self, exc: BaseException) -> None: pass
    def set_status(self, status: Any) -> None: pass
```

#### `harness/observability/metrics.py`
```python
from __future__ import annotations
import logging
from typing import Any

# prometheus_client guarded by try/except. If not installed, all
# metrics calls are no-ops.
try:
    from prometheus_client import (
        Counter, Histogram, Gauge, Summary, CollectorRegistry, generate_latest,
    )
    _HAS_PROM = True
except ImportError:
    _HAS_PROM = False

class MetricsRegistry:
    """Lazy metric factory. All metrics created on first access.

    Trust boundary: NEVER imports harness.agents or harness.server.
    Mirror of hooks trust boundary. Returns no-op if prometheus_client
    is not installed AND settings.observability_prometheus_enabled=True
    (logs warning, returns dict-based shim).
    """
    def __init__(self, registry: Any = None) -> None:
        self._registry = registry or (CollectorRegistry() if _HAS_PROM else None)
        self._counters: dict[str, Any] = {}
        self._histograms: dict[str, Any] = {}
        self._gauges: dict[str, Any] = {}

    def counter(self, name: str, labels: list[str] | None = None) -> Any:
        if not _HAS_PROM or self._registry is None:
            return _NoOpMetric()
        if name not in self._counters:
            self._counters[name] = Counter(name, name, labelnames=labels or [], registry=self._registry)
        return self._counters[name]

    def histogram(self, name: str, labels: list[str] | None = None, buckets: tuple = ...) -> Any:
        if not _HAS_PROM or self._registry is None:
            return _NoOpMetric()
        if name not in self._histograms:
            self._histograms[name] = Histogram(name, name, labelnames=labels or [], buckets=buckets, registry=self._registry)
        return self._histograms[name]

    def gauge(self, name: str, labels: list[str] | None = None) -> Any:
        if not _HAS_PROM or self._registry is None:
            return _NoOpMetric()
        if name not in self._gauges:
            self._gauges[name] = Gauge(name, name, labelnames=labels or [], registry=self._registry)
        return self._gauges[name]

    def render(self) -> bytes:
        """Render Prometheus text format (used by /metrics route)."""
        if not _HAS_PROM or self._registry is None:
            return b"# prometheus_client not installed\n"
        return generate_latest(self._registry)

class _NoOpMetric:
    def inc(self, amount: float = 1.0, **labels: str) -> None: pass
    def dec(self, amount: float = 1.0, **labels: str) -> None: pass
    def set(self, value: float, **labels: str) -> None: pass
    def observe(self, value: float, **labels: str) -> None: pass
    def labels(self, **labels: str) -> "_NoOpMetric": return self

#: Module-level singleton (lazy-init from settings).
_metrics: MetricsRegistry | None = None
def get_metrics() -> MetricsRegistry:
    global _metrics
    if _metrics is None:
        from harness.config import settings
        _metrics = MetricsRegistry(enabled=settings.observability_prometheus_enabled)
    return _metrics
```

#### `harness/observability/health.py`
```python
from __future__ import annotations
import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

class DeepHealthChecker:
    """3-level health probe.

    Trust boundary: NEVER imports harness.agents or harness.server.
    Probes are duck-typed (call .ping() / .execute("SELECT 1") /
    .lock_for() etc) via app.state. This is the SAME pattern as
    Phase 1.6 TokenStore DI.
    """
    def __init__(self, *, ready_timeout_s: float = 2.0, deep_timeout_s: float = 5.0) -> None:
        self._ready_timeout = ready_timeout_s
        self._deep_timeout = deep_timeout_s

    async def live(self) -> dict[str, Any]:
        """Always 200 if process is up. Used by k8s liveness probe."""
        return {"status": "ok", "check": "liveness"}

    async def ready(self, *, app_state: Any) -> dict[str, Any]:
        """Readiness: Qdrant + SQLite + Neo4j reachable.

        Each probe is wrapped in ``asyncio.wait_for(timeout=2.0)``
        so a slow DB doesn't DOS the load balancer (R3).
        """
        checks: dict[str, str] = {}
        for name, probe in [
            ("sqlite", self._probe_sqlite),
            ("qdrant", self._probe_qdrant),
            ("neo4j", self._probe_neo4j),
        ]:
            try:
                ok = await asyncio.wait_for(probe(app_state), timeout=self._ready_timeout)
                checks[name] = "ok" if ok else "unreachable"
            except asyncio.TimeoutError:
                checks[name] = "timeout"
            except Exception as e:
                checks[name] = f"error: {type(e).__name__}"
        healthy = all(v == "ok" for v in checks.values())
        return {
            "status": "ok" if healthy else "degraded",
            "checks": checks,
        }

    async def deep(self, *, app_state: Any) -> dict[str, Any]:
        """Full diagnostics: ready checks + queue depth + hook registry + last compact.

        Returns 503 if any REQUIRED check fails; 200 with warnings otherwise.
        """
        ready = await self.ready(app_state=app_state)
        diagnostics: dict[str, Any] = {
            "ready": ready,
            "queue_depth": self._queue_depth(app_state),
            "hook_registry_size": self._hook_registry_size(app_state),
            "last_compact_age_s": self._last_compact_age(app_state),
            "active_sessions": self._active_sessions(app_state),
        }
        all_ok = ready["status"] == "ok"
        return {
            "status": "ok" if all_ok else "degraded",
            "version": self._version(),
            "project_root": self._project_root(),
            "diagnostics": diagnostics,
        }
```

#### `harness/observability/cost.py`
```python
from __future__ import annotations
from typing import Mapping

# Hardcoded cost table (USD per 1M tokens). Last updated 2026-06-16.
# Override via settings.observability_cost_overrides (JSON string).
DEFAULT_COSTS: Mapping[str, tuple[float, float]] = {
    # (input_cost_per_1m, output_cost_per_1m) in USD
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "claude-opus-4": (15.00, 75.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-haiku-4": (0.80, 4.00),
    "MiniMax-M2.7": (0.30, 1.20),
    "MiniMax-M3": (0.50, 2.00),
    "glm-4.7": (0.10, 0.40),
    "kimi-k2.6": (0.20, 0.80),
    "qwen3:8b": (0.0, 0.0),  # local
    "qwen3-coder:30b": (0.0, 0.0),  # local
}

def compute_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Return cost in USD for one LLM call. Best-effort: 0.0 if model unknown."""
    if model not in DEFAULT_COSTS:
        return 0.0
    in_cost, out_cost = DEFAULT_COSTS[model]
    return (prompt_tokens / 1_000_000.0) * in_cost + (completion_tokens / 1_000_000.0) * out_cost
```

#### `harness/observability/exporter.py`
```python
from __future__ import annotations
import logging
from typing import Any

# OTel OTLP exporter (optional). If not installed, no-op.
try:
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    _HAS_OTLP = True
except ImportError:
    _HAS_OTLP = False

def setup_otlp_exporter(endpoint: str, *, headers: dict[str, str] | None = None) -> bool:
    """Configure OTLP exporter. Returns True on success, False on no-op (missing dep).

    Idempotent: if already configured, returns True without re-configuring.
    """
    if not _HAS_OTLP:
        logging.getLogger(__name__).warning(
            "opentelemetry-exporter-otlp not installed; OTLP export disabled"
        )
        return False
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        # No SDK provider yet — caller forgot to call setup_tracer_provider.
        return False
    exporter = OTLPSpanExporter(endpoint=endpoint, headers=headers or {})
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return True
```

### § 2.4. Trust Boundary

**`harness/observability/*` НЕ импортирует `harness.agents` или `harness.server`.**

- Allowed: stdlib, `harness.config` (read-only Settings), `harness.redaction` (read-only utility, optional)
- Forbidden: `harness.agents.*`, `harness.server.*`, `harness.hooks.*` (однако в observability можно импортировать `harness.hooks.audit.HookAuditSink` КАК reference pattern — НЕЛЬЗЯ импортировать сами хуки; проверка запрещает полный путь `harness.hooks.*`)

**Зачем строго:** observability — нижний слой, dependency для всего. Если observability потянет за собой `harness.agents`, то агентский код не сможет импортировать observability (circular import). Mirror `harness/hooks/` boundary.

**Static test:** `tests/test_observability_trust_boundary.py` — mirror `tests/test_hooks_trust_boundary.py`. AST-парсинг каждого `.py` файла под `harness/observability/`, проверка `forbidden prefixes = ("harness.agents", "harness.server", "harness.hooks")`. Тест компилируется в mock-сете.

### § 2.5. Backward Compatibility

- `GET /api/health` (Phase 0) — **сохраняется**, теперь = alias для `GET /health/deep?minimal=true` (только `{status, version, project_root}`).
- `GET /health/live` — новый, для k8s liveness.
- `GET /health/ready` — новый, для k8s readiness.
- `GET /health/deep` — новый, для диагностики.
- `GET /metrics` — новый, opt-in (default OFF).
- `logger.info("...")` в production коде — **не удаляются**, оборачиваются через `from harness.observability.logger import get_logger; get_logger().emit(LogEvent(...))` (idempotent pattern: и JSONL строка, и stdlib logger). Stdlib logger остаётся default output, JSONL — mirror.

---

## § 3. Trigger Points / Instrumentation Sites

Минимум 15 trigger points. Колонка "Kind" — что именно инсрументируем (log / span / metric).

| # | Call site | File:line | Kind | What we emit |
|---|-----------|-----------|------|--------------|
| 1 | HTTP request entry | `server/app.py` (new middleware) | log+span+metric | `request_started{session_id, route, method}` |
| 2 | HTTP request exit | `server/app.py` (new middleware) | log+span+metric | `request_completed{status, latency_ms}` |
| 3 | Agent run | `agents/runner.py:AgentRunner._drive` | log+span+metric | `agent_run{agent, spec, iterations, cost_usd}` |
| 4 | LLM call | `server/llm/router.py:LLMRouter.completion` | log+span+metric | `llm_call{model, tier, prompt_tokens, completion_tokens, cost_usd, latency_ms}` |
| 5 | Cascade decision | `agents/cascade.py:TierSelector.select` | log+metric | `cascade_decision{tier, confidence, promoted_to}` |
| 6 | Routing decision | `agents/router.py:LLMRouterClassifier.classify` | log+metric | `routing_decision{agent, confidence, fallback}` |
| 7 | Hook dispatch | `hooks/runner.py:HookRunner.fire` | log+span+metric | `hook_dispatch{event, hook_id, decision, duration_ms}` |
| 8 | Hook timeout | `hooks/runner.py:_invoke_builtin` | log+metric | `hook_timeout{event, hook_id, timeout_ms}` |
| 9 | Tool call | `server/agent/runtime.py:ToolRuntime.execute` | log+span+metric | `tool_call{tool_name, ok, duration_ms, error_kind}` |
| 10 | Compaction | `context/compaction.py:ContextCompactor._run_slow_path` | log+span+metric | `compaction{mode, cache_hit, latency_ms, tokens_saved}` |
| 11 | Merge queue event | `agents/merge_queue.py:MergeQueue._emit` | log+metric | `merge_queue_event{kind, job_id, payload_size}` |
| 12 | Outbound webhook | `agents/outbound.py:OutboundWebhookDispatcher._deliver_one` | log+metric | `outbound_delivery{kind, url, status_code, attempts}` |
| 13 | Privacy zone decision | `privacy/zone_filter.py:PrivacyZoneFilter.check` | log+metric | `privacy_zone{action, matched, pattern, path}` |
| 14 | Webhook inbound | `agents/webhook_handler.py:WebhookHandler.handle` | log+metric | `webhook_inbound{event_type, delivery_id, ok}` |
| 15 | Session lifecycle | `server/app.py:lifespan` (start/end) | log+metric | `session_lifecycle{event, session_id}` |
| 16 | Cost calculation | `server/llm/router.py:LLMRouter.completion` (in completion result) | log+metric | `cost_accumulated{model, total_usd}` |
| 17 | Memory write | `memory/unified.py:UnifiedMemory.write` | log+metric | `memory_write{layer, kind, size}` |

**Total: 17 trigger points** (well above 15).

---

## § 4. Data Model

### 4.1. `LogEvent` dataclass
```python
@dataclass(frozen=True)
class LogEvent:
    event: str                          # "agent_run", "llm_call", etc.
    payload: dict[str, Any]             # Event-specific data
    level: str = "INFO"
    session_id: str = ""
    agent_id: str = ""
    request_id: str = ""
    trace_id: str = ""                  # 32-char hex (W3C); "" if no active span
    span_id: str = ""                   # 16-char hex; "" if no active span
    latency_ms: float | None = None
    status: str = "ok"                  # ok|error|timeout|cancelled
    error: str | None = None
```

### 4.2. `MetricSample` (logical, not a class — Prometheus types)
| Type | Prometheus class | Example metric | Labels | Notes |
|------|------------------|----------------|--------|-------|
| Counter | `Counter` | `harness_requests_total` | `route, method, status` | Monotonic; resets on process restart |
| Histogram | `Histogram` | `harness_request_duration_seconds` | `route, method` | Buckets: 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5 |
| Gauge | `Gauge` | `harness_active_sessions` | (none) | Set on inc/dec |
| Summary | `Summary` | `harness_llm_latency_seconds` | `model, tier` | Quantiles 0.5, 0.9, 0.99 |

**Naming convention:** `<namespace>_<subsystem>_<name>_<unit>`. Namespace = `harness`. Subsystem = `agent`, `llm`, `hook`, `compaction`, `queue`, `outbound`, `privacy`, `http`, `session`. Unit suffix: `_seconds`, `_bytes`, `_total`, `_usd`. Examples: `harness_llm_cost_total_usd`, `harness_hook_dispatches_total`, `harness_compaction_duration_seconds`.

### 4.3. `TraceSpan` (OTel native, not custom)
OTel SDK provides `trace.Span` with: `name`, `context.trace_id`, `context.span_id`, `parent.span_id`, `start_time`, `end_time`, `attributes`, `status`. We do NOT redefine. Our `start_span()` context manager is a thin wrapper.

**Span hierarchy example:**
```
harness.request.GET_/api/chat/stream        (HTTP middleware)
  └── harness.agent_loop.run                (AgentLoop.run)
        ├── harness.llm_call                (per LLM call)
        ├── harness.tool_call.read_file     (per tool)
        │     └── harness.hook.PreToolUse   (per hook dispatch)
        ├── harness.llm_call
        └── harness.compaction.run          (if triggered)
```

---

## § 5. Settings (20-30 new fields)

```python
# === Phase 4.1: Observability — master switches ===
observability_enabled: bool = Field(
    default=True,
    description="Phase 4.1: master switch. False → all observability is no-op. "
                "Mirrors hooks_enabled pattern (Phase 4.0).",
)
observability_jsonl_enabled: bool = Field(
    default=True,
    description="Phase 4.1: write structured JSONL logs to data/logs/. "
                "Default True (cheap, ~1ms per log line).",
)
observability_prometheus_enabled: bool = Field(
    default=False,
    description="Phase 4.1: enable /metrics endpoint. Default OFF (zero overhead). "
                "Set True for production deployments with Prometheus scrape.",
)
observability_otlp_enabled: bool = Field(
    default=False,
    description="Phase 4.1: export spans via OTLP. Default OFF (requires OTel SDK "
                "extras + collector endpoint).",
)

# === JSONL logger ===
observability_log_dir: Path = Field(
    default=PROJECT_ROOT / "data" / "logs",
    description="Phase 4.1: directory for harness-YYYY-MM-DD.jsonl files. "
                "Rotated daily at midnight (date suffix in filename).",
)
observability_log_max_files: int = Field(
    default=30, ge=1, le=365,
    description="Phase 4.1: max retained rotated log files. Older files are deleted "
                "by a background task (once per hour). 30 = ~1 month retention.",
)
observability_log_max_file_size_mb: int = Field(
    default=100, ge=1, le=1024,
    description="Phase 4.1: rotate file by size (in addition to daily rotation). "
                "If a single file exceeds this, rotate early. 0 = size-based disabled.",
)

# === Prometheus ===
observability_metrics_path: str = Field(
    default="/metrics",
    description="Phase 4.1: path for Prometheus scrape. Standard is /metrics.",
)
observability_metrics_namespace: str = Field(
    default="harness",
    description="Phase 4.1: metric name prefix. All metrics start with this.",
)

# === OpenTelemetry ===
observability_otlp_endpoint: str = Field(
    default="",
    description="Phase 4.1: OTLP collector endpoint (e.g. http://localhost:4317). "
                "Empty = no OTLP export.",
)
observability_otlp_headers: str = Field(
    default="",
    description="Phase 4.1: OTLP headers (comma-separated key=value). "
                "E.g. 'api-key=abc123,x-source=harness'.",
)
observability_trace_sample_ratio: float = Field(
    default=1.0, ge=0.0, le=1.0,
    description="Phase 4.1: trace sampling ratio. 1.0 = sample every request. "
                "0.1 = sample 10% (reduce collector load).",
)

# === Deep health ===
observability_health_ready_timeout_s: float = Field(
    default=2.0, gt=0, le=30.0,
    description="Phase 4.1: per-probe timeout for /health/ready. "
                "Default 2s. If a DB takes >2s to respond, mark as timeout (R3).",
)
observability_health_deep_timeout_s: float = Field(
    default=5.0, gt=0, le=60.0,
    description="Phase 4.1: total timeout for /health/deep. "
                "Default 5s. Sum of all probes (sqlite+qdrant+neo4j+queue+...).",
)
observability_health_require_qdrant: bool = Field(
    default=False,
    description="Phase 4.1: when True, /health/ready returns 503 if Qdrant is down. "
                "Default False (degraded, not unhealthy).",
)
observability_health_require_neo4j: bool = Field(
    default=False,
    description="Phase 4.1: when True, /health/ready returns 503 if Neo4j is down.",
)

# === Cost tracking ===
observability_cost_enabled: bool = Field(
    default=True,
    description="Phase 4.1: compute cost_usd for every LLM call. "
                "Default True. If False, cost is always 0.0 in logs/metrics.",
)
observability_cost_overrides: str = Field(
    default="",
    description="Phase 4.1: JSON overrides for cost table. Format: "
                "'{\"gpt-4o\": [3.00, 12.00]}'. Empty = use DEFAULT_COSTS table.",
)

# === Per-event enable (subset) ===
observability_log_http_requests: bool = Field(default=True, description="...")
observability_log_llm_calls: bool = Field(default=True, description="...")
observability_log_tool_calls: bool = Field(default=True, description="...")
observability_log_hook_dispatches: bool = Field(default=True, description="...")
observability_log_compactions: bool = Field(default=True, description="...")
observability_log_merge_queue_events: bool = Field(default=True, description="...")
observability_log_outbound_deliveries: bool = Field(default=True, description="...")
observability_log_privacy_decisions: bool = Field(default=True, description="...")
```

**Total new settings: 22** (4 master + 3 log + 2 metrics + 3 otel + 4 health + 2 cost + 8 per-event = 26; recounted: 4+3+2+3+4+2+8 = 26).

**Validator additions** in `_cascade_thresholds_ordered`:
```python
# Phase 4.1: trace sample ratio must be in [0, 1] (Pydantic enforces).
# Phase 4.1: log_max_files must be >= 1 (Pydantic enforces).
# Phase 4.1: cost_overrides, if non-empty, must be valid JSON.
import json
if self.observability_cost_overrides:
    try:
        overrides = json.loads(self.observability_cost_overrides)
        if not isinstance(overrides, dict):
            raise ValueError("observability_cost_overrides must be a JSON object")
        for k, v in overrides.items():
            if not isinstance(v, list) or len(v) != 2:
                raise ValueError(f"observability_cost_overrides[{k!r}] must be [input, output]")
            for cost in v:
                if not isinstance(cost, (int, float)) or cost < 0:
                    raise ValueError(f"observability_cost_overrides[{k!r}] values must be >= 0")
    except (json.JSONDecodeError, ValueError) as e:
        raise ValueError(f"observability_cost_overrides invalid: {e}")
```

---

## § 6. Step-by-Step Implementation Plan

8 шагов. Каждый — отдельный commit.

### Step 1: Foundation (logger + cost + metrics stub)
- **Files:**
  - `harness/observability/logger.py` — `LogEvent` + `JsonlLogger` (stdlib only, mirror `hooks/audit.py`).
  - `harness/observability/cost.py` — `DEFAULT_COSTS` + `compute_cost_usd()`.
  - `harness/observability/__init__.py` — public API exports (lazy).
  - `harness/config.py` — 8 new settings (master switches + log dir + cost).
  - `tests/test_observability_logger.py` (20 tests) — basic emit, rotation, lock, fail-open, redaction.
  - `tests/test_observability_cost.py` (10 tests) — known models, unknown models, overrides.
- **Mock count target:** +30.
- **Commit message:** `feat(phase-4.1): observability foundation — JsonlLogger + CostTable`.

### Step 2: Metrics registry + Prometheus stub
- **Files:**
  - `harness/observability/metrics.py` — `MetricsRegistry` + 12+ metric definitions.
  - `harness/config.py` — 4 more settings (metrics path/namespace, sample ratio).
  - `tests/test_observability_metrics.py` (25 tests) — counter inc, histogram observe, gauge set, no-op fallback, render.
- **Mock count target:** +25.
- **Commit message:** `feat(phase-4.1): MetricsRegistry + 12 Prometheus metrics (NoOp fallback)`.

### Step 3: Tracer + OTel stub
- **Files:**
  - `harness/observability/tracer.py` — `start_span()` context manager (NoOp fallback).
  - `harness/observability/exporter.py` — `setup_otlp_exporter()` (optional).
  - `harness/config.py` — 3 OTel settings (endpoint, headers, sample ratio).
  - `tests/test_observability_tracer.py` (15 tests) — span creation, attribute set, exception record, NoOp fallback.
- **Mock count target:** +15.
- **Commit message:** `feat(phase-4.1): OTel-compatible tracer (NoOp fallback) + OTLP exporter setup`.

### Step 4: DeepHealthChecker + 3 health routes
- **Files:**
  - `harness/observability/health.py` — `DeepHealthChecker` (3 methods: live/ready/deep).
  - `harness/server/routes/health.py` — extend with `/health/live`, `/health/ready`, `/health/deep`. Keep `/api/health` as alias.
  - `harness/server/app.py:lifespan` — construct `DeepHealthChecker`, attach to `app.state.health_checker`.
  - `harness/config.py` — 4 health settings (timeouts, require flags).
  - `tests/test_observability_health.py` (20 tests) — live always 200, ready probes with timeout, deep with diagnostics, alias to /api/health.
  - `tests/test_observability_health_api.py` (15 tests) — integration: HTTP GET on all 4 endpoints.
- **Mock count target:** +35.
- **Commit message:** `feat(phase-4.1): DeepHealthChecker + /health/{live,ready,deep} routes + /api/health alias`.

### Step 5: /metrics endpoint + opt-in wiring
- **Files:**
  - `harness/server/routes/metrics.py` — new route, gated by `settings.observability_prometheus_enabled`.
  - `harness/server/app.py` — register metrics route conditionally.
  - `harness/observability/metrics.py` — add `get_metrics().render()` route handler.
  - `tests/test_observability_metrics_endpoint.py` (10 tests) — opt-in default, response format, label cardinality.
- **Mock count target:** +10.
- **Commit message:** `feat(phase-4.1): /metrics endpoint (opt-in via observability_prometheus_enabled)`.

### Step 6: Wire 17 trigger points into production code
- **Files:**
  - `harness/agents/runner.py:_drive` — emit agent_run (log+span+metric).
  - `harness/server/llm/router.py:completion` — emit llm_call (log+span+metric).
  - `harness/agents/cascade.py:TierSelector.select` — emit cascade_decision.
  - `harness/agents/router.py:LLMRouterClassifier.classify` — emit routing_decision.
  - `harness/hooks/runner.py:HookRunner.fire` — emit hook_dispatch.
  - `harness/server/agent/runtime.py:ToolRuntime.execute` — emit tool_call.
  - `harness/context/compaction.py:ContextCompactor._run_slow_path` — emit compaction.
  - `harness/agents/merge_queue.py:MergeQueue._emit` — emit merge_queue_event.
  - `harness/agents/outbound.py:_deliver_one` — emit outbound_delivery.
  - `harness/privacy/zone_filter.py:PrivacyZoneFilter.check` — emit privacy_zone.
  - `harness/agents/webhook_handler.py:WebhookHandler.handle` — emit webhook_inbound.
  - `harness/memory/unified.py:UnifiedMemory.write` — emit memory_write.
  - `harness/server/app.py:lifespan` — emit session_lifecycle.
  - `harness/server/app.py` — add request middleware (request_started/completed).
  - `tests/test_observability_integration.py` (50 tests) — 1 per trigger point × 17 + edge cases.
- **Mock count target:** +50.
- **Commit message:** `feat(phase-4.1): wire 17 trigger points (log+span+metric)`.

### Step 7: Trust boundary test + fail-open behaviour
- **Files:**
  - `tests/test_observability_trust_boundary.py` (mirror `test_hooks_trust_boundary.py`) — AST scan of `harness/observability/`.
  - `tests/test_observability_failopen.py` (10 tests) — every emit/call wrapped in try/except, log write failure doesn't crash app, metric inc failure doesn't crash.
- **Mock count target:** +15.
- **Commit message:** `feat(phase-4.1): trust boundary + fail-open tests`.

### Step 8: Performance budget + docs + tag
- **Files:**
  - `tests/test_observability_perf.py` (5 tests) — assert overhead <5ms per JSONL emit, <2ms per metric inc, <10ms per span creation.
  - `docs/observability.md` (user-facing, ~400 lines) — setup, metric names, trace flow, JSONL schema, integration with Grafana/Tempo/Prometheus.
  - `docs/PHASE4-OBSERVABILITY-PLAN.md` (this file, move to spec).
  - `CHANGELOG.md` — v1.7.0 entry.
  - `README.md` — Phase 4.1 status update.
- **Mock count target:** +5.
- **Commit message:** `feat(phase-4.1): perf tests + docs/observability.md + v1.7.0 changelog`.

**Total mock tests added: 30 + 25 + 15 + 35 + 10 + 50 + 15 + 5 = 185 new tests.**

**Total cumulative mock tests: 1770 → 1955 (well above 1500 floor).**

---

## § 7. Trust Boundary + Tests

### § 7.1. Trust Boundary Test

`tests/test_observability_trust_boundary.py` — mirror `tests/test_hooks_trust_boundary.py`:

```python
"""Phase 4.1: Trust boundary test for observability library.

Mirrors tests/test_hooks_trust_boundary.py and tests/eval/test_eval_trust_boundary.py.
Parses every .py file under harness/observability/ with ast and verifies
that no top-level import references harness.agents, harness.server, or
harness.hooks (we don't import from hooks because the same boundary
must be one-way: hooks depends on observability, not vice versa).

A violation of this invariant breaks the entire layering model:
observability is the BOTTOM of the stack. If observability starts
importing from agents/server, we get circular imports and
observability is no longer reusable as a library.

Allowed:
    - stdlib imports
    - harness.config (Settings — read-only)
    - harness.observability (relative or absolute within package)

Forbidden:
    - harness.agents.* (sub-agents, runner, merge_queue, router, ...)
    - harness.server.* (FastAPI app, routes, lifespan, ...)
    - harness.hooks.* (hooks is LAYERED ABOVE observability; one-way dep)
"""
from __future__ import annotations
import ast
from pathlib import Path
import pytest

OBSERVABILITY_DIR = Path(__file__).parent.parent / "harness" / "observability"
assert OBSERVABILITY_DIR.is_dir(), f"harness/observability/ not found at {OBSERVABILITY_DIR}"

FORBIDDEN_PREFIXES: tuple[str, ...] = ("harness.agents", "harness.server", "harness.hooks")
ALLOWED_PREFIXES: tuple[str, ...] = ("harness.config", "harness.observability", "harness.redaction")

def _iter_files() -> list[Path]:
    return sorted(p for p in OBSERVABILITY_DIR.rglob("*.py") if p.is_file())

def _imported_modules(tree: ast.AST) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue
            if node.module:
                out.append((node.lineno, node.module))
    return out

class TestObservabilityTrustBoundary:
    def test_forbidden_imports(self) -> None:
        violations: list[str] = []
        for path in _iter_files():
            source = path.read_text(encoding="utf-8")
            try:
                tree = ast.parse(source, filename=str(path))
            except SyntaxError as e:
                violations.append(f"{path}:{e.lineno}: SyntaxError: {e.msg}")
                continue
            for lineno, module in _imported_modules(tree):
                for prefix in FORBIDDEN_PREFIXES:
                    if module == prefix or module.startswith(prefix + "."):
                        violations.append(
                            f"{path.relative_to(OBSERVABILITY_DIR.parent.parent)}:{lineno}: "
                            f"forbidden import: {module!r}"
                        )
        assert not violations, "Trust boundary violations:\n  " + "\n  ".join(violations)

    @pytest.mark.parametrize("path", _iter_files(), ids=lambda p: str(p.relative_to(OBSERVABILITY_DIR)))
    def test_each_file_parses(self, path: Path) -> None:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_no_circular_import(self) -> None:
        import harness.observability
        import harness.observability.logger
        import harness.observability.metrics
        import harness.observability.tracer
        import harness.observability.health
        import harness.observability.cost
        import harness.observability.exporter
        assert hasattr(harness.observability, "get_logger")
        assert hasattr(harness.observability, "get_metrics")
        assert hasattr(harness.observability, "start_span")
```

### § 7.2. Unit Tests (per module)

| Module | Test file | Tests | Coverage |
|--------|-----------|-------|----------|
| `logger.py` | `test_observability_logger.py` | 20 | basic emit, rotation, lock, fail-open, redaction, OTel context fields, empty payload |
| `cost.py` | `test_observability_cost.py` | 10 | known models, unknown models, overrides, edge cases (0 tokens, 1M tokens) |
| `metrics.py` | `test_observability_metrics.py` | 25 | counter inc, histogram observe, gauge set, render(), label cardinality, NoOp fallback |
| `tracer.py` | `test_observability_tracer.py` | 15 | span creation, attribute set, exception record, NoOp fallback, context manager |
| `health.py` | `test_observability_health.py` | 20 | live always 200, ready probes, deep diagnostics, timeout per probe, app_state DI |
| `/metrics` endpoint | `test_observability_metrics_endpoint.py` | 10 | opt-in default OFF, response format, no auth required, label cardinality cap |
| integration | `test_observability_integration.py` | 50 | one per trigger point × 17 + edge cases |
| fail-open | `test_observability_failopen.py` | 10 | log write failure, metric inc failure, span creation failure all no-op |
| perf | `test_observability_perf.py` | 5 | budget <5ms/<2ms/<10ms |

### § 7.3. Integration Test

`tests/test_observability_e2e.py` (10 tests) — full E2E:
1. Start FastAPI app with `observability_enabled=True, observability_prometheus_enabled=True`.
2. POST to `/api/chat` (WebSocket). Verify JSONL file grows.
3. GET `/health/live` → 200. GET `/health/ready` → 200. GET `/health/deep` → 200 with diagnostics.
4. GET `/metrics` → 200 with text format. Verify `harness_requests_total{route="/api/chat"}` present.
5. GET `/api/health` (backward compat) → 200 with `{status, version, project_root}`.
6. Stop Qdrant (mock). GET `/health/ready` → 503. (Optional: integration with real Qdrant)
7. Verify cost_usd is logged for LLM call.
8. Verify W3C `traceparent` header round-trip.
9. Verify OTLP endpoint receives spans (mocked).
10. Verify observability is OFF (default) — `/metrics` returns 404.

---

## § 8. Performance Budget

| Operation | Budget | Measurement | Failsafe |
|-----------|--------|-------------|----------|
| JSONL log emit (sync write) | <5ms | wall time from `emit()` to return | try/except + stdlib logger fallback |
| Prometheus counter inc | <2ms | wall time from `.inc()` to return | try/except + stdlib logger warning |
| OTel span creation | <10ms | wall time from `start_span()` context manager | try/except + NoOp span |
| Deep health check (3 probes) | <5s total | `asyncio.wait_for` sum | per-probe timeout 2s, return degraded |
| /metrics endpoint render | <100ms | wall time from request to response | cache for 5s (separate setting) |
| HTTP middleware overhead | <1ms | wall time per request | try/except + skip on error |

**Failsafe contract:** EVERY observability call is wrapped in `try/except`. On exception, log a warning via stdlib logger and continue. Observability NEVER crashes the calling code. This is a hard invariant — verified by `test_observability_failopen.py`.

**Why sync JSONL write (not async queue):**
- asyncio.Queue + background drainer loses logs on SIGKILL (R5).
- Sync write with `threading.Lock` is ~1-2ms (mirror `hooks/audit.py`).
- File rotation is cheap (date-based, no I/O if file exists).
- Total overhead at 100 RPS × 5 log emits = 500 writes/s × 1.5ms = 750ms/s = 75% CPU. **Acceptable for 100 RPS**, NOT acceptable for 1000+ RPS. Mitigation: settings toggle to reduce log events at high RPS (R7).

**Why opt-in Prometheus:**
- `prometheus_client` Registry + thread-safe counters add ~0.5-1ms per inc.
- Histograms are more expensive (lock + bucket sort). ~1-2ms per observe.
- Total at 100 RPS × 3 metrics = 300 ops/s × 1.5ms = 450ms/s = 45% CPU. **Acceptable IF opt-in**, NOT acceptable as default.

---

## § 9. Rollout + Tag

### Version: v1.7.0

### Migration notes

| Old (Phase 4.0) | New (Phase 4.1) | Action |
|-----------------|-----------------|--------|
| `GET /api/health` | `GET /api/health` (still works) OR `GET /health/deep` | No change required. |
| stdlib `logger.info("...")` | stdlib `logger.info("...")` (still works) + JSONL mirror | No change required. |
| No `/metrics` | `GET /metrics` (opt-in) | Enable via `OBSERVABILITY_PROMETHEUS_ENABLED=true` env. |
| No trace context | W3C `traceparent` header auto-extracted | No change; observability is opt-in. |
| No cost tracking | `cost_usd` in JSONL + `harness_llm_cost_total_usd` metric | Auto-enabled when `observability_enabled=True`. |

### Backward compat guarantees
- `GET /api/health` returns the same shape as Phase 0: `{status, version, project_root}` (verified by `test_observability_health_api.py::test_api_health_alias`).
- All stdlib `logger.info("...")` calls continue to emit to console. JSONL is ADDITIVE.
- No existing tests should break. If they do, the failing test indicates an instrumentation bug.

### Tag
```
git tag -a v1.7.0 -m "Phase 4.1 — Observability (JSONL + OTel + Prometheus + health)"
git push origin v1.7.0
```

### Rollout sequence
1. `feat/phase-4-observability` branch off master.
2. 8 commits (Step 1-8 above).
3. Run full test suite (`pytest tests/`). Target: 1955 mock tests pass, 0 regressions.
4. Open PR against master.
5. Mark reviews, merge.
6. Tag v1.7.0.
7. Update master roadmap (`docs/roadmap.md` Phase 4 = 1/12 done; the rest deferred).

---

## § 10. Risks and Mitigations

| ID | Severity | Description | Mitigation |
|----|----------|-------------|------------|
| **R1** | HIGH | `prometheus_client` registry locks may block asyncio event loop on high RPS. | Use `prometheus_client.Counter` (thread-safe, but C-level lock — fast). Avoid `Summary` (uses RLock). Default `observability_prometheus_enabled=False` keeps overhead zero. |
| **R2** | MEDIUM | JSONL file rotation at midnight races with active write from another thread. | Mirror `hooks/audit.py`: open-write-close per line (no persistent file handle). Date suffix means new file = new path, no race. |
| **R3** | HIGH | Deep health probes Qdrant/Neo4j with no timeout → 1 slow probe blocks load balancer. | Per-probe `asyncio.wait_for(timeout=observability_health_ready_timeout_s=2.0)`. Default 2s. |
| **R4** | MEDIUM | OTel SDK not installed but `observability_otlp_enabled=True` → silent failure. | Check `_HAS_OTEL` flag at startup, log warning, set OTLP span processor = no-op. |
| **R5** | HIGH | Async log queue + drainer loses logs on crash. | Sync write with `threading.Lock` (mirror `hooks/audit.py`). NO async queue. |
| **R6** | MEDIUM | Per-session labels in metrics explode cardinality (10k+ sessions). | NO `session_id` as metric label. Use `route` and `method` only. Session correlation via trace_id, NOT via metric label. |
| **R7** | MEDIUM | High-RPS workloads (1000+ RPS) make JSONL writes dominate CPU. | Settings toggle per-event: `observability_log_llm_calls=False` etc. Operators can disable hot events without disabling everything. |
| **R8** | LOW | OTel context propagation breaks on WebSocket upgrade → spans orphan. | Test with `test_observability_websocket_trace.py` (10 tests). Manual verification with `wscat -c ws://...` + check trace_id in logs. |

---

## § 11. ADR-005: Observability Architecture

### Status
Proposed. Pending Mark approval.

### Context
Solomon Harness has hit production usage. Mark needs visibility into:
- Latency per LLM call (model + tier)
- Token costs (per session, per day)
- Hook dispatch latency (Phase 4.0 hooks could slow chat loop)
- Merge queue depth (jobs backing up)
- Privacy zone decisions (PII being blocked)
- Webhook delivery success rate

We have 3 architectural options:

### Option A: Log-only (file-based, no metrics/traces)
**Pros:** Zero deps, simplest implementation.
**Cons:** Operators need to grep JSONL files to find anything. No time-series data. No trace correlation. Poor fit for "find slow LLM calls in last hour".

### Option B: OTel-only (traces + logs, no Prometheus)
**Pros:** Single backend (OTLP collector). Trace correlation built-in.
**Cons:** Operators used to Prometheus dashboards would need a separate tool. No high-cardinality time-series. Metric scraping in OTel = non-standard.

### Option C: Prometheus-only (metrics + logs, no traces)
**Pros:** Operator-familiar (most production deployments have Prometheus). Time-series queries are easy.
**Cons:** No trace correlation. Debugging "why was this request slow" requires log diving.

### Option D: All three (JSONL + OTel + Prometheus) — CHOSEN
**Rationale:**
- JSONL is the universal fallback (any operator can `grep`).
- OTel traces for trace correlation and distributed debugging.
- Prometheus for time-series metrics (operator-familiar).
- Each is opt-in (default OFF for Prometheus and OTLP) so overhead is zero by default.

### Decision
Implement Option D. All three observability surfaces, each opt-in via settings.

### Consequences
- **+0 new required deps** (all extras in `[observability]` extra).
- **+~185 mock tests** (target 1955 total, well above 1500 floor).
- **+3 new HTTP routes** (`/metrics`, `/health/live`, `/health/ready`).
- **+1 backward-compat alias** (`/api/health` → `/health/deep?minimal=true`).
- **+22 new settings** (4 master + 3 log + 2 metrics + 3 otel + 4 health + 2 cost + 8 per-event = 26; recounted: 26).
- **+2 new module directories** (`harness/observability/` with 6 modules).
- **+1 static test** (`test_observability_trust_boundary.py`).

### Alternatives considered
- **StatsD** — out of fashion, OTel is the modern standard.
- **Sentry SDK** — error tracking, not metrics. Phase 4.6+ candidate.
- **Datadog APM agent** — proprietary, vendor lock-in. Operator-configured.
- **Log-only (Option A)** — too limiting for production.

---

## § 12. Adversarial Review (5+ BLOCKERS, 5+ RISKS, 5+ CONCERNS)

### BLOCKERS (B) — must be fixed before merge

| ID | Category | Description | Fix |
|----|----------|-------------|-----|
| **B1** | Trust boundary | `health.py` needs to call Qdrant/SQLite/Neo4j probes. Direct import breaks trust boundary. | Use duck-typed DI: probes are passed via `app.state` (mirror Phase 1.6 TokenStore). `health.py` NEVER imports `harness.agents` or `harness.server`. The wiring happens in `app.py:lifespan`. |
| **B2** | Backward compat | `GET /api/health` (Phase 0) returns `{status, version, project_root}`. If we change it to return deep health dict, existing clients break. | Keep `GET /api/health` as alias for `GET /health/deep?minimal=true`. The minimal query param returns the Phase 0 shape. Verified by `test_observability_health_api.py::test_api_health_alias`. |
| **B3** | Fail-open | If JSONL write fails (disk full, permission denied), app should NOT crash. Without try/except, `logger.info()` could raise. | Every `JsonlLogger.emit()` is wrapped in try/except + stdlib logger fallback. Verified by `test_observability_failopen.py`. |
| **B4** | Cardinality explosion | Using `session_id` as Prometheus label → 10k+ unique label values → OOM. | NEVER use `session_id` / `agent_id` / `request_id` as metric labels. Use only `route`, `method`, `status`, `model`, `tier`, `event`. Session correlation via trace_id, NOT metric label. |
| **B5** | Trace context loss | W3C `traceparent` header NOT extracted from incoming HTTP request → spans orphan (no parent). | Add FastAPI middleware that extracts `traceparent` from headers and starts a root span. Verified by `test_observability_websocket_trace.py` (WS upgrade + header extraction). |
| **B6** | Sync write on hot path | `JsonlLogger.emit()` does sync file I/O. On hot path (per LLM call), 1-2ms overhead. At 1000 RPS, 100% CPU. | Settings toggle per-event (`observability_log_llm_calls=False` etc.). Operators can disable hot events at high RPS. **Failsafe is opt-out per event, NOT per-emit** — emit still happens but at lower rate. |
| **B7** | Health probe DOS | `/health/ready` probes Qdrant with no timeout → 1 slow Qdrant blocks load balancer. | Per-probe `asyncio.wait_for(timeout=observability_health_ready_timeout_s=2.0)`. Default 2s. If timeout, return `degraded`, NOT crash. |
| **B8** | Prometheus registry lock | `prometheus_client.Counter.inc()` is fast (C-level) but `Histogram.observe()` is slower (lock + bucket sort). On hot path, ~2ms each. | Use `Counter` for hot metrics (requests_total). Use `Gauge` for state (active_sessions). Use `Histogram` sparingly (only for latency). Document this in `docs/observability.md`. |

### RISKS (R) — may go wrong, mitigated

| ID | Description | Mitigation |
|----|-------------|------------|
| **R1** | `prometheus_client` + `aiohttp` event loop conflict (registry uses threading.Lock). | Use `prometheus_client`'s own async-safe Registry. Default to `generate_latest()` from sync context. Verified by `test_observability_metrics.py::test_async_safety`. |
| **R2** | JSONL rotation at midnight races with active write from another thread. | Mirror `hooks/audit.py`: open-write-close per line. Date suffix means new file = new path, no race. |
| **R3** | Deep health check probes Neo4j with timeout, may DOS the database. | Per-probe timeout 2s. Operators can disable specific probes via `observability_health_require_qdrant=False` etc. |
| **R4** | OTLP exporter fails to start if collector down, blocks app startup. | Try/except in `setup_otlp_exporter()`. On failure, log warning, continue without OTLP. App never blocks on OTLP. |
| **R5** | W3C `traceparent` header malformed → OTel context propagation breaks. | OTel SDK validates format. If invalid, log warning, start new trace. |
| **R6** | Log file grows unbounded (no rotation policy). | Daily rotation by date suffix. `observability_log_max_files=30` (1 month retention). Background task (1/hour) deletes old files. |
| **R7** | Cost table hardcoded → goes stale. | `observability_cost_overrides` setting (JSON) lets operators override. Also accept env var `OBSERVABILITY_COST_<MODEL>` (parsed in `cost.py`). |
| **R8** | Multi-worker deployments (uvicorn --workers 4) get 4× /metrics scrapes. | Each worker has its own registry. Standard pattern. Document in `docs/observability.md`. Aggregator (Prometheus) handles this correctly. |
| **R9** | Observability test depends on real Qdrant/Neo4j (CI flakiness). | All probes are mocked via `app_state` DI. No real DBs needed for unit tests. Integration test (`test_observability_e2e.py`) is marked `@pytest.mark.requires_qdrant` and skipped if Qdrant not running. |
| **R10** | OTel context token overhead (~1µs per context propagation) accumulates at 10k+ spans. | Default sample ratio = 1.0 in dev, 0.1 in prod (operator configurable). |

### CONCERNS (C) — code quality, not blocking

| ID | Description | Resolution |
|----|-------------|------------|
| **C1** | LogEvent has 11 fields. Hard to remember all of them. | Use kwargs in `JsonlLogger.emit()`. `emit(event="...", payload={...}, session_id="...")` is self-documenting. |
| **C2** | MetricsRegistry creates metrics lazily. If two callers ask for the same metric with different label sets, we get TWO metrics. | Document: "Label sets must match across all inc/observe calls." Add a test that asserts this. |
| **C3** | DeepHealthChecker probes are duck-typed via `app_state`. Brittle if `app_state` keys change. | Use explicit `ProbeRegistry` class (DI). Probes registered in `lifespan`, not accessed via `app_state`. |
| **C4** | `start_span()` context manager requires OTel SDK. If OTel extras not installed, we get NoOp. Magic. | Document: "Install `[observability]` extra for OTel support. Default = NoOp tracer." |
| **C5** | `JsonlLogger` uses stdlib logger for fallback. If stdlib logger is also broken (e.g. logging.Handler exception), we lose logs. | Belt and suspenders: stdlib `print()` as last-resort. Rare in practice. |
| **C6** | `compute_cost_usd()` is a pure function. Easy to call from production code. But what about streaming responses? | For streaming, cost is computed on `stream_done` event. Document. |
| **C7** | `/metrics` endpoint returns raw text format. Should we add auth? | Document: "Default = no auth. Operators should restrict via reverse proxy (nginx with `allow` rules)." Add `observability_metrics_auth_required` setting (default False) for opt-in token auth. |
| **C8** | 26 new settings is a lot. Operators may not know which to tune. | Document in `docs/observability.md` "Tuning guide" section. Top 3 settings: `observability_prometheus_enabled`, `observability_trace_sample_ratio`, `observability_log_max_files`. |
| **C9** | `test_observability_trust_boundary.py` may break if someone adds a relative import that LOOKS like a forbidden prefix. | Test only checks absolute imports (relative imports can't reach forbidden prefixes). |
| **C10** | Deep health check returns 503 on Qdrant down. If Qdrant is intentionally down for maintenance, load balancer pulls all instances. | `observability_health_require_qdrant=False` (default) means Qdrant down = degraded, not unhealthy. Operator can opt-in to strict mode. |

---

## § 13. Files (NEW/MODIFIED)

### NEW files
```
harness/observability/
├── __init__.py                                # Public API (lazy exports)
├── logger.py                                  # LogEvent + JsonlLogger
├── metrics.py                                 # MetricsRegistry
├── tracer.py                                  # start_span() + NoOp fallback
├── health.py                                  # DeepHealthChecker
├── cost.py                                    # DEFAULT_COSTS + compute_cost_usd
└── exporter.py                                # OTLP exporter setup

tests/
├── test_observability_logger.py               # 20 tests
├── test_observability_cost.py                 # 10 tests
├── test_observability_metrics.py              # 25 tests
├── test_observability_tracer.py               # 15 tests
├── test_observability_health.py               # 20 tests
├── test_observability_health_api.py           # 15 tests
├── test_observability_metrics_endpoint.py     # 10 tests
├── test_observability_integration.py          # 50 tests (17 trigger points)
├── test_observability_failopen.py             # 10 tests
├── test_observability_trust_boundary.py       # CRITICAL: trust boundary test
├── test_observability_perf.py                 # 5 tests
├── test_observability_websocket_trace.py      # 10 tests
└── test_observability_e2e.py                  # 10 tests (E2E)

docs/
├── observability.md                            # User-facing docs (~400 lines)
└── PHASE4-OBSERVABILITY-PLAN.md                # This file
```

### MODIFIED files
```
harness/config.py                              # +26 settings
harness/server/app.py                          # +request middleware, +health route mounting, +metrics route (conditional)
harness/server/routes/health.py                # +3 health routes, alias /api/health
harness/agents/runner.py                       # +emit agent_run (Step 6)
harness/agents/router.py                       # +emit routing_decision (Step 6)
harness/agents/cascade.py                      # +emit cascade_decision (Step 6)
harness/agents/merge_queue.py                  # +emit merge_queue_event (Step 6)
harness/agents/outbound.py                     # +emit outbound_delivery (Step 6)
harness/agents/webhook_handler.py              # +emit webhook_inbound (Step 6)
harness/hooks/runner.py                        # +emit hook_dispatch (Step 6)
harness/server/agent/runtime.py                # +emit tool_call (Step 6)
harness/server/agent/loop.py                   # +start_span per iteration (Step 6)
harness/server/llm/router.py                   # +emit llm_call + cost_usd (Step 6)
harness/context/compaction.py                  # +emit compaction (Step 6)
harness/privacy/zone_filter.py                 # +emit privacy_zone (Step 6)
harness/memory/unified.py                      # +emit memory_write (Step 6)
harness/server/routes/metrics.py               # NEW: /metrics route
pyproject.toml                                 # +[observability] extras
CHANGELOG.md                                   # v1.7.0 entry
README.md                                      # Phase 4.1 status update
```

### UNCHANGED files
```
harness/agents/pre_compact.py                  # ZERO TOUCH (backward compat)
harness/hooks/audit.py                         # ZERO TOUCH (reference pattern only)
master roadmap                                 # Read-only, updated post-coding
```

### Total file count
- **NEW: 20** (6 observability source + 13 tests + 1 trust test = 20... actually 6 + 13 = 19, +1 plan = 20)
- **MODIFIED: 19** (1 config + 1 app + 1 health route + 13 production + 1 metrics route + 1 pyproject + 1 CHANGELOG + 1 README = 20; recounted: 1+1+1+13+1+1+1+1 = 20)
- **UNCHANGED: 3** (pre_compact.py + hooks/audit.py + master roadmap)

---

## § 14. Стек — что добавляем, что НЕ добавляем

### ДОБАВЛЯЕМ (zero new required deps, [observability] extras)
- `prometheus-client>=0.20` (extra) — counters/histograms/gauges
- `opentelemetry-api>=1.24` (extra) — Tracer/Span interfaces
- `opentelemetry-sdk>=1.24` (extra) — TracerProvider
- `opentelemetry-exporter-otlp>=1.24` (extra) — OTLP export

### Existing deps USED
- `pydantic` (existing) — Settings
- `pydantic_settings` (existing) — Settings class
- `asyncio` (stdlib) — wait_for, gather
- `json` (stdlib) — JSONL encoding
- `threading` (stdlib) — Lock
- `pathlib` (stdlib) — log file paths
- `datetime` (stdlib) — timestamps, rotation
- `ast` (stdlib) — static trust boundary test
- `logging` (stdlib) — fallback logger
- `typing` (stdlib) — type hints
- `dataclasses` (stdlib) — LogEvent
- `contextlib` (stdlib) — start_span context manager
- `fastapi` (existing) — middleware, routes
- `aiosqlite` (existing) — SQLite health probe (via DI)

### НЕ добавляем (explicitly OUT)
- `sentry-sdk` — Phase 4.6+ (error tracking, not metrics)
- `statsd` — OTel is the modern standard
- `datadog` — vendor lock-in, operator-configured
- `fluent-logger` / `python-logstash-async` — log shipping is external
- `watchfiles` / `watchdog` — hot-reload deferred to Phase 4.2
- `jsonschema` — use Pydantic (mirrors Phase 4.0 B6)
- `tenacity` — stdlib retry sufficient for OTLP

---

## § 15. Поэтапная сводка

| Step | Commit | Files | Tests | Cumulative |
|------|--------|-------|-------|------------|
| 1 | Foundation (logger + cost) | 4 new + 1 mod | +30 | 1770 → 1800 |
| 2 | MetricsRegistry + 12 metrics | 1 new + 1 mod | +25 | 1800 → 1825 |
| 3 | OTel tracer + exporter setup | 2 new + 1 mod | +15 | 1825 → 1840 |
| 4 | DeepHealthChecker + 3 routes | 2 new + 2 mod | +35 | 1840 → 1875 |
| 5 | /metrics endpoint | 1 new + 2 mod | +10 | 1875 → 1885 |
| 6 | 17 trigger points | 14 mod | +50 | 1885 → 1935 |
| 7 | Trust boundary + fail-open | 2 new | +15 | 1935 → 1950 |
| 8 | Perf tests + docs + tag | 1 new + 4 mod | +5 | 1950 → 1955 |

**Total:** 8 commits, +185 tests, 0 new required deps, 0 breaking changes.

---

## § 16. Заключение

Phase 4.1 — это **visibility** phase. Цель: дать Марку и операторам полную картину работы production-harness через 3 observability surface (JSONL + OTel + Prometheus) + 3 health endpoints (live/ready/deep) + per-task cost tracking, не ломая backward compat с Phase 0 (1 alias: `/api/health` → `/health/deep?minimal=true`) и Phase 4.0 hooks (17 trigger points — additional instrumentation, не замены). Trust boundary строго изолирован: `harness/observability/` — bottom layer, не импортирует ничего из `harness.agents` / `harness.server` / `harness.hooks`. Static test это проверяет.

Все 8 BLOCKERS идентифицированы и имеют фиксы в § 12. Все 10 RISKS трекаются с митигациями. Все 10 CONCERNS — code quality, не блокеры.

После coding — `feat/phase-4-observability` PR → review → merge → tag v1.7.0 → update master roadmap (Phase 4 = 1/12 done; the rest deferred to Phase 4.2-4.7).

**Next phase (4.2):** hot-reload hooks + agents via file watcher.
**Next phase (4.3):** `/api/* → /api/v1/*` migration.
**Next phase (4.4):** Elicitation / Notification observability events.
**Next phase (4.5):** `harness observability` CLI subcommand.
**Next phase (4.6):** Sentry-style error tracking.
**Next phase (4.7):** Cross-PR distributed traces.

---

**Last updated:** 2026-06-16
**Version:** 1.0 (DRAFT — pending Mark approval)
