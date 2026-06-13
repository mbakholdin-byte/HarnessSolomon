# Спецификация Фазы 0 — Solomon Harness Web MVP

**Версия:** 1.0 (13.06.2026)
**Автор:** Соломон
**Статус:** Утверждено
**Таймлайн:** 1–2 недели

---

## 1. Видение

Web-обёртка вместо CLI. Марк работает в браузере (или нескольких браузерах параллельно) и получает полный опыт Claude Code / OpenCode, но на **открытых LLM через LiteLLM**, с возможностью смены провайдера per-task.

**Главное отличие от CLI:** история сессий живёт в БД, доступна с любого устройства; легко делиться с коллегами read-only ссылкой.

## 2. Scope (что входит)

### 2.1. Backend (`harness/server/`)

- **FastAPI** (Python 3.12+, async) на `:8000`
- **LiteLLM** как multi-provider абстрактор
- **5 tools** (выполняются на сервере):
  1. `read_file(path)` → текст файла
  2. `edit_file(path, old_string, new_string)` → атомарная правка
  3. `write_file(path, content)` → создать/перезаписать
  4. `bash(command, timeout)` → shell с таймаутом
  5. `grep(pattern, path, globs)` → ripgrep
  6. `glob(pattern)` → список файлов
- **Agent loop** (5 циклов max per task, защита от зацикливания)
- **WebSocket** endpoint для streaming токенов + tool calls
- **Session persistence:** JSONL в `data/sessions/<session_id>.jsonl`
- **SQLite** для метаданных сессий (path, title, model, created_at)

### 2.2. Frontend (`harness/web/`)

- **Vite + React 18 + TypeScript**
- **Chat UI** (левая колонка: история, правая: диалог)
- **Выбор модели** через dropdown (MiniMax-M2.7, GLM-4.7, Kimi K2.6)
- **Streaming ответов** через WebSocket
- **Tool call visualization** (collapsible cards: input/output)
- **Markdown рендер** ответов (react-markdown)
- **Session resume** по session_id

### 2.3. Multi-provider (облачные API)

| Провайдер | Модель | Tier | Env var |
|-----------|--------|------|---------|
| ZhipuAI | `glm-4.7` | T3 (coding) | `ZHIPUAI_API_KEY` |
| Moonshot | `moonshot-v1-128k` (Kimi K2.6) | T3 (long-ctx) | `MOONSHOT_API_KEY` |
| MiniMax | `MiniMax-M2.7` | T3 (vibe) | `MINIMAX_API_KEY` |

**DeepSeek не подключаем** (решение Марка).
**Локальные модели (Qwen3 8B/30B) — в Фазе 0.5** (после MVP).

### 2.4. Что НЕ входит в Фазу 0

- ❌ Sub-agents (Фаза 2)
- ❌ Multi-layer memory: hmem/mem0/mempalace/hybrid (Фаза 1)
- ❌ KG-RAG, Neo4j (Фаза 1+)
- ❌ Rerank (BGE-reranker) (Фаза 1)
- ❌ Reflection loop, consolidation (Фаза 1)
- ❌ Cost-aware router (только manual выбор модели)
- ❌ Hooks (Фаза 4)
- ❌ Eval harness (Фаза 5)
- ❌ Docker sandbox (Фаза 5)
- ❌ Локальные модели (Фаза 0.5)
- ❌ Hot-reload skills (Фаза 2+)
- ❌ RU-first локализация UI (Фаза 6, базовые labels — EN)

## 3. Архитектура

```
┌─────────────────┐       WebSocket       ┌──────────────────────┐
│  React/TS       │ ◄──────────────────► │  FastAPI :8000       │
│  Vite dev :5173 │       REST (CRUD)     │  /api/sessions       │
└─────────────────┘                        │  /api/chat/ws        │
                                          └──────────┬───────────┘
                                                     │
                                          ┌──────────▼───────────┐
                                          │  Agent Loop          │
                                          │  (max 5 iterations)  │
                                          └──────────┬───────────┘
                                                     │
                            ┌────────────────────────┼────────────────────┐
                            │                        │                    │
                  ┌─────────▼─────────┐  ┌────────────▼────────┐  ┌───────▼──────┐
                  │  LiteLLM Router   │  │  Tool Runtime       │  │  Session     │
                  │  (MiniMax/GLM/    │  │  (subprocess)       │  │  Store       │
                  │   Kimi)           │  │  read/edit/write/   │  │  JSONL+SQLite│
                  └───────────────────┘  │  bash/grep/glob     │  └──────────────┘
                                         └─────────────────────┘
```

## 4. Контракты API

### 4.1. REST endpoints

| Method | Path | Описание | Тело ответа |
|--------|------|----------|-------------|
| `GET` | `/api/health` | Liveness | `{status: "ok", version: "0.1.0"}` |
| `GET` | `/api/models` | Список доступных моделей | `[{id, provider, tier, context, pricing}]` |
| `GET` | `/api/sessions` | Список сессий (последние 50) | `[{id, title, model, created_at, message_count}]` |
| `POST` | `/api/sessions` | Создать сессию | `{id, title, model, created_at}` |
| `GET` | `/api/sessions/{id}` | Метаданные сессии | `{id, title, model, created_at, message_count}` |
| `GET` | `/api/sessions/{id}/messages` | Все сообщения сессии | `[{role, content, tool_calls?, ts}]` |
| `DELETE` | `/api/sessions/{id}` | Удалить сессию | `{status: "deleted"}` |
| `POST` | `/api/sessions/{id}/messages` | Добавить user message | `{id: <msg_id>}` |

### 4.2. WebSocket

**Endpoint:** `ws://localhost:8000/api/chat/ws?session_id={id}&model={model_id}`

**Клиент → Сервер:**
```json
{
  "type": "user_message",
  "content": "Прочитай файл C:/MyAI/CLAUDE.md и ответь, что в нём главное"
}
```

**Сервер → Клиент** (event types):
```json
{ "type": "token", "content": "В CLAUDE.md..." }
{ "type": "tool_call", "name": "read_file", "args": {...}, "id": "tc_abc" }
{ "type": "tool_result", "id": "tc_abc", "output": "...", "ok": true }
{ "type": "message_done", "message_id": "msg_xyz", "usage": {...} }
{ "type": "error", "code": "...", "message": "..." }
{ "type": "session_done" }
```

### 4.3. Tool schemas (для LLM, OpenAI-compatible)

```python
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read text file. Returns content or error.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative path"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Atomically replace old_string with new_string in file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"}
                },
                "required": ["path", "old_string", "new_string"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite file with content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"}
                },
                "required": ["path", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute shell command with timeout. cwd is project root.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command (bash syntax)"},
                    "timeout": {"type": "integer", "default": 30, "minimum": 1, "maximum": 300}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search for pattern in files using ripgrep.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                    "globs": {"type": "array", "items": {"type": "string"}},
                    "max_results": {"type": "integer", "default": 50}
                },
                "required": ["pattern"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "List files matching glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern like **/*.py"}
                },
                "required": ["pattern"]
            }
        }
    }
]
```

## 5. Хранилище

### 5.1. SQLite (`data/harness.db`)

```sql
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,              -- uuid4
    title TEXT NOT NULL,
    model TEXT NOT NULL,               -- 'MiniMax-M2.7' | 'glm-4.7' | 'moonshot-v1-128k'
    created_at TEXT NOT NULL,          -- ISO 8601
    updated_at TEXT NOT NULL,
    message_count INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    total_cost REAL DEFAULT 0.0
);

CREATE INDEX idx_sessions_updated_at ON sessions(updated_at DESC);

CREATE TABLE messages (
    id TEXT PRIMARY KEY,              -- uuid4
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,               -- 'user' | 'assistant' | 'tool'
    content TEXT NOT NULL,
    tool_calls TEXT,                  -- JSON array
    tool_results TEXT,                -- JSON array
    model TEXT,                       -- для assistant
    usage TEXT,                       -- JSON: {input_tokens, output_tokens}
    cost REAL,
    ts TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX idx_messages_session ON messages(session_id, ts);
```

### 5.2. JSONL mirror

`data/sessions/<session_id>.jsonl` — каждая строка = одно сообщение в формате:
```json
{"id":"msg_...","role":"user","content":"...","ts":"2026-06-13T..."}
```

**Dual-write:** запись идёт в SQLite + append в JSONL. JSONL — source of truth для восстановления.

## 6. Структура репозитория (после Фазы 0)

```
06_Harness/
├── CLAUDE.md
├── README.md
├── pyproject.toml
├── .env.example                  # ZHIPUAI_API_KEY, MOONSHOT_API_KEY, MINIMAX_API_KEY
├── .gitignore
├── data/                         # gitignored
│   ├── harness.db
│   └── sessions/
├── docs/
│   ├── PHASE-0-SPEC.md            # ← ЭТОТ ФАЙЛ
│   ├── roadmap.md                # v1.1
│   ├── architecture.md           # обновить под Web
│   ├── MODEL_REGISTRY.md
│   ├── MODEL_SUPPORT.md
│   ├── cc-vs-opencode.md
│   ├── techniques-catalog.md
│   ├── sources.md
│   └── quickstart.md             # ← создать в Фазе 0
├── harness/
│   ├── __init__.py
│   ├── __main__.py               # python -m harness → uvicorn harness.server.app:app
│   ├── config.py                 # Pydantic Settings (env loading)
│   ├── server/
│   │   ├── __init__.py
│   │   ├── app.py                # FastAPI app factory
│   │   ├── deps.py               # DI (db, litellm_router)
│   │   ├── routes/
│   │   │   ├── __init__.py
│   │   │   ├── health.py
│   │   │   ├── models.py
│   │   │   ├── sessions.py
│   │   │   └── chat.py           # WebSocket endpoint
│   │   ├── agent/
│   │   │   ├── __init__.py
│   │   │   ├── loop.py           # Agent loop (max 5 iter)
│   │   │   ├── tools.py          # Tool registry + schemas
│   │   │   └── runtime.py        # Tool execution (subprocess)
│   │   ├── llm/
│   │   │   ├── __init__.py
│   │   │   ├── router.py         # LiteLLM wrapper
│   │   │   └── streaming.py
│   │   └── db/
│   │       ├── __init__.py
│   │       ├── sqlite.py         # async sqlite3
│   │       └── models.py         # Pydantic domain models
│   ├── web/                       # Vite + React/TS
│   │   ├── package.json
│   │   ├── vite.config.ts
│   │   ├── tsconfig.json
│   │   ├── index.html
│   │   ├── src/
│   │   │   ├── main.tsx
│   │   │   ├── App.tsx
│   │   │   ├── api/
│   │   │   │   ├── client.ts     # REST client
│   │   │   │   └── ws.ts         # WebSocket client
│   │   │   ├── components/
│   │   │   │   ├── SessionList.tsx
│   │   │   │   ├── ChatView.tsx
│   │   │   │   ├── MessageBubble.tsx
│   │   │   │   ├── ToolCallCard.tsx
│   │   │   │   ├── ModelSelector.tsx
│   │   │   │   └── InputBar.tsx
│   │   │   └── styles/
│   │   │       └── globals.css
└── tests/
    ├── test_smoke.py              # 5 сценариев из Definition of Done
    ├── test_tools.py              # unit tests для каждого tool
    ├── test_agent_loop.py         # agent loop с mock LLM
    └── test_db.py                 # SQLite roundtrip
```

## 7. Definition of Done (smoke tests)

5 базовых сценариев, которые проходят на любой из 3 моделей:

### Test 1: Read + ответ
- **Задача:** "Прочитай файл C:/MyAI/06_Harness/README.md и ответь, какой стек"
- **Ожидаемо:** tool_call `read_file` → assistant отвечает текстом из README

### Test 2: Edit файл
- **Задача:** "В файле data/test_edit.md замени 'old' на 'new'"
- **Ожидаемо:** tool_call `edit_file` → файл изменён, assistant подтверждает

### Test 3: Grep + анализ
- **Задача:** "Найди все TODO в harness/ и перечисли"
- **Ожидаемо:** tool_call `grep` → assistant выдаёт список

### Test 4: WebFetch (proxy через bash)
- **Задача:** "Скачай https://example.com и скажи заголовок"
- **Ожидаемо:** tool_call `bash` (curl) → assistant отвечает

### Test 5: Multi-turn с TodoWrite
- **Задача 1:** "Создай файл test.txt с 'hello'"
- **Задача 2:** "Допиши в конец ' world'"
- **Задача 3:** "Прочитай и подтверди"
- **Ожидаемо:** 3 user messages, 3 assistant + tool_call цепочки, история сохраняется

## 8. Security

### 8.1. В Фазе 0 (минимум)

- **API ключи:** только из env, не из payload
- **Bash tool:** deny по glob:
  - `rm -rf /` (и варианты)
  - `del /s *` (Windows)
  - `format *`
  - `git push --force`
  - `git reset --hard`
- **Пути:** все tool calls резолвятся относительно project_root (`C:/MyAI/`), абсолютные пути за пределами — deny
- **CORS:** allow `http://localhost:5173` (Vite dev), prod — настраивается через env

### 8.2. В будущих фазах (не блокеры)

- Auth (JWT, OAuth)
- Multi-tenant scope
- Per-user rate limits
- PII redaction

## 9. Метрики успеха Фазы 0

| Метрика | Целевое значение |
|---------|------------------|
| Server startup | < 5 сек |
| First token latency (MiniMax-M2.7) | < 3 сек |
| Tool execution (read_file) | < 100 мс |
| Session resume time | < 500 мс |
| Smoke tests pass rate | 5/5 на 3 моделях |
| Time-to-first-useful-response | < 30 сек (cold) |

## 10. Риски

| Риск | Митигация |
|------|-----------|
| LiteLLM ломает streaming для одной из моделей | Mock streaming для тестов, fallback на non-streaming |
| Frontend Vite HMR конфликтует с FastAPI reload | Отдельные порты, Vite proxy /api → :8000 |
| JSONL дублирование расходится с SQLite | JSONL — source of truth, SQLite — индекс; rebuild SQLite из JSONL при старте |
| Марк не имеет ключей API для всех 3 провайдеров | MiniMax-M2.7 — primary, GLM-4.7/Kimi — optional; smoke tests на 1 модели достаточно для DoD |

## 11. Следующие шаги (после утверждения spec)

1. Создать `pyproject.toml` секции для FastAPI, uvicorn, websockets, aiosqlite
2. Установить зависимости в `harness/.venv`
3. Реализовать по плану (см. `PHASE-0-PLAN.md`)
4. Прогнать smoke tests
5. Написать `docs/quickstart.md`

---

**Согласовано с Марком:** 13.06.2026
**Следующий review:** после завершения Фазы 0 (через 1–2 недели)
