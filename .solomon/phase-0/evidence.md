# Evidence — Solomon Harness Phase 0

**Дата:** 13.06.2026
**Связано с:** `spec.md`, `verdict.json`

## AC1: Backend запускается, /api/health → 200

**Статус**: PASS
**Доказательство**:
- Факт 1: `python -m harness` поднимает uvicorn на :8765 (свободный порт)
- Факт 2: `curl http://127.0.0.1:8765/api/health` → `{"status":"ok","version":"0.1.0","project_root":"C:\\MyAI"}` (HTTP 200)
- Факт 3: OpenAPI schema доступна: `/openapi.json` → `title: "Solomon Harness", paths: ['/api/health']`
- Факт 4: CORS headers присутствуют: `access-control-allow-origin: http://localhost:5173` ✓
**Лог**: см. `raw/step1_health.log`

## AC2: pyproject.toml зависимости

**Статус**: PASS
**Доказательство**:
- Факт 1: pip install -e ".[dev]" — 98 пакетов, 0 ошибок
- Факт 2: Установлены: fastapi 0.136.3, uvicorn 0.49.0, pydantic 2.13.4, pydantic-settings 2.14.1, aiosqlite 0.22.1, litellm 1.88.1, websockets 16.0, python-multipart 0.0.32
- Факт 3: dev: pytest 9.0.3, pytest-asyncio 1.4.0, pytest-cov 7.1.0, ruff 0.15.17, mypy 2.1.0, black 26.5.1, pre-commit 4.6.0

## AC3: SQLite CRUD

**Статус**: PASS
**Доказательство**:
- `tests/test_db.py::test_session_roundtrip` PASSED — create/get/list/delete работают
- `tests/test_db.py::test_delete_session_cascades_messages` PASSED — FK cascade работает
- PRAGMA foreign_keys = ON в каждом соединении

## AC4: JSONL mirror (write-through + rebuild)

**Статус**: PASS
**Доказательство**:
- `tests/test_db.py::test_message_roundtrip` PASSED — add_message + append_jsonl работают
- `tests/test_db.py::test_rebuild_from_jsonl` PASSED — orphan JSONL пересобирается в SQLite (3 messages, total_tokens=15, cost=0.0001)
- JSONL формат: одна строка = одно сообщение, сериализация через `model_dump_json`

## AC5: Pydantic v2 domain models

**Статус**: PASS
**Доказательство**:
- `tests/test_db.py::test_tool_call_serialization` PASSED
- ToolCall/ToolResult/MessageUsage roundtrip через JSONL
- Все модели используют ConfigDict(extra="ignore") для устойчивости

## AC6: Lifespan hook

**Статус**: PASS
**Доказательство**:
- `harness/server/app.py::lifespan` — init_db() + rebuild_from_jsonl() если sessions пуст и JSONL есть
- Verified: server start на :8767 → health 200 OK (после фикса lifespan)

## AC7: pytest test_db.py 5/5 green

**Статус**: PASS
**Доказательство**:
```
tests/test_db.py::test_session_roundtrip PASSED                  [ 20%]
tests/test_db.py::test_message_roundtrip PASSED                  [ 40%]
tests/test_db.py::test_tool_call_serialization PASSED            [ 60%]
tests/test_db.py::test_rebuild_from_jsonl PASSED                 [ 80%]
tests/test_db.py::test_delete_session_cascades_messages PASSED   [100%]
======================= 5 passed, 0 warnings in 0.29s =======================
```

## Sol-Verify (Level 1 + 2) на Шаги 1+2

**Статус**: PASS (8/8)
- ✓ 12 файлов существуют, не пустые
- ✓ Нет TODO/FIXME/TBD/XXX/placeholder
- ✓ Нет null/undefined в коде
- ✓ pyproject.toml парсится (TOML)
- ✓ 5/5 тестов passed
- ✓ JSONL файлы создаются в data/sessions/
- ✓ harness.db создаётся в data/

## Pending (AC8–AC15) — 9 шагов

- AC8: Sessions REST API (Шаг 3)
- AC9: Tool runtime + safety (Шаг 4)
- AC10: LiteLLM router (Шаг 5)
- AC11: Agent loop (Шаг 6)
- AC12: WebSocket chat (Шаг 7)
- AC13: 5 smoke tests e2e (Шаг 8)
- AC14: Frontend scaffold (Шаг 9)
- AC15: Chat UI (Шаг 10) + Quickstart (Шаг 11)
