# Phase 3 v1.2.0 — Write context (scratchpad + notes + plan.md per session)

> **Status:** ЗАКРЫТО v1.2.0 (2026-06-15)
> **Tag:** `v1.2.0` (annotated)
> **Tests:** 1076 mock (от 1032 в v1.1.0, +44 net)

## TL;DR

Phase 3 v1.2.0 реализует **"Write context"** стратегию из Anthropic
context-engineering playbook. Агенты получают persistent scratchpad
(per-`session_id` × per-`agent_id`) для структурированных заметок
(`write_note` / `read_notes`) и плана задачи (`plan_step` /
`mark_done`). Это первый шаг от stateless message history к
stateful notes, переживающим compaction.

**Что нового:**

- `harness/agents/scratchpad.py` — `Note`, `PlanStep` dataclasses, `NoteLevel` (L0/L1/L2), `PlanStatus` enum
- `harness/agents/scratchpad_store.py` — `ScratchpadStore` class (SQLite, 2 tables)
- `harness/context/scratchpad_audit.py` — `ScratchpadAudit` (JSONL mirror)
- 4 новых tool'а: `scratchpad_write_note`, `scratchpad_read_notes`, `scratchpad_plan_step`, `scratchpad_mark_done`
- `harness context {read,write,plan}` CLI subcommand
- 2 таблицы в `agent-jobs.db` (sibling `compact_store` / `merge_jobs` / `webhook_events`)
- 4 new settings: `scratchpad_enabled`, `scratchpad_max_notes_per_session`, `scratchpad_l0_max_bytes`, `scratchpad_audit_log`
- Trust boundary preserved: `runner.py` continues to NOT import new modules
- `scratchpad_factory` factory-DI в `AgentRunner.__init__` (mirror `unified_memory_factory`)

## Архитектура

### L0 / L1 / L2 стратификация

```
┌────────────────────────────────────────────────────────────┐
│                  ANTHROPIC "WRITE CONTEXT"                  │
│  4 стратегии контекст-инжиниринга: Write / Select /        │
│  Compress / Isolate. Phase 3 v1.2.0 = Write.               │
└────────────────────────────────────────────────────────────┘
                           │
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
   ┌─────────┐       ┌──────────┐       ┌──────────┐
   │   L0    │       │    L1    │       │    L2    │
   │ "hot"   │       │ "plan"   │       │"archive" │
   │  ≤1KB   │       │  ~10KB   │       │unbounded │
   │ system  │       │per-sess  │       │ dense+   │
   │ prompt  │       │ on-read  │       │  BM25    │
   └────┬────┘       └────┬─────┘       └────┬─────┘
        │                 │                  │
        └─────────────────┼──────────────────┘
                          ▼
                ┌──────────────────┐
                │ ScratchpadStore  │
                │  SQLite (aiosqlite)│
                │  agent-jobs.db   │
                └──────────────────┘
```

**L0 (hot, ≤1KB)** — критические факты которые должны попадать в
system prompt на каждом turn (принятые решения, текущая цель,
known constraints). Cap `scratchpad_l0_max_bytes=1024` enforced на
write — auto-prune oldest при overflow, single note > cap →
`ValueError` (НЕ fail-open, иначе сломан "hot" guarantee).

**L1 (plan, ~10KB)** — контекст плана / decision trail текущей
сессии. Read on demand через `scratchpad_read_notes` tool.

**L2 (archive, unbounded)** — долгосрочный архив. В v1.2.0 —
просто L2-tagged notes без специальной обработки. Dense+BM25
retrieval через этот layer — **Phase 3 v1.3.0**.

### Storage layout (SQLite, agent-jobs.db)

```sql
CREATE TABLE scratchpad_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    agent_id TEXT,                     -- NULL = admin / cross-agent
    level TEXT NOT NULL CHECK(level IN ('L0','L1','L2')),
    content TEXT NOT NULL,
    tags TEXT NOT NULL,                -- JSON list[str]
    created_at REAL NOT NULL
);
CREATE INDEX idx_notes_session_level
    ON scratchpad_notes(session_id, agent_id, level);

CREATE TABLE plan_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    agent_id TEXT,
    description TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','in_progress','done','blocked')),
    deps TEXT NOT NULL,                -- JSON list[int]
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX idx_plans_session_status
    ON plan_steps(session_id, agent_id, status);
```

WAL + `busy_timeout=5000` + `synchronous=NORMAL` — mirror
`CompactStore` / `JobStore` defaults.

### agent_id namespacing

Per-instance binding через constructor: `ScratchpadStore(db_path,
session_id=..., agent_id=...)`. Каждый SELECT/INSERT/UPDATE
фильтрует WHERE по обоим колонкам → sub-agent не видит parent'а и
наоборот. `agent_id=None` = admin / cross-agent (CLI inspector).

## Settings (4 new)

| Setting | Default | Constraint | Описание |
|---------|---------|------------|----------|
| `scratchpad_enabled` | `True` | — | Вкл/выкл 4 scratchpad tools |
| `scratchpad_max_notes_per_session` | `100` | `ge=1` | Cap на кол-во notes |
| `scratchpad_l0_max_bytes` | `1024` | `ge=128` | L0 cap (1KB) |
| `scratchpad_audit_log` | `False` | — | JSONL audit в `data/audit/scratchpad-*.ndjson` |

## Tools (4 new)

| Tool | Required | Optional | Behaviour |
|------|----------|----------|-----------|
| `scratchpad_write_note` | `level`, `content` | `tags` | Persist note. Returns `{id, level, created_at}` |
| `scratchpad_read_notes` | — | `level` | List notes, newest first, max 50 |
| `scratchpad_plan_step` | `description` | `deps` | Add plan step. Returns `{id, status: "pending"}` |
| `scratchpad_mark_done` | `step_id` | `status` | Update step. Default status `done` |

3 из 4 (write_note / plan_step / mark_done) добавлены в
`_READ_ONLY_DENY` denylist. `scratchpad_read_notes` остаётся
доступным read-only агентам (они могут консультироваться со своими
заметками).

## CLI subcommand

```bash
harness context read  --session <id> [--agent <id>] [--level L0|L1|L2]
harness context write --session <id> --level <L> --content <text> [--tags a,b,c]
harness context plan  --session <id> [--agent <id>] [--status pending|...]
harness context plan mark-done --session <id> --step-id <int> [--status done]
```

Прямой SQLite read из `agent-jobs.db` через `asyncio.run(store...)`.
Без HTTP — operator escape hatch когда сервер down. Mirror
`_cmd_agents_jobs` стиля.

## DI в AgentRunner

```python
# server/app.py lifespan
from harness.agents.scratchpad_store import ScratchpadStore

def scratchpad_factory(spec, session_id):
    return ScratchpadStore(
        settings.db_path.parent / "agent-jobs.db",
        session_id=session_id,
        agent_id=spec.memory_namespace or "solomon",
    )

runner = AgentRunner(
    router=router,
    repo=repo,
    scratchpad_factory=scratchpad_factory,
    scratchpad_audit=scratchpad_audit,
)
```

Per-call `runner.run(spec, prompt, session_id="...")` строит
fresh `ScratchpadStore` через factory, init()'ит его, инжектит в
`ToolRuntime` как `scratchpad` kwarg. `session_id=None` →
scratchpad disabled (backward compat).

## Trust boundary

- `runner.py` continues to NOT import `ScratchpadStore`/`Note`/`PlanStep`/`ScratchpadAudit`
- Verified by `test_runner_does_not_import_scratchpad` (mirror
  `test_runner_does_not_import_router_classifier` pattern)
- Все scratchpad модули DI'd через factory callable
- Fail-open: factory exception → `logger.warning` + `scratchpad=None` (chat loop продолжает работать)

## Lessons learned

1. **Pydantic v2 extra fields silently dropped** — но в нашем случае
   settings `Settings(BaseSettings, extra="ignore")` нормально
   принимает новые поля, т.к. это первое появление в `Settings`.
   Pattern: `getattr(settings, "new_field", default)` — НЕ нужен
   на Step 0 (новые поля добавляются в одном коммите), но нужен
   в будущих шагах при добавлении в существующий v1.2.0 Settings.
2. **Mutable dataclass для ID assignment** — `@dataclass(slots=True)`
   без `frozen=True`, чтобы `insert()` мог мутировать
   `record.id` после INSERT (mirror `CompactRecord`).
3. **L0 cap auto-prune требует FIFO order** — `ORDER BY created_at
   ASC, id ASC` для tiebreak. Без `id` tiebreak — две ноты с
   одинаковым `created_at` (sub-millisecond) дают
   недетерминированный prune.
4. **Async generator for AgentLoop stub in tests** — `loop.run()`
   это async generator, не coroutine. `_NoopLoop.run` с
   `if False: yield None` — паттерн для тестов.
5. **Factory-DI per-(spec, session_id)** — единая factory signature
   `Callable[[AgentSpec, str | None], Any]`. Type hints в
   `TYPE_CHECKING` блоке — runner.py не импортирует scratchpad
   модуль, но mypy/IDE понимают сигнатуру.

## Out of scope (Phase 3 v1.2.1+ / v1.3.0+)

- System prompt injection of L0 (нужны изменения в `AgentLoop` /
  `build_system_prompt_for`) — v1.2.1
- L2 dense+BM25 retrieval через `OnnxEmbedder` + `DenseRetriever` —
  v1.3.0
- Cross-session handoff через L2 (continuity) — v1.3.0
- Auto-promote L1 → L2 на size threshold — v1.3.0
- HTTP endpoints `/api/v1/context/...` — Phase 4
- Prometheus counters для scratchpad events — Phase 4
- Audit log rotation (currently append-only) — Phase 4

## Файлы

**7 NEW:**
- `harness/agents/scratchpad.py` (95 LoC)
- `harness/agents/scratchpad_store.py` (430 LoC)
- `harness/context/scratchpad_audit.py` (85 LoC)
- `tests/test_scratchpad.py` (290 LoC, 17 tests)
- `tests/test_scratchpad_tools.py` (230 LoC, 10 tests)
- `tests/test_runner_scratchpad_factory.py` (200 LoC, 6 tests)
- `tests/test_cli_context.py` (180 LoC, 7 tests)
- `tests/test_phase3_v1_2_integration.py` (160 LoC, 5 tests)

**4 MODIFIED:**
- `harness/config.py` (+46 LoC, 4 settings)
- `harness/server/agent/tools.py` (+98 LoC, 4 tool schemas)
- `harness/server/agent/runtime.py` (+253 LoC, 4 methods + Literal +
  `__init__` kwargs)
- `harness/agents/runner.py` (+77 LoC, factory kwarg + session_id
  threading + denylist)
- `harness/cli.py` (+201 LoC, 3 subcommands + dispatcher)
- `tests/test_agent_runner.py` (+29 LoC, trust boundary test)
- `docs/CHANGELOG.md` (Phase 3 v1.2.0 section)
- `docs/PHASE3-write.md` (this file)

**2 EXTERNAL SYNCED:**
- `C:\MyAI\_output\2026-06\12.06 Harness-Claude-Code-Architecture\roadmap.md`
  (Phase 3 v1.2.0 row → done, 6/12 closed)
- Annotated tag `v1.2.0`
