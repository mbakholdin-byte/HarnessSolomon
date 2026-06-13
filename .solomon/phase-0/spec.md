# Задача: Solomon Harness Phase 0 — Web MVP

**Дата:** 13.06.2026
**Статус:** IN_PROGRESS (Шаги 1-2 завершены, 9 в очереди)
**Связано с:** `docs/PHASE-0-SPEC.md`, `docs/PHASE-0-PLAN.md`

## Acceptance Criteria

- [x] **AC1**: Backend на FastAPI запускается, `GET /api/health` → 200 с `status:ok, version, project_root`
- [x] **AC2**: `pyproject.toml` содержит все зависимости Фазы 0 (fastapi, uvicorn, websockets, aiosqlite, pydantic, pydantic-settings, litellm, python-multipart)
- [x] **AC3**: SQLite store поддерживает CRUD сессий и сообщений через aiosqlite
- [x] **AC4**: JSONL mirror работает в обе стороны: write-through (add_message → append_jsonl), rebuild (orphan JSONL → SQLite)
- [x] **AC5**: Pydantic v2 domain models (Session, Message, ToolCall, ToolResult, MessageUsage) сериализуются в JSONL
- [x] **AC6**: Lifespan hook в app.py инициализирует DB и при необходимости пересобирает индекс из JSONL
- [x] **AC7**: pytest test_db.py содержит 5 тестов, все green
- [ ] **AC8**: Sessions REST API: POST/GET/LIST/DELETE/add_message/list_messages
- [ ] **AC9**: Tool runtime: 6 tools (read_file, edit_file, write_file, bash, grep, glob) + safety
- [ ] **AC10**: LiteLLM router: 3 cloud-провайдера, /api/models endpoint
- [ ] **AC11**: Agent loop: max 5 iter, streaming
- [ ] **AC12**: WebSocket /api/chat/ws: token + tool_call + tool_result + message_done + session_done
- [ ] **AC13**: 5 smoke-сценариев из SPEC §7 проходят
- [ ] **AC14**: Frontend: Vite + React/TS chat UI, Vite proxy /api → :8000
- [ ] **AC15**: docs/quickstart.md: clone → run → first message

## Constraints

- Только облачные API в Фазе 0 (MiniMax-M2.7, GLM-4.7, Kimi K2.6)
- Никаких локальных моделей, никаких sub-agents, никаких memory layers
- JSONL — source of truth, SQLite — индекс
- pyproject.toml ≥ 3.12, async, Pydantic v2
- Каждый шаг = один коммит с conventional commit message + push

## Non-Goals (явно вне Фазы 0)

- ❌ Sub-agents (Фаза 2)
- ❌ Memory: hmem/mem0/mempalace/hybrid (Фаза 1)
- ❌ Local models (Qwen3 8B/30B — Фаза 0.5)
- ❌ Hooks, eval, sandbox (Фазы 4, 5)
- ❌ RU-first UI локализация (Фаза 6)
- ❌ Auth, multi-tenant
