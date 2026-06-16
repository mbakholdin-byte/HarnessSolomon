# Hooks Framework — Solomon Harness v1.6.0

> **Phase 4.0 — Hooks framework** для production-пайплайна. Позволяет встраивать side-effects (логирование, валидация, блокировки, аудит) в ключевые точки жизненного цикла агента через декларативные **hook specs** с 4 транспортами: **builtin / subprocess / http / llm**.

---

## Содержание

1. [Что такое hooks](#1-что-такое-hooks)
2. [14 событий (EventType)](#2-14-событий-eventtype)
3. [Решения (Decision)](#3-решения-decision)
4. [4 транспорта](#4-4-транспорта)
5. [5 встроенных хуков (builtin)](#5-5-встроенных-хуков-builtin)
6. [Регистрация и настройка](#6-регистрация-и-настройка)
7. [Конфигурация (31 settings)](#7-конфигурация-31-settings)
8. [Audit log (NDJSON)](#8-audit-log-ndjson)
9. [Примеры (built-in / subprocess / http / llm)](#9-примеры)
10. [Troubleshooting](#10-troubleshooting)
11. [См. также](#11-см-также)

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

## 2. 14 событий (EventType)

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
| `PermissionRequest` | Tool запрещён denylist'ом | `{"tool_name": str, "arguments": dict, "reason": str}` | `modify` → allow (override) |
| `OnMemoryWrite` | Внутри `UnifiedMemory.write` (post-redact, pre-persist) | `{"layer": "L1"/"L2", "key": str, "value_preview": str}` | `block` → отменить запись |
| `OnRoutingDecision` | После `LLMRouterClassifier.classify` | `{"tier": "T1"/"T2"/"T3", "model": str, "confidence": float}` | `modify` → override tier/model |
| `OnCompaction` | После `ContextCompactor` (cache-miss only, если opt-in) | `{"session_id": str, "summary_preview": str, "saved_tokens": int}` | allow (post-process) |

> **Elicitation / Notification** — DEFERRED to Phase 4.4.

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

## 5. 5 встроенных хуков (builtin)

5 хуков идут в коробке и подключаются по `settings.hooks_builtin_<name>_enabled: bool`.

### 5.1. log (default: ON)

Логирует каждое событие через stdlib `logging` на INFO. Не меняет решение.

```python
# harness/hooks/builtin/log.py
async def log_hook(ctx: HookContext) -> HookDecision:
    logger.info("hook %s for %s in session %s",
                ctx.event, ctx.agent_id, ctx.session_id)
    return HookDecision(decision="allow", hook_id="builtin.log")
```

### 5.2. validate (default: ON)

Валидирует payload против Pydantic-схемы (если есть). `block` при невалидном.

```python
# Схемы переопределяются через _SCHEMAS_OVERRIDE dict в тестах
async def validate_hook(ctx: HookContext) -> HookDecision:
    schema = _SCHEMAS.get(ctx.event)
    if schema is None:
        return HookDecision(decision="allow", hook_id="builtin.validate")
    try:
        schema(**ctx.payload)
    except ValidationError as e:
        return HookDecision(
            decision="block", hook_id="builtin.validate",
            output={"reason": str(e)},
        )
    return HookDecision(decision="allow", hook_id="builtin.validate")
```

### 5.3. block_dangerous (default: ON)

Regex-блокировка деструктивных команд в `PreToolUse` для `bash` tool.

**7 паттернов:**

1. `rm -r[f] /<path>` — рекурсивное удаление в root
2. `mkfs /dev/...` — форматирование диска
3. `dd of=/dev/...` — запись в raw device
4. `:(){ :|:& };:` — fork bomb
5. `DROP DATABASE` — удаление БД
6. `TRUNCATE TABLE` — очистка таблицы
7. `format c:` (Windows) — форматирование системного диска

**Настройка:** `settings.hooks_block_dangerous_patterns: str` (comma-separated regex), default = встроенные 7.

### 5.4. inject_context (default: OFF)

Инжектит L0 (scratchpad) в system prompt при `InstructionsLoaded`. Использует Phase 3 v1.2.0 `ScratchpadStore`.

```python
async def inject_context_hook(ctx: HookContext) -> HookDecision:
    if ctx.event != "InstructionsLoaded":
        return HookDecision(decision="allow", hook_id="builtin.inject_context")
    spec_name = ctx.payload.get("spec_name", "")
    notes = scratchpad.read_l0(spec_name=spec_name, session_id=ctx.session_id)
    if not notes:
        return HookDecision(decision="allow", hook_id="builtin.inject_context")
    return HookDecision(
        decision="modify", hook_id="builtin.inject_context",
        output={"payload": {
            "spec_name": spec_name,
            "file_path": ctx.payload["file_path"],
            "l0_injection": "\n".join(notes),
        }},
    )
```

**Включение:** `settings.hooks_builtin_inject_context_enabled: bool` (default False — opt-in, может быть шумно).

### 5.5. autosave (default: ON)

На `SessionEnd` пишет `data/audit/session-end.ndjson` со списком сессий.

**Формат строки:**

```json
{"ts": "2026-06-16T12:34:56Z", "session_id": "...", "duration_seconds": 1234.5}
```

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

## 7. Конфигурация (31 settings)

Все в `harness/config.py`, секция "Hooks framework". **Master switch:** `hooks_enabled: bool = True` — False = вся framework отключён.

### Framework (13)

| Setting | Type | Default | Описание |
|---------|------|---------|----------|
| `hooks_enabled` | bool | True | Master switch |
| `hooks_default_max_ms` | int | 3000 | Default per-hook timeout |
| `hooks_max_per_event` | int | 10 | Max hook'ов на одно событие |
| `hooks_max_recursion_depth` | int | 3 | Recursion guard depth |
| `hooks_subprocess_specs` | str | "" | Comma-separated subprocess specs |
| `hooks_http_specs` | str | "" | Comma-separated HTTP specs |
| `hooks_llm_specs` | str | "" | Comma-separated LLM specs |
| `hooks_filter_chain` | str | "" | Global filter (fnmatch syntax) |
| `hooks_fail_open` | bool | True | Если True — ошибка в hook → allow |
| `hooks_redact_payloads` | bool | True | Redact PII в audit log |
| `hooks_audit_log` | bool | False | Включить NDJSON audit sink |
| `hooks_subprocess_allowed_paths` | str | ".harness/hooks/**" | Glob для разрешённых путей |
| `hooks_on_memory_write_silent_layers` | str | "L1" | Слои, для которых OnMemoryWrite НЕ срабатывает |
| `hooks_on_compaction_skip_cache_hit` | bool | True | OnCompaction fires только на cache miss |

### Per-event enable (14)

`hooks_on_pre_tool_use_enabled`, `hooks_on_post_tool_use_enabled`, `hooks_on_stop_enabled`, `hooks_on_subagent_start_enabled`, `hooks_on_subagent_stop_enabled`, `hooks_on_session_start_enabled`, `hooks_on_session_end_enabled`, `hooks_on_user_prompt_submit_enabled`, `hooks_on_pre_compact_enabled`, `hooks_on_instructions_loaded_enabled`, `hooks_on_permission_request_enabled`, `hooks_on_memory_write_enabled`, `hooks_on_routing_decision_enabled`, `hooks_on_compaction_enabled`. **Все default True.**

### Builtin enable (5)

`hooks_builtin_log_enabled`, `hooks_builtin_validate_enabled`, `hooks_builtin_block_dangerous_enabled` (default True), `hooks_builtin_inject_context_enabled` (default False — opt-in), `hooks_builtin_autosave_enabled`.

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

## 9. Примеры

### 9.1. Минимальный (log + block_dangerous)

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

### 9.2. Subprocess (allow / block через exit 0 / 2)

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

### 9.3. HTTP (внешний API для policy enforcement)

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

### 9.4. LLM-as-hook (catch-all safety classifier)

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

### 9.5. Modify (privacy filter, redaction в payload)

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

## 10. Troubleshooting

### 10.1. Hook не срабатывает

- Проверьте `settings.hooks_enabled: True`.
- Проверьте per-event флаг: `hooks_on_pre_tool_use_enabled: True` (default).
- Проверьте `filter_chain` / `matcher` в HookSpec — может фильтровать.
- Проверьте `hooks_max_per_event` — если зарегистрировано >10 hook'ов, лишние дропаются с warning.

### 10.2. Subprocess hook крашится с "file not found"

- Используйте **абсолютный путь** (`script_path="/abs/path/hook.py"`).
- На Windows: `C:/abs/path/hook.py` (forward slashes) или `C:\\abs\\path\\hook.py`.
- Проверьте `settings.hooks_subprocess_allowed_paths` — путь должен матчиться.

### 10.3. HTTP hook всегда allow (fail-open)

- Проверьте URL доступен (curl / ping).
- Проверьте 2xx ответ содержит JSON с полем `decision`.
- 4xx / 5xx / timeout / network error = fail-open by design (см. §4.3).
- Включите `settings.hooks_audit_log: True` и смотрите `aggregate.decisions[].error`.

### 10.4. LLM hook "llm_router is None"

- Конструктор `HookRunner(llm_router=<router>)` обязателен для LLM transport.
- Runner **не** импортирует `harness.server.llm.router` напрямую (trust boundary) — DI обязателен.

### 10.5. Audit log не пишется

- `settings.hooks_audit_log: True` (default False).
- Проверьте права на запись в `<project_root>/data/audit/`.
- Файл ротируется по дням: `hooks-2026-06-15.ndjson`, `hooks-2026-06-16.ndjson`.

### 10.6. Trust boundary violation в тестах

`test_hooks_trust_boundary.py` (4 проверки) валит CI если `harness.hooks/*` начнёт импортить `harness.agents` или `harness.server`. Это **by design** — hooks framework не должен зависеть от production кода (zero coupling).

---

## 11. См. также

- `docs/PHASE4-HOOKS-PLAN.md` — подробный план Phase 4.0 (1082 строки, для maintainer'ов)
- `docs/CHANGELOG.md` — v1.6.0 entry (что добавлено)
- `docs/roadmap.md` — Phase 4 статус
- `harness/hooks/` — исходный код
- `tests/test_hooks_*` — 23 test file, ~276 tests
- [Anthropic context engineering — Hooks](https://docs.anthropic.com/en/docs/agents-and-tools/claude-code/hooks) — upstream reference

---

**Версия документа:** v1.6.0 (2026-06-16)
**Phase:** 4.0 — Hooks framework (ЗАКРЫТО)
