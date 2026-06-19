# Hooks Framework — Solomon Harness v1.22.0+

> **Phase 4.0–4.12 — Hooks framework** для production-пайплайна. Позволяет встраивать side-effects (логирование, валидация, блокировки, аудит, интерактивные запросы к человеку, уведомления) в ключевые точки жизненного цикла агента через декларативные **hook specs** с 4 транспортами: **builtin / subprocess / http / llm**.
>
> Покрытие: **16 событий**, **12 builtin хуков**, **4 транспорта**, **hot-reload** (Phase 4.2), **defensive layer** (rate limiter + circuit breaker, Phase 4.8), **payload schema validation** (Phase 4.6), **hook pattern library** (8 готовых JSON specs, Phase 4.10).

---

## Содержание

1. [Что такое hooks](#1-что-такое-hooks)
2. [16 событий (EventType)](#2-16-событий-eventtype)
3. [Решения (Decision)](#3-решения-decision)
4. [4 транспорта](#4-4-транспорта)
5. [12 встроенных хуков (builtin)](#5-12-встроенных-хуков-builtin)
6. [Регистрация и настройка](#6-регистрация-и-настройка)
7. [Конфигурация](#7-конфигурация)
8. [Audit log (NDJSON)](#8-audit-log-ndjson)
9. [Hot-reload (Phase 4.2)](#9-hot-reload-phase-42)
10. [Defensive layer: rate limiter + circuit breaker (Phase 4.8)](#10-defensive-layer-rate-limiter--circuit-breaker-phase-48)
11. [Hook pattern library (Phase 4.10)](#11-hook-pattern-library-phase-410)
12. [Примеры (built-in / subprocess / http / llm)](#12-примеры)
13. [Troubleshooting](#13-troubleshooting)
14. [См. также](#14-см-также)

---

## 1. Что такое hooks

**Hook** — это пользовательская или системная функция, которая вызывается в определённый момент жизненного цикла агента. Hook получает `HookContext` и возвращает `HookDecision` (allow / block / modify). Несколько hook'ов для одного события **агрегируются** (block > modify > allow).

**Где срабатывают:** см. таблицу событий ниже — покрытие от `PreToolUse` (каждый tool call) до `OnRoutingDecision` (классификация T1/T2/T3 LLM router'ом).

**Когда НЕ нужны hooks:**

- Если нужно локальное логирование — `logger.info(...)` проще.
- Если нужен hot-reload — Phase 4.2 (см. `docs/roadmap.md`).
- Если нужна метрика/trace — Phase 4.1 (observability).

**Trust boundary:** `harness.hooks/*` НЕ импортирует `harness.agents` или `harness.server`. Это enforced статическим тестом `tests/test_hooks_trust_boundary.py`.

---

## 2. 16 событий (EventType)

| Событие | Где срабатывает | Payload (`HookContext.payload`) | Решение |
|---------|-----------------|----------------------------------|---------|
| `PreToolUse` | **Перед каждым tool call** в `ToolRuntime.execute` | `{"tool_name": str, "arguments": dict}` | `block` → abort; `modify` → заменить args |
| `PostToolUse` | **После каждого tool call** | `{"tool_name": str, "arguments": dict, "result": ToolResult}` | `block` → result заменяется на error |
| `Stop` | `AgentLoop` завершается (max iter / explicit stop) | `{"reason": str, "final_message": str, "iterations": int}` | allow/modify |
| `SubagentStart` | **Перед** `AgentRunner.run` | `{"agent_name": str, "prompt": str, "model": str}` | allow/block |
| `SubagentStop` | **После** `AgentRunner.run` | `{"agent_name": str, "result": str, "duration_ms": float}` | allow/block |
| `SessionStart` | FastAPI lifespan startup | `{"session_id": str, "working_dir": str}` | allow |
| `SessionEnd` | FastAPI lifespan shutdown | `{"session_id": str, "duration_seconds": float}` | allow |
| `UserPromptSubmit` | Каждое WebSocket user message | `{"prompt": str, "session_id": str}` | block → отклонить prompt |
| `PreCompact` | **Перед** `ContextCompactor.maybe_compact` | `{"messages_count": int, "tokens_estimate": int}` | allow (snapshot state) |
| `InstructionsLoaded` | `AgentSpec` загружен с диска | `{"spec_name": str, "file_path": str}` | allow |
| `PermissionRequest` | Tool запрещён denylist'ом (Phase 4.5+: `_bash` + 5 file tools + scratchpad WRITE) | `{"tool_name": str, "arguments": dict, "reason": str}` | `modify` → allow (override) |
| `OnMemoryWrite` | Внутри `UnifiedMemory.write` (post-redact, pre-persist) | `{"layer": "L1"/"L2", "key_hash": str, "scope": str, "size_bytes": int}` | `block` → отменить запись |
| `OnRoutingDecision` | После `LLMRouterClassifier.classify` | `{"tier": "T1"/"T2"/"T3", "model": str, "confidence": float}` | `modify` → override tier/model |
| `OnCompaction` | После `ContextCompactor` (cache-miss only, если opt-in) | `{"session_id": str, "summary_preview": str, "saved_tokens": int}` | allow (post-process) |
| `Elicitation` | Phase 4.3+ — интерактивный запрос к человеку (requires_confirmation) | `{"question": str, "options": list, "default_answer": str, "requires_confirmation": bool}` | `modify` → inject answer (broker.publish → wait → answer_source) |
| `Notification` | Phase 4.3+ — асинхронное уведомление (fanout по каналам) | `{"message": str, "severity": "info"\|"warn"\|"error", "channels": ["stdout","webhook","desktop","slack","teams"]}` | allow (never block) |

**Per-event enable flag:** каждое событие имеет настройку `hooks_on_<event>_enabled: bool` (default: True).

**Per-event cap:** `hooks_max_per_event: int` (default 10) — при превышении лишние hook'и дропаются с warning.

---

## 3. Решения (Decision)

Каждый hook возвращает `HookDecision(decision, hook_id, duration_ms, output, error)`:

```python
from harness.hooks import HookDecision

# Allow — пропустить дальше
HookDecision(decision="allow", hook_id="my-hook")

# Block — прервать выполнение
HookDecision(
    decision="block",
    hook_id="my-hook",
    output={"reason": "policy violation: rm -rf"},
)

# Modify — продолжить с изменённым payload
HookDecision(
    decision="modify",
    hook_id="my-hook",
    output={"payload": {"new_args": "..."}},
)
```

**Агрегация (для N hook'ов на одно событие):**

1. **Первый block побеждает** — `final_decision = "block"`, `blocked_by = <hook_id первогo blocker>`.
2. **Последний modify побеждает** для payload — `final_decision = "modify"`, `final_payload = последний modify.output["payload"]`.
3. **Иначе allow.**
4. **Fail-closed** (если `settings.hooks_fail_open=False`): любая ошибка в hook → итоговое решение = `block`.

```python
aggregate = await runner.fire(ctx)
if aggregate.final_decision == "block":
    raise RuntimeError(f"Blocked by {aggregate.blocked_by}")
```

---

## 4. 4 транспорта

### 4.1. builtin (in-process Python)

Самый быстрый. Hook — async функция в том же процессе.

```python
from harness.hooks import EventType, HookContext, HookDecision, HookSpec

async def my_hook(ctx: HookContext) -> HookDecision:
    if "rm -rf" in str(ctx.payload.get("arguments", "")):
        return HookDecision(
            decision="block", hook_id="builtin.rm-guard",
            output={"reason": "destructive command"},
        )
    return HookDecision(decision="allow", hook_id="builtin.rm-guard")

spec = HookSpec(
    hook_id="builtin.rm-guard",
    event=EventType.PRE_TOOL_USE,
    transport="builtin",
    callable=my_hook,
    timeout_ms=500,
)
```

**Плюсы:** zero overhead, type-safe, easy debug.
**Минусы:** живёт в одном процессе с агентом (нельзя изолировать).

---

### 4.2. subprocess (JSON via stdin/stdout)

Hook — отдельный Python (или любой) скрипт. Runner отправляет `HookContext` (JSON) в stdin, читает JSON из stdout.

**Протокол:**

| Условие | Результат | Действие runner'а |
|---------|-----------|-------------------|
| exit 0 + JSON на stdout | `HookDecision` (parsed) | Используется |
| exit 0 + no JSON | empty decision | `allow` (no-op) |
| exit 2 + stderr (non-empty) | `block` with reason = stderr | Прерывает |
| exit 2 + no stderr | `block` (no reason) | Прерывает |
| exit 1 / 3+ / exception | `allow` + error | fail-open |
| Timeout | `allow` + "timeout" | fail-open |

**Пример скрипта (`hooks/my_blocker.py`):**

```python
import json
import sys

def main() -> int:
    ctx = json.load(sys.stdin)
    args = ctx.get("payload", {}).get("arguments", {})
    if args.get("command", "").startswith("rm -rf"):
        print("destructive command blocked", file=sys.stderr)
        return 2  # block
    return 0  # allow

if __name__ == "__main__":
    sys.exit(main())
```

**Spec:**

```python
spec = HookSpec(
    hook_id="subprocess.rm-blocker",
    event=EventType.PRE_TOOL_USE,
    transport="subprocess",
    script_path="/abs/path/to/hooks/my_blocker.py",
    timeout_ms=1000,
)
```

**Плюсы:** процесс-изоляция (hook крашится → агент жив); любой язык.
**Минусы:** ~50ms overhead на spawn; нужно абсолютный путь.

**Разрешённые пути:** `settings.hooks_subprocess_allowed_paths` (default: `.harness/hooks/**`) — глоб по fnmatch с recursive `**` (через `match_glob` из `harness.privacy.path_match`).

---

### 4.3. http (POST + JSON)

Hook — HTTP endpoint. Runner отправляет POST с `HookContext` (JSON), читает `HookDecision` (JSON).

**Пример сервера (FastAPI):**

```python
from fastapi import FastAPI, Request

app = FastAPI()

@app.post("/hook")
async def hook(request: Request):
    ctx = await request.json()
    if "rm -rf" in str(ctx.get("payload", {}).get("arguments", "")):
        return {
            "decision": "block", "hook_id": "http.rm-guard",
            "output": {"reason": "destructive"},
        }
    return {"decision": "allow", "hook_id": "http.rm-guard"}
```

**Spec:**

```python
spec = HookSpec(
    hook_id="http.rm-guard",
    event=EventType.PRE_TOOL_USE,
    transport="http",
    url="https://my-hooks.example.com/hook",
    headers={"Authorization": "Bearer my-token"},
    timeout_ms=2000,
)
```

**Поведение:** 4xx/5xx → `allow` (fail-open). Timeout → `allow`. Network error → `allow`. Только 2xx с JSON `{"decision": ...}` интерпретируется.

**Плюсы:** переиспользование существующих API; cross-language.
**Минусы:** сетевая задержка (~10-100ms); требует external deployment.

---

### 4.4. llm (LLM-as-hook)

Hook — это LLM-prompt, который классифицирует контекст и возвращает решение.

**Как работает:**

1. Runner отправляет prompt + payload в `LLMRouter` (DI, default: `runner.llm_router`).
2. Router выбирает T1/T2/T3 по сложности (cost cascade).
3. LLM отвечает в формате `{"decision": "...", "reason": "..."}`.
4. Regex/JSON парсер извлекает decision; `reason` cap = 200 chars; `payload` cap = 1KB.

**Spec:**

```python
spec = HookSpec(
    hook_id="llm.safety-classifier",
    event=EventType.PRE_TOOL_USE,
    transport="llm",
    model="qwen3:8b",  # T1 default
    prompt="""Is this tool call safe? Args: {payload.arguments}
Respond JSON: {{"decision": "allow"|"block", "reason": "..."}}""",
    timeout_ms=5000,
)
```

**Плюсы:** zero-code catch-all (LLM классифицирует); полезно для политик.
**Минусы:** 100ms-2s latency; non-deterministic; стоимость LLM-вызова.

**Требование:** `HookRunner(llm_router=<router>)` (DI) — runner **НЕ** импортирует `harness.server.llm.router` (trust boundary).

---

## 5. 12 встроенных хуков (builtin)

12 хуков идут в коробке и подключаются по `settings.hooks_builtin_<name>_enabled: bool` или через JSON spec в `.harness/hooks/*.json` (hot-reloadable).

### Framework builtins (5, Phase 4.0)

#### 5.1. log (default: ON)
Логирует каждое событие через stdlib `logging` на INFO. Не меняет решение.

#### 5.2. validate (default: ON)
Валидирует payload против Pydantic-схемы (Phase 4.6: `harness/hooks/schemas.py` — 16 моделей). `block` при невалидном. Fail-open: при ошибке схемы payload остаётся оригинальным (hook dispatch не должен падать).

#### 5.3. block_dangerous (default: ON)
Regex-блокировка деструктивных команд в `PreToolUse` для `bash` tool.

**7 паттернов:** `rm -r[f] /<path>`, `mkfs /dev/...`, `dd of=/dev/...`, `:(){ :|:& };:`, `DROP DATABASE`, `TRUNCATE TABLE`, `format c:`.

**Настройка:** `settings.hooks_block_dangerous_patterns: str` (comma-separated regex).

#### 5.4. inject_context (default: OFF)
Инжектит L0 (scratchpad) в system prompt при `InstructionsLoaded`. Использует Phase 3 v1.2.0 `ScratchpadStore`.

#### 5.5. autosave (default: ON)
На `SessionEnd` пишет `data/audit/session-end.ndjson` со списком сессий.

### Interactive builtins (2, Phase 4.3)

#### 5.6. confirm_dangerous (default: ON, Elicitation event)
Когда `requires_confirmation=True`, публикует вопрос в `ElicitationBroker` и ждёт ответ (timeout = `hooks_elicitation_ws_timeout_s`, default 30s). Возвращает `modify` с `answer=default_answer` (по умолчанию `"abort"`, safe fallback).

**3 пути разрешения** (`payload["answer_source"]`):
- `ws_human` — WebSocket/SSE/long-poll клиент ответил вовремя.
- `default_timeout` — клиент не ответил, использован default.
- `default_ws_disabled` — `hooks_elicitation_ws_enabled=False`.

#### 5.7. notify_terminal (default: ON, Notification event)
Диспетчер уведомлений. Итерирует `payload["channels"]` (default `["stdout"]`) и вызывает handler per-channel:

| Channel | Реализация |
|---------|-----------|
| `stdout` (default) | `[severity] message` в stderr |
| `webhook` (Phase 4.3+) | HTTP POST + HMAC-SHA256 (опционально) |
| `desktop` (Phase 4.3+, opt-in) | Windows `msg *`, macOS `osascript`, Linux `notify-send` |
| `slack` (Phase 4.6) | Slack Incoming Webhook (severity → color) |
| `teams` (Phase 4.6) | MS Teams MessageCard (severity → themeColor) |

**Retry + DLQ** (Phase 4.8): transient errors (5xx, timeout) → exponential backoff (`hooks_notify_max_retries=3`, `hooks_notify_retry_initial_delay_ms=100`, max=5000ms). Permanent errors (4xx) → DLQ immediately. DLQ entries в `data/audit/agent-jobs.db`, table `notify_dlq`. Per-channel isolation через `asyncio.gather(return_exceptions=True)`.

**DLQ metric:** `notify_dlq_total{severity, channel, terminal}` — emit'ится ВСЕГДА (даже при `dlq_enabled=False`).

### Pattern library builtins (5, Phase 4.10)

Готовые паттерны — JSON specs в `.harness/hooks/*.json` + Python реализации. See [§11](#11-hook-pattern-library-phase-410).

| Pattern | Event | Transport | Что делает |
|---------|-------|-----------|------------|
| `auto_format` | PostToolUse | subprocess | `ruff format` после write/edit на `*.py` |
| `license_check` | PreToolUse | builtin | Block GPL-3.0/AGPL-3.0/SSPL imports |
| `complexity_check` | PostToolUse | builtin | Warn если cyclomatic complexity > 10 (AST) |
| `secret_detect` | PreToolUse | builtin | Block AWS/GitHub/OpenAI/PEM/JWT/password в args |
| `sql_injection_guard` | PreToolUse | builtin | Block f-string/concat/format SQL queries |
| `unsafe_import_block` | PreToolUse | builtin | Block `os.system`, `pickle`, `eval`, `yaml.load` без SafeLoader |
| `test_required` | PreToolUse | builtin | Block `git commit` с `*.py` changes без `pytest` |
| `docs_required` | PostToolUse | builtin | Warn на public funcs без docstring |

> **Note:** `BUILTIN_HOOKS` registry (5 framework + 2 interactive + 5 pattern = **12**) обновлён в Phase 4.10. Тест `test_total_builtin_count` проверяет registry size.

---

## 6. Регистрация и настройка

### 6.1. Программная регистрация

```python
from harness.hooks import (
    EventType, HookRegistry, HookRunner, HookSpec,
)

registry = HookRegistry()

# Встроенный hook (Python callable)
async def my_hook(ctx): return HookDecision(decision="allow", hook_id="h1")
await registry.register(HookSpec(
    hook_id="my.hook",
    event=EventType.PRE_TOOL_USE,
    transport="builtin",
    callable=my_hook,
))

# Subprocess
await registry.register(HookSpec(
    hook_id="subprocess.blocker",
    event=EventType.PRE_TOOL_USE,
    transport="subprocess",
    script_path="/abs/path/hook.py",
    timeout_ms=1000,
))

# HTTP
await registry.register(HookSpec(
    hook_id="http.remote",
    event=EventType.PRE_TOOL_USE,
    transport="http",
    url="https://example.com/h",
    headers={"Authorization": "Bearer xyz"},
))

# LLM
await registry.register(HookSpec(
    hook_id="llm.classifier",
    event=EventType.PRE_TOOL_USE,
    transport="llm",
    model="qwen3:8b",
    prompt="Safe? Args: {payload.arguments}",
    timeout_ms=3000,
))

runner = HookRunner(registry, llm_router=router, audit_sink=sink)
```

### 6.2. Settings-строковый формат

Для регистрации через `settings.hooks_*_specs` (comma-separated):

```python
# harness/config.py
hooks_subprocess_specs: str = ""  # "PreToolUse:subprocess:/abs/hook.py:1000,OnRoutingDecision:subprocess:/abs/audit.py:2000"
hooks_http_specs: str = ""  # "PreToolUse:http:https://example.com/h:2000:Bearer xyz"
hooks_llm_specs: str = ""  # "OnRoutingDecision:llm:qwen3:8b:3000:Decide whether to override"
```

**Парсинг:** `parse_spec(spec_string) → HookSpec` (см. `harness/hooks/registry.py`).

**Примеры:**

| Строка | Результат |
|--------|-----------|
| `PreToolUse:builtin:log` | builtin hook named "log" |
| `PreToolUse:subprocess:/abs/hook.py:1000` | subprocess с timeout=1000ms |
| `PreToolUse:http:https://ex.com/h:2000` | HTTP с timeout=2000, без auth |
| `PreToolUse:http:https://ex.com/h:Bearer abc` | HTTP без timeout, Authorization: Bearer abc |
| `OnRoutingDecision:llm:qwen3:3000:Is it safe?` | LLM с model=qwen3, timeout=3000ms, prompt=... (model не должен содержать `:`) |

---

## 7. Конфигурация

Все в `harness/config.py`, секция "Hooks framework". **Master switch:** `hooks_enabled: bool = True` — False = вся framework отключён. Полный набор settings разбит по подгруппам:

### Framework (13 core)
`hooks_enabled`, `hooks_default_max_ms=3000`, `hooks_max_per_event=10`, `hooks_max_recursion_depth=3`, `hooks_subprocess_specs=""`, `hooks_http_specs=""`, `hooks_llm_specs=""`, `hooks_filter_chain=""`, `hooks_fail_open=True`, `hooks_redact_payloads=True`, `hooks_audit_log=False`, `hooks_subprocess_allowed_paths=".harness/hooks/**"`, `hooks_on_memory_write_silent_layers="L1"`, `hooks_on_compaction_skip_cache_hit=True`.

### Per-event enable (16, default True)
`hooks_on_pre_tool_use_enabled`, …, `hooks_on_elicitation_enabled`, `hooks_on_notification_enabled`.

### Builtin enable (12)
`hooks_builtin_log_enabled=True`, `hooks_builtin_validate_enabled=True`, `hooks_builtin_block_dangerous_enabled=True`, `hooks_builtin_inject_context_enabled=False` (opt-in), `hooks_builtin_autosave_enabled=True`, `hooks_builtin_confirm_dangerous_enabled=True`, `hooks_builtin_notify_terminal_enabled=True`, + 5 pattern builtins (license/complexity/secret/sql_injection/unsafe_import).

### Hot-reload (3, Phase 4.2)
`hot_reload_enabled=True`, `hot_reload_debounce_ms=200`, `hot_reload_poll_interval_s=1.0`.

### Elicitation (Phase 4.3, 4.5, 4.11)
`hooks_elicitation_ws_enabled=True`, `hooks_elicitation_ws_timeout_s=30.0`, `hooks_elicitation_longpoll_enabled=False` (opt-in), `hooks_elicitation_longpoll_timeout_s=30.0`, `hooks_elicitation_longpoll_poll_interval_s=0.25`, `hooks_elicitation_longpoll_interval_s=0.25`, `hooks_elicitation_sse_enabled=False` (opt-in), `hooks_elicitation_sse_heartbeat_s=15`, `hooks_elicitation_sse_max_session_age_s=3600`.

### Notification channels (Phase 4.3, 4.6)
`hooks_notify_webhook_url=""`, `hooks_notify_webhook_secret=""`, `hooks_notify_webhook_timeout_s=5.0`, `hooks_notify_desktop_enabled=False`, `hooks_notify_slack_webhook_url=""`, `hooks_notify_slack_channel=""`, `hooks_notify_slack_username="Solomon Harness"`, `hooks_notify_teams_webhook_url=""` (+ 2 reserved).

### Notify retry + DLQ (Phase 4.8)
`hooks_notify_max_retries=3`, `hooks_notify_retry_initial_delay_ms=100`, `hooks_notify_retry_max_delay_ms=5000`, `hooks_notify_dlq_enabled=True`.

### Defensive layer (Phase 4.8)
`hooks_rate_limit_capacity=60`, `hooks_rate_limit_refill_per_sec=1.0`, `hooks_rate_limit_enabled=True`, `hooks_circuit_breaker_threshold=5`, `hooks_circuit_breaker_cooldown_s=60.0`, `hooks_circuit_breaker_enabled=True`.

### Admin endpoints (Phase 4.11)
`hooks_observability_admin_enabled=True`, `hooks_observability_admin_audit_max_limit=500`, `hooks_observability_admin_metrics_filter=""`.

### Pattern thresholds (Phase 4.10)
`hooks_license_check_forbidden="GPL-3.0,AGPL-3.0,SSPL"`, `hooks_complexity_threshold=10`, `hooks_unsafe_imports_blocklist=...`, `hooks_test_required_pattern="*.py"`.

**Backward compat:** все новые kwargs в ToolRuntime / AgentLoop / app.lifespan default `None` или `""` — старый код работает.

---

## 8. Audit log (NDJSON)

При `settings.hooks_audit_log=True` runner пишет каждое решение в `<project_root>/data/audit/hooks-YYYY-MM-DD.ndjson` (ротация по дням).

**Формат строки:**

```json
{
  "ts": "2026-06-16T12:34:56.789+00:00",
  "event": "PreToolUse",
  "session_id": "abc-123",
  "agent_id": "my-agent",
  "request_id": "f4e8a1b2c3d4",
  "aggregate": {
    "final_decision": "block",
    "blocked_by": "builtin.rm-guard",
    "final_payload": {},
    "decisions": [
      {
        "decision": "allow",
        "hook_id": "builtin.log",
        "duration_ms": 0.5,
        "output": {},
        "error": ""
      },
      {
        "decision": "block",
        "hook_id": "builtin.rm-guard",
        "duration_ms": 0.2,
        "output": {"reason": "destructive command"},
        "error": ""
      }
    ]
  }
}
```

**Чтение tail:**

```python
from harness.hooks.audit import HookAuditSink
sink = HookAuditSink(Path("data/audit"))
recent = sink.tail(n=20)
```

**Privacy:** `hooks_redact_payloads=True` (default) → PII (email, phone, IPv4, GitHub tokens, AWS keys, etc.) заменяется на `<EMAIL>`, `<PHONE>`, `<IPV4>`, `<GITHUB_TOKEN>`, `<AWS_KEY>`. Полный список 12 паттернов — `harness/privacy/redaction.py` (Phase 3 v1.0.0).

**Thread-safe:** `threading.Lock` на file handle. Crash-safe: каждая строка — отдельный open/write/close (нет half-line state в kernel buffer).

---

## 9. Hot-reload (Phase 4.2)

**FileWatcher primitive** (`harness/watcher.py`) — watchfiles (Rust-backed) с polling fallback (POSIX + Windows). 3 ресурса hot-reloadable:

| Ресурс | Директория | Что происходит при изменении |
|--------|-----------|------------------------------|
| Agent specs | `.harness/agents/*.md` + `harness/agents/builtin/*.md` (Phase 4.2+ v1.9.0) | Re-parse, следующий `all_specs()` подхватит |
| Hook specs | `.harness/hooks/*.json` (single object или list) | Re-parse → `registry.register(spec)` |
| Privacy zones | `.harness/privacy/*.json` (Phase 4.2+ v1.8.1) | `PrivacyZoneFilter.set_rules(new_rules)` (atomic swap) |

**Гарантии:**
- **Best-effort:** init failure → log + continue (app работает без hot-reload).
- **Fail-open:** malformed file → log warning, last good spec stays.
- **Debounce:** 200ms окно (default, `hot_reload_debounce_ms`) — editor save events coalesce.
- **Singleton:** `get_file_watcher()` — один watcher на процесс.
- **AST-enforced trust boundary:** `watcher.py` НЕ импортирует agents/hooks/server/observability.

**Force-reload без file event:** `harness reload [kind]` CLI (Phase 4.2+ v1.9.0). Kinds: `all` (default), `agents`, `hooks`, `privacy`.

---

## 10. Defensive layer: rate limiter + circuit breaker (Phase 4.8)

`harness/hooks/rate_limit.py` (~280 LoC). Wire в `HookRunner._dispatch_one` — **до** вызова hook body.

### Token bucket (rate limiter)
- Capacity + refill_per_sec (`hooks_rate_limit_capacity=60`, `hooks_rate_limit_refill_per_sec=1.0`).
- `consume(n) → bool` — атомарный drain.
- Per-hook_id, thread-safe (`threading.Lock`).
- Metric: `hook_rate_limited_total{hook_id}`.

### Circuit breaker
- States: `closed` → `open` (threshold failures: `hooks_circuit_breaker_threshold=5`) → `half_open` (cooldown: `hooks_circuit_breaker_cooldown_s=60.0`) → `closed` (probe success) или `open` (probe failure).
- Half-open probe через sentinel (предотвращает race conditions).
- Metric: `hook_circuit_skip_total{hook_id, state}`.

**Композиция:** rate limit → circuit breaker → hook body. Skip возвращает `allow+error` marker (НЕ блокирует остальные hooks).

---

## 11. Hook pattern library (Phase 4.10)

8 готовых JSON specs в `.harness/hooks/*.json` + Python реализации. See [§5](#5-12-встроенных-хуков-builtin) (pattern builtins 5.8–5.12).

**Совместимость с hot-reload:** JSON specs подхватываются FileWatcher автоматически. Изменение в файле → следующий hook dispatch использует новую spec.

**Configuration thresholds (4 settings):**
- `hooks_license_check_forbidden` — list forbidden licenses (default: GPL-3.0, AGPL-3.0, SSPL).
- `hooks_complexity_threshold` — cyclomatic complexity threshold (default: 10).
- `hooks_unsafe_imports_blocklist` — list dangerous imports.
- `hooks_test_required_pattern` — git diff pattern (default: `*.py`).

**Why JSON specs vs Settings strings:** Hot-reload работает с файлами. Settings strings в env vars требуют restart процесса.

---

## 12. Примеры

### 12.1. Минимальный (log + block_dangerous)

```python
from harness.config import Settings
from harness.hooks import HookRegistry, HookRunner, EventType
from harness.hooks.builtin import log_hook, block_dangerous_hook

settings = Settings()
registry = HookRegistry()
await registry.register(HookSpec(
    hook_id="builtin.log", event=EventType.PRE_TOOL_USE,
    transport="builtin", callable=log_hook,
))
await registry.register(HookSpec(
    hook_id="builtin.block_dangerous", event=EventType.PRE_TOOL_USE,
    transport="builtin", callable=block_dangerous_hook,
))
runner = HookRunner(registry)
```

### 12.2. Subprocess (allow / block через exit 0 / 2)

```python
# File: /abs/hooks/audit_tool_use.py
import json, sys
ctx = json.load(sys.stdin)
with open("/var/log/audit.log", "a") as f:
    f.write(f"{ctx['event']}: {ctx['payload']}\n")
sys.exit(0)  # allow
```

```python
spec = HookSpec(
    hook_id="subprocess.audit",
    event=EventType.POST_TOOL_USE,
    transport="subprocess",
    script_path="/abs/hooks/audit_tool_use.py",
    timeout_ms=2000,
)
```

### 12.3. HTTP (внешний API для policy enforcement)

```python
# Сервер: app.py → POST /enforce
from fastapi import FastAPI, Request
app = FastAPI()

@app.post("/enforce")
async def enforce(request: Request):
    ctx = await request.json()
    # ... вызов policy engine ...
    return {"decision": "allow", "hook_id": "http.policy"}
```

```python
spec = HookSpec(
    hook_id="http.policy",
    event=EventType.PRE_TOOL_USE,
    transport="http",
    url="https://policy.example.com/enforce",
    headers={"Authorization": "Bearer my-token"},
    timeout_ms=1000,
)
```

### 12.4. LLM-as-hook (catch-all safety classifier)

```python
from harness.hooks.llm_hook import LLMHook

hook = LLMHook(
    router=router,  # DI: harness.server.llm.router.LLMRouter
    model="qwen3:8b",
    prompt="""Tool call: {event}, Args: {payload.arguments}.
Is this safe? Respond JSON: {{"decision": "allow"|"block", "reason": "..."}}""",
    timeout_ms=3000,
)
```

### 12.5. Modify (privacy filter, redaction в payload)

```python
async def redact_pii(ctx: HookContext) -> HookDecision:
    import re
    text = str(ctx.payload.get("arguments", {}).get("text", ""))
    redacted = re.sub(r"\b\d{16}\b", "<CARD>", text)
    if redacted == text:
        return HookDecision(decision="allow", hook_id="builtin.redact-pii")
    new_args = dict(ctx.payload["arguments"])
    new_args["text"] = redacted
    return HookDecision(
        decision="modify", hook_id="builtin.redact-pii",
        output={"payload": {"tool_name": ctx.payload["tool_name"],
                            "arguments": new_args}},
    )
```

---

## 13. Troubleshooting

### 13.1. Hook не срабатывает

- Проверьте `settings.hooks_enabled: True`.
- Проверьте per-event флаг: `hooks_on_pre_tool_use_enabled: True` (default).
- Проверьте `filter_chain` / `matcher` в HookSpec — может фильтровать.
- Проверьте `hooks_max_per_event` — если зарегистрировано >10 hook'ов, лишние дропаются с warning.

### 13.2. Subprocess hook крашится с "file not found"

- Используйте **абсолютный путь** (`script_path="/abs/path/hook.py"`).
- На Windows: `C:/abs/path/hook.py` (forward slashes) или `C:\\abs\\path\\hook.py`.
- Проверьте `settings.hooks_subprocess_allowed_paths` — путь должен матчиться.

### 13.3. HTTP hook всегда allow (fail-open)

- Проверьте URL доступен (curl / ping).
- Проверьте 2xx ответ содержит JSON с полем `decision`.
- 4xx / 5xx / timeout / network error = fail-open by design (см. §4.3).
- Включите `settings.hooks_audit_log: True` и смотрите `aggregate.decisions[].error`.

### 13.4. LLM hook "llm_router is None"

- Конструктор `HookRunner(llm_router=<router>)` обязателен для LLM transport.
- Runner **не** импортирует `harness.server.llm.router` напрямую (trust boundary) — DI обязателен.

### 13.5. Audit log не пишется

- `settings.hooks_audit_log: True` (default False).
- Проверьте права на запись в `<project_root>/data/audit/`.
- Файл ротируется по дням: `hooks-2026-06-15.ndjson`, `hooks-2026-06-16.ndjson`.

### 13.6. Trust boundary violation в тестах

`test_hooks_trust_boundary.py` (4 проверки) валит CI если `harness.hooks/*` начнёт импортить `harness.agents` или `harness.server`. Это **by design** — hooks framework не должен зависеть от production кода (zero coupling).

---

## 14. См. также

- [`docs/PHASE4-HOOKS-PLAN.md`](PHASE4-HOOKS-PLAN.md) — подробный план Phase 4.0 (maintainer reference)
- [`docs/CHANGELOG.md`](CHANGELOG.md) — v1.6.0 → v1.22.0 history (Phase 4.0 → 4.12)
- [`docs/observability.md`](observability.md) — hooks metrics (`hook_dispatches_total`, `hook_duration_seconds`, `hook_rate_limited_total`, `hook_circuit_skip_total`)
- [`docs/elicitation.md`](elicitation.md) — Elicitation event (3 транспорта: WS/long-poll/SSE)
- [`docs/webhooks.md`](webhooks.md) — Outbound webhook fanout (Notification event)
- [`docs/scope-api.md`](scope-api.md) — 10 RBAC scopes (`elicitation.read`, `observability.read`, `webhooks.admin`)
- [`docs/cli.md`](cli.md) — `harness hooks` subcommand reference
- [`docs/roadmap.md`](roadmap.md) — Phase 4 статус (10/12 step)
- `harness/hooks/` — исходный код (16 events, 12 builtins, 4 transports)
- `tests/test_hooks_*` / `tests/test_hook_*` — 40+ test files, ~600 tests
- [Anthropic context engineering — Hooks](https://docs.anthropic.com/en/docs/agents-and-tools/claude-code/hooks) — upstream reference

---

**Версия документа:** v1.22.0 (2026-06-19)
**Phase:** 4.0–4.12 — Hooks framework (12/12 step done, Phase 4.13 webhook hardening separately documented)
