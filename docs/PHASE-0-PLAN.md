# План реализации Фазы 0

**Версия:** 1.0 (13.06.2026)
**Зависимости:** `docs/PHASE-0-SPEC.md`
**Стратегия:** Backend-first, smoke tests — verification gate

---

## Принципы

1. **Backend-first** — сначала работающий API + CLI-клиент для smoke tests, потом UI
2. **Mock LLM** для разработки UI (без затрат на API)
3. **JSONL — source of truth**, SQLite — индекс (rebuild на старте)
4. **Каждый шаг** = коммит + verification

---

## Шаг 1: Backend skeleton + конфиг

**Файлы:**
- `harness/__main__.py` (entry: `python -m harness` → uvicorn)
- `harness/config.py` (Pydantic Settings: env loading)
- `harness/server/app.py` (FastAPI app factory, CORS, lifespan)
- `harness/server/routes/health.py` (`/api/health`)
- `.env.example` (ZHIPUAI_API_KEY, MOONSHOT_API_KEY, MINIMAX_API_KEY)
- `pyproject.toml` — добавить deps: `fastapi`, `uvicorn[standard]`, `websockets`, `aiosqlite`, `python-multipart`

**DoD:**
- `python -m harness` запускает сервер на :8000
- `curl http://localhost:8000/api/health` → `{"status": "ok", "version": "0.1.0"}`
- CORS настроен на `http://localhost:5173`
- Health endpoint возвращает 200

**Verify:**
```bash
python -m harness &
sleep 3
curl http://localhost:8000/api/health
```

---

## Шаг 2: SQLite + Pydantic domain models

**Файлы:**
- `harness/server/db/models.py` (`Session`, `Message` Pydantic)
- `harness/server/db/sqlite.py` (aiosqlite, init, CRUD, rebuild_from_jsonl)
- `harness/server/deps.py` (DI: get_db, get_session_store)
- `data/.gitkeep` (пустая директория под data/)

**DoD:**
- `pytest tests/test_db.py` — roundtrip создание/чтение/удаление сессий и сообщений
- При старте сервера, если `harness.db` отсутствует, но есть `data/sessions/*.jsonl` — rebuild

**Verify:**
```python
async def test_session_roundtrip():
    s = await create_session(db, title="t", model="MiniMax-M2.7")
    s2 = await get_session(db, s.id)
    assert s2.title == "t"
    await delete_session(db, s.id)
```

---

## Шаг 3: Sessions REST API

**Файлы:**
- `harness/server/routes/sessions.py` (CRUD: list, create, get, delete, add_message, get_messages)

**DoD:**
- `curl POST /api/sessions` → 201 + session
- `curl GET /api/sessions` → list
- `curl GET /api/sessions/{id}/messages` → list messages
- `curl DELETE /api/sessions/{id}` → 204

**Verify:**
```bash
SID=$(curl -s -X POST http://localhost:8000/api/sessions \
  -H "Content-Type: application/json" \
  -d '{"title":"smoke","model":"MiniMax-M2.7"}' | jq -r .id)
curl http://localhost:8000/api/sessions/$SID
curl -X DELETE http://localhost:8000/api/sessions/$SID
```

---

## Шаг 4: Tool runtime + schemas

**Файлы:**
- `harness/server/agent/tools.py` (TOOL_SCHEMAS + ToolRegistry)
- `harness/server/agent/runtime.py` (subprocess execution с таймаутом)
- `harness/server/agent/safety.py` (deny patterns)

**Tools:**

| Tool | Реализация | Таймаут |
|------|-----------|---------|
| `read_file` | `pathlib.read_text(encoding='utf-8')` | — |
| `edit_file` | exact string match, `pathlib.write_text` | — |
| `write_file` | `pathlib.write_text(encoding='utf-8')` | — |
| `bash` | `asyncio.subprocess.create_subprocess_shell` | 30s (1-300s) |
| `grep` | `asyncio.subprocess` + `rg` (если есть) или `grep` fallback | 30s |
| `glob` | `pathlib.Path.glob` (sync, в `asyncio.to_thread`) | — |

**Safety:**
- Bash: regex deny на `rm\s+-rf\s+/`, `del\s+/s`, `format\s+`, `git\s+push\s+--force`, `git\s+reset\s+--hard`
- Paths: project_root = `C:/MyAI/`, `..` за пределы — deny

**DoD:**
- `pytest tests/test_tools.py` — каждый tool с позитивным и негативным кейсом
- 6 tools × 2 кейса = 12 unit tests, все green

**Verify:**
```python
async def test_bash_deny_rm_rf():
    result = await runtime.execute("bash", {"command": "rm -rf /"})
    assert not result.ok
    assert "denied" in result.error
```

---

## Шаг 5: LiteLLM router

**Файлы:**
- `harness/server/llm/router.py` (LiteLLM wrapper: completion, streaming)
- `harness/server/llm/models.py` (model catalog: id, provider, env var, context, pricing)
- `harness/server/routes/models.py` (`/api/models`)

**Model catalog:**
```python
MODELS = [
    {"id": "MiniMax-M2.7", "provider": "minimax", "tier": "T3", "env": "MINIMAX_API_KEY", "ctx": 200000},
    {"id": "glm-4.7", "provider": "zhipuai", "tier": "T3", "env": "ZHIPUAI_API_KEY", "ctx": 128000},
    {"id": "moonshot-v1-128k", "provider": "moonshot", "tier": "T3", "env": "MOONSHOT_API_KEY", "ctx": 128000},
]
```

**DoD:**
- `GET /api/models` возвращает 3 модели с метаданными
- Модель без API ключа помечается `available: false`
- `completion()` работает на MiniMax-M2.7 (smoke)
- `streaming_completion()` возвращает async iterator

**Verify:**
```bash
curl http://localhost:8000/api/models
```

---

## Шаг 6: Agent loop

**Файлы:**
- `harness/server/agent/loop.py` (AgentLoop класс)
- `harness/server/agent/prompts.py` (system prompt: role, tools, project_root)

**Loop algorithm:**
```
for iteration in range(5):
    response = await llm.completion(messages, tools=TOOL_SCHEMAS, stream=True)
    async for event in response:
        yield event  # → WebSocket
    if response.has_tool_calls:
        for tool_call in response.tool_calls:
            result = await runtime.execute(tool_call.name, tool_call.args)
            messages.add_tool_result(tool_call.id, result)
            yield tool_result event
        continue  # next iteration
    else:
        break  # final answer
```

**DoD:**
- `pytest tests/test_agent_loop.py` с mock LLM:
  - 1 итерация, без tools → 1 assistant message
  - 2 итерации, 1 tool call → 2 assistant + 1 tool result
  - 5 итераций cap → loop завершается с ошибкой "max iterations"

**Verify:** mock test в test_agent_loop.py (без API)

---

## Шаг 7: WebSocket chat endpoint

**Файлы:**
- `harness/server/routes/chat.py` (`/api/chat/ws`)
- `harness/server/agent/session.py` (обёртка: load history, run loop, persist)

**WebSocket flow:**
1. Клиент подключается: `?session_id=...&model=...`
2. Сервер загружает историю из SQLite
3. Клиент шлёт `{type: "user_message", content: "..."}`
4. Сервер добавляет user message в SQLite + JSONL
5. Сервер запускает agent loop, стримит события
6. На `session_done` клиент может слать следующее сообщение

**DoD:**
- `tests/test_chat_ws.py` (async): подключиться, отправить message, получить `message_done` + `session_done`
- Smoke test 1 (read_file) проходит через WebSocket

**Verify:**
```python
async with websockets.connect("ws://localhost:8000/api/chat/ws?session_id=...&model=MiniMax-M2.7") as ws:
    await ws.send(json.dumps({"type": "user_message", "content": "Привет"}))
    events = []
    async for msg in ws:
        events.append(json.loads(msg))
        if msg["type"] == "session_done":
            break
    assert any(e["type"] == "token" for e in events)
```

---

## Шаг 8: Smoke tests end-to-end

**Файлы:**
- `tests/test_smoke.py` (5 сценариев из SPEC §7)
- `tests/conftest.py` (фикстуры: test_db, tmp_sessions_dir, real LLM marker)

**Marking:**
- `pytest -m "not real_llm"` — все 5 mock-тестов
- `pytest -m real_llm` — 5 тестов с реальным API (нужен ключ)
- По умолчанию запускается mock-версия (5/5 green = DoD)

**DoD:**
- `pytest tests/test_smoke.py` — 5/5 green на mock LLM
- README/quickstart содержит инструкцию для real_llm

**Verify:**
```bash
pytest tests/test_smoke.py -v
# 5 passed
```

---

## Шаг 9: Frontend scaffold

**Файлы:**
- `harness/web/package.json` (React 18, TS, Vite, react-markdown, ws)
- `harness/web/vite.config.ts` (proxy /api → :8000)
- `harness/web/tsconfig.json`
- `harness/web/index.html`
- `harness/web/src/main.tsx`
- `harness/web/src/App.tsx` (роутинг, layout)
- `harness/web/src/api/client.ts` (REST)
- `harness/web/src/api/ws.ts` (WebSocket)

**DoD:**
- `cd harness/web && npm install && npm run dev` стартует :5173
- Vite proxy /api/* → :8000 работает
- Страница-заглушка с надписью "Solomon Harness — Phase 0"

**Verify:**
```bash
cd harness/web
npm install
npm run dev &
sleep 5
curl http://localhost:5173
```

---

## Шаг 10: Frontend chat UI

**Файлы:**
- `harness/web/src/components/SessionList.tsx` (левая колонка, fetch /api/sessions)
- `harness/web/src/components/ChatView.tsx` (правая колонка: messages + input)
- `harness/web/src/components/MessageBubble.tsx` (user/assistant/tool rendering)
- `harness/web/src/components/ToolCallCard.tsx` (collapsible input/output)
- `harness/web/src/components/ModelSelector.tsx` (dropdown с /api/models)
- `harness/web/src/components/InputBar.tsx` (textarea + send, Enter для отправки)
- `harness/web/src/styles/globals.css` (минимальный layout: 2 колонки, тёмная тема)

**DoD:**
- Открыть http://localhost:5173 в браузере
- Слева: список сессий, кнопка "New chat"
- Справа: модельный selector, история, input bar
- Создать новую сессию, выбрать модель, отправить "Привет", получить стриминг ответа
- В чате видны tool_call cards (если LLM вызвал tool)

**Verify:** ручное тестирование в браузере + скриншот в `_output/2026-06/`

---

## Шаг 11: Quickstart + обновление docs

**Файлы:**
- `docs/quickstart.md` (как запустить backend + frontend + первый запрос)
- `docs/architecture.md` (обновить Web-схему)
- `README.md` (обновить статус: Фаза 0 ✅)

**DoD:**
- `docs/quickstart.md` пошагово от `git clone` до первого сообщения в чате
- Скриншот UI в `docs/images/`
- README указывает на quickstart

**Verify:**
- Новый разработчик (или Марк после перерыва) может пройти quickstart за <10 мин

---

## Порядок выполнения и оценка

| Шаг | Что | Оценка | Зависит от |
|------|-----|--------|------------|
| 1 | Backend skeleton | 1-2 ч | — |
| 2 | SQLite + models | 1-2 ч | 1 |
| 3 | Sessions REST | 1-2 ч | 2 |
| 4 | Tool runtime | 2-3 ч | 1 |
| 5 | LiteLLM router | 1-2 ч | 1 |
| 6 | Agent loop | 2-3 ч | 4, 5 |
| 7 | WebSocket chat | 2-3 ч | 3, 6 |
| 8 | Smoke tests | 1-2 ч | 7 |
| 9 | Frontend scaffold | 1 ч | — |
| 10 | Chat UI | 3-4 ч | 9, 7 |
| 11 | Quickstart + docs | 1-2 ч | 8, 10 |
| **Итого** | | **16-26 ч** (1.5-3 дня full-time) | |

---

## Коммиты

Один шаг = один коммит. Conventional Commits:
- `feat(backend): skeleton + health endpoint` (шаг 1)
- `feat(db): sqlite + pydantic models` (шаг 2)
- `feat(api): sessions CRUD` (шаг 3)
- `feat(tools): 6 tools + safety` (шаг 4)
- `feat(llm): litellm router + 3 models` (шаг 5)
- `feat(agent): loop with max 5 iterations` (шаг 6)
- `feat(chat): websocket streaming` (шаг 7)
- `test(smoke): 5 scenarios e2e` (шаг 8)
- `feat(web): vite + react scaffold` (шаг 9)
- `feat(ui): chat interface` (шаг 10)
- `docs(quickstart): web mvp walkthrough` (шаг 11)

---

## Стратегия отката

На любом шаге — `git revert HEAD`. JSONL — append-only, ничего не теряется.

---

**Готов к старту после утверждения спецификации.**
