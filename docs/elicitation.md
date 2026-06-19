# Elicitation — Solomon Harness v1.0.0+

> Last updated: 2026-06-19, v1.0.0 final. RBAC: WS требует scope `elicitation.write`, long-poll требует `elicitation.read`, SSE требует `elicitation.read`.

> **Phase 4.3–4.11** — интерактивный запрос к человеку. Hook event `Elicitation` публикует вопрос в `ElicitationBroker`, который ждёт ответ от подключённого клиента (или fallback на default_answer после timeout).
>
> 3 транспорта: **WebSocket** (primary), **HTTP long-poll** (fallback для corporate firewalls), **Server-Sent Events** (Phase 4.11, для dashboards).

## Контекст

**Elicitation event** (Phase 4.3 v1.10) — один из 16 hook events. В отличие от большинства events (которые fire-and-forget), Elicitation **блокирует** выполнение agent loop до разрешения вопроса. Типичные use cases:

- Confirm destructive action (`rm -rf`, `git push --force`, etc.) — `requires_confirmation=True`
- Prompt user для input ("Which file do you mean?")
- Choose between options ("Apply patch A, B, or C?")

## Hook contract

`confirm_dangerous_hook` (builtin, `Elicitation` event) — единственный builtin для Elicitation. Поведение:

1. Если `payload["requires_confirmation"] == True` → публикует question в `ElicitationBroker`.
2. Broker возвращает `question_id`, создаёт `asyncio.Future`.
3. Hook ждёт future (timeout = `hooks_elicitation_ws_timeout_s`, default 30s).
4. **3 пути разрешения** (записываются в `payload["answer_source"]`):
   - `ws_human` — клиент ответил через любой транспорт до timeout.
   - `default_timeout` — timeout истёк, использован `default_answer` (по умолчанию `"abort"`, safe fallback).
   - `default_ws_disabled` — все transports disabled, использован `default_answer` немедленно.
5. Hook всегда возвращает `modify` (never `block` — agent loop остаётся жив).

## 3 транспорта

### 1. WebSocket (primary, Phase 4.3+ v1.12)

**Endpoint:** `WS /api/v1/elicitation/ws`

**Default:** ON (`hooks_elicitation_ws_enabled=True`).

**Protocol:**
- Server → client (push): `{action: "question", question_id, question, options, default_answer}` (diff-based, poll 500ms)
- Client → server: `{action: "answer", question_id, value}`
- Client → server: `{action: "list"}` → snapshot pending
- Client → server: `{action: "ping"}` → `{action: "pong", stats}`
- On connect: `{action: "connected"}` hello

**Disabled:** если `hooks_elicitation_ws_enabled=False`, server закрывает connection с code 1008 (policy violation).

### 2. HTTP long-poll (fallback, Phase 4.5 v1.15)

**Endpoints:**
- `GET /api/v1/elicitation/poll?session=S` — long-poll (max `hooks_elicitation_longpoll_timeout_s=30s`, poll interval `hooks_elicitation_longpoll_interval_s=0.25s`)
- `POST /api/v1/elicitation/answer` — submit answer

**Default:** OFF (`hooks_elicitation_longpoll_enabled=False`). Opt-in через env var.

**Status codes:**
- `200` + question JSON — pending question found
- `403` — long-poll disabled
- `404` `no_pending_question` — timeout, no question arrived (caller retries)

**POST body:**
```json
{
  "session_id": "abc-123",      // optional, informational
  "question_id": "uuid-hex",     // required
  "answer": "yes"                // required, "" allowed
}
```

**Response:** `{"accepted": true, "question_id": "...", "session_id": "..."}` или `404 unknown_or_resolved_question`.

### 3. Server-Sent Events (Phase 4.11 v1.21)

**Endpoint:** `GET /api/v1/elicitation/sse?session=S`

**Default:** OFF (`hooks_elicitation_sse_enabled=False`). Opt-in.

**Scope:** `elicitation.read` (первый transport с RBAC enforcement — WS и long-poll не имеют scope check).

**Wire format** (one blank-line-separated block per event):

```
event: new_question
data: {"question_id": "...", "question": "...", "options": [...],
       "default_answer": "...", "session_id": "...", "created_at": 0.0}

event: answered
data: {"question_id": "...", "answer": "...", "session_id": "..."}

event: timeout
data: {"question_id": "...", "default_answer": "...", "session_id": "..."}

: keep-alive
```

**Behaviour:**
- `StreamingResponse` с `media_type="text/event-stream"`.
- Polls `broker.pending()` каждые 250ms.
- Seen-questions dedup (per-stream set).
- Heartbeat comment `: keep-alive` каждые `hooks_elicitation_sse_heartbeat_s=15s` (anti-proxy-timeout).
- Client disconnect detection (`await request.is_disconnected()`).
- Max session age auto-disconnect (`hooks_elicitation_sse_max_session_age_s=3600s`).
- Headers: `Cache-Control: no-cache`, `X-Accel-Buffering: no` (nginx), `Connection: keep-alive`.

**Why SSE как 3rd transport (НЕ replacement WS):** WebSocket — primary, full-duplex. SSE — server-push only через HTTP/1.1 streaming. Корпоративные networks с proxy/firewall часто блокируют WS upgrade, но пропускают HTTP streaming. SSE = fallback без дополнительных ports/protocols.

## Decision history (Phase 4.8 v1.18)

**Endpoint:** `GET /api/v1/elicitation/history?session=S&limit=N`

SQLite table `elicitation_decisions` в `data/audit/agent-jobs.db`. 12 колонок: `decision_id`, `session_id`, `request_id`, `question_id`, `question_preview` (200 chars PII-safe), `options_json`, `default_answer`, `decision` (pending/answered/timed_out), `answer`, `source` (ws/poll/timeout), `latency_ms`, `ts`.

CLI: `harness elicitation history [--session S] [--limit N] [--json] [--project-root P]`.

**Wire response** (array of dicts):
```json
[
  {
    "decision_id": "uuid",
    "session_id": "abc-123",
    "question_id": "uuid",
    "question_preview": "Delete file X?",
    "options": ["yes", "no"],
    "default_answer": "abort",
    "decision": "answered",
    "answer": "no",
    "source": "ws",
    "latency_ms": 1234.5,
    "ts": 1718800000.0
  }
]
```

## Configuration

| Setting | Default | Описание |
|---------|---------|----------|
| `hooks_elicitation_enabled` | True | Master switch для Elicitation event |
| `hooks_elicitation_ws_enabled` | True | WebSocket transport (primary) |
| `hooks_elicitation_ws_timeout_s` | 30.0 | Wait timeout для human answer |
| `hooks_elicitation_longpoll_enabled` | False | HTTP long-poll fallback (opt-in) |
| `hooks_elicitation_longpoll_timeout_s` | 30.0 | Long-poll max wait |
| `hooks_elicitation_longpoll_interval_s` | 0.25 | Poll cadence |
| `hooks_elicitation_sse_enabled` | False | SSE transport (opt-in) |
| `hooks_elicitation_sse_heartbeat_s` | 15 | SSE keep-alive comment interval |
| `hooks_elicitation_sse_max_session_age_s` | 3600 | SSE auto-disconnect after N seconds |
| `hooks_builtin_confirm_dangerous_enabled` | True | Builtin hook для Elicitation |

## Observability

- Metric: `elicitation_total{decision}` (Counter, Phase 4.3)
- Emit helper: `emit_elicitation_response(decision, ...)`
- JSONL log: event `"elicitation"` с truncated question (PII safety)
- Decision store (SQLite) — best-effort, errors logged, broker продолжает работать

## Architecture

`ElicitationBroker` (`harness/elicitation.py`, ~175 LoC, stdlib + asyncio only):
- In-memory pub/sub. `publish(question, options, default, timeout_s) → question_id`. `wait(question_id) → blocks`. `answer(question_id, value) → bool`.
- Lazy future creation (per-loop, no global event-loop dependency).
- Stats counters: `published_total`, `answered_total`, `timed_out_total`, `pending_count`.
- Process-level singleton: `ElicitationBroker.get()`. `reset()` для tests.

**Trust boundary:** `harness/elicitation.py` НЕ импортирует `harness.agents`/`harness.server`/`harness.hooks` (AST-enforced). Routes импортируют broker lazily внутри handler.

## Examples

### WebSocket client (Python)

```python
import asyncio
import json
import websockets

async def main():
    async with websockets.connect(
        "ws://localhost:8765/api/v1/elicitation/ws",
    ) as ws:
        # Hello
        hello = json.loads(await ws.recv())
        assert hello["action"] == "connected"

        # Wait for question
        while True:
            msg = json.loads(await ws.recv())
            if msg["action"] == "question":
                print(f"Q: {msg['question']}")
                # Answer
                await ws.send(json.dumps({
                    "action": "answer",
                    "question_id": msg["question_id"],
                    "value": "no",
                }))
                break

asyncio.run(main())
```

### SSE client (curl)

```bash
curl -N "http://localhost:8765/api/v1/elicitation/sse?session=abc-123" \
  -H "Authorization: Bearer $TOKEN_WITH_ELICITATION_READ"
```

### Long-poll client (Python)

```python
import requests

BASE = "http://localhost:8765"

# Poll
r = requests.get(f"{BASE}/api/v1/elicitation/poll", timeout=35)
if r.status_code == 200:
    q = r.json()
    # Get answer from user
    answer = input(f"{q['question']} ({q['options']}): ")
    requests.post(f"{BASE}/api/v1/elicitation/answer", json={
        "question_id": q["question_id"],
        "answer": answer,
    })
# 404 = no pending question; retry
```

## Troubleshooting

### WS connection закрывается с code 1008

`hooks_elicitation_ws_enabled=False`. Включить через env var или settings.

### SSE возвращает 403

- `hooks_elicitation_sse_enabled=True`?
- Токен имеет scope `elicitation.read`? Создать: `harness auth create --scopes elicitation.read`.

### Long-poll всегда 404

Нет pending questions. Это нормально — caller должен retry. Timeout = `hooks_elicitation_longpoll_timeout_s=30s`.

### Decision history пустая

Брокер не опубликовал ни одного вопроса с `requires_confirmation=True`. Проверь что `confirm_dangerous_hook` включён (`hooks_builtin_confirm_dangerous_enabled=True`, default).

## См. также

- [`docs/hooks.md`](hooks.md) — Hooks framework (Elicitation — одно из 16 events)
- [`docs/api.md`](api.md) — endpoints reference
- [`docs/scope-api.md`](scope-api.md) — `elicitation.read` scope
- `harness/elicitation.py` — broker исходный код
- `harness/server/routes/elicitation*.py` — routes (4 файла)
- `tests/test_elicitation_*.py` — test files (broker, WS, long-poll, SSE, history)

---

**Версия документа:** v1.22.0 (2026-06-19)
**Phase:** 4.3–4.11 — Elicitation (3 транспорта + decision history)
