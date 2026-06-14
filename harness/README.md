# Solomon Harness — Backend Module

FastAPI-based backend для Phase 0.

## Структура

```
harness/
├── __init__.py
├── __main__.py        # Entry point: python -m harness
├── config.py          # Pydantic Settings (env loading, port=8765)
├── server/
│   ├── app.py         # FastAPI factory, CORS, lifespan
│   ├── routes/        # health, models, sessions, chat
│   │   ├── health.py
│   │   ├── models.py
│   │   ├── sessions.py
│   │   └── chat.py    # WebSocket
│   ├── db/            # SQLite + Pydantic models
│   │   ├── models.py
│   │   └── sqlite.py
│   ├── agent/         # Tools, runtime, safety, loop, prompts, session
│   │   ├── tools.py
│   │   ├── runtime.py
│   │   ├── safety.py
│   │   ├── loop.py
│   │   ├── prompts.py
│   │   └── session.py
│   └── llm/           # MODELS, LLMRouter (litellm)
│       ├── models.py
│       └── router.py
└── web/               # Frontend (Vite + React) — см. harness/web/
```

## Запуск

```bash
# Backend
python -m harness
# Uvicorn running on http://0.0.0.0:8765

# Frontend (отдельный терминал)
cd web && npm install && npm run dev
# ➜ Local: http://localhost:5173/
```

## Тесты

```bash
# Все тесты (62 unit, mock-only)
pytest tests/ -q

# Только smoke (после Step 8)
pytest tests/test_smoke.py -v

# С реальным LLM (нужен MINIMAX_API_KEY или другой)
pytest tests/test_smoke.py -v -m real_llm
```

## Env vars

| Var | Описание |
|-----|----------|
| `MINIMAX_API_KEY` | MiniMax-M2.7 |
| `ZHIPUAI_API_KEY` | glm-4.7 |
| `MOONSHOT_API_KEY` | moonshot-v1-128k |
| `HARNESS_HOST` | Bind host (default `0.0.0.0`) |
| `HARNESS_PORT` | Bind port (default `8765`) |
| `HARNESS_LOG_LEVEL` | `INFO` / `DEBUG` |
| `HARNESS_PROJECT_ROOT` | Корень для file tools (default `C:/MyAI`) |

## Слои (коротко)

1. **Routes** (`server/routes/`) — REST + WebSocket endpoints.
2. **Agent** (`server/agent/`) — tools, runtime, safety, loop.
3. **LLM** (`server/llm/`) — модельный каталог + LiteLLM-обёртка.
4. **DB** (`server/db/`) — SQLite (index) + JSONL (источник истины).

## Endpoints (REST)

| Метод | Путь | Назначение |
|-------|------|------------|
| `GET` | `/api/health` | Healthcheck |
| `GET` | `/api/models` | Каталог моделей |
| `GET` | `/api/sessions` | Список сессий |
| `POST` | `/api/sessions` | Создать |
| `GET` | `/api/sessions/{id}` | Метаданные |
| `PATCH` | `/api/sessions/{id}` | Переименовать / сменить модель |
| `DELETE` | `/api/sessions/{id}` | Удалить |
| `GET` | `/api/sessions/{id}/messages` | История |
| `WS` | `/api/chat/ws` | Streaming chat |

## Storage

```
harness/data/
├── harness.db              # SQLite (aiosqlite) — индекс
└── sessions/
    └── {session_id}.jsonl  # append-only, rebuild при старте
```

## Подробная документация

- `docs/quickstart.md` — quickstart для пользователя.
- `docs/architecture.md` — секция "Phase 0 Web MVP" (добавлена в Step 11).
- `docs/CHANGELOG.md` — история Фазы 0.
