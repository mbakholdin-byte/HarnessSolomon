# Changelog — Solomon Harness

## Phase 2.1 — Sub-agents v1.1 (2026-06-14)

### 5 шагов / 5 коммитов за ~2.5 часа (post-Phase 2.0, единая сессия)

| # | Шаг | Коммит | Что | +Tests |
|---|-----|--------|-----|--------|
| 0 | Prerequisites | `4ca72e7` | 4 cascade settings (`subagent_t1_model`, `subagent_t2_model`, `subagent_confidence_high`, `subagent_confidence_low`) + `model_validator` guard `low < high` + CHANGELOG Phase 2.1 section + 2 conftest fixtures (`memory_namespace`, `cascade_decision`) | 0 |
| 1 | Cost-aware cascade | `f9358f9` | `harness/agents/cascade.py` (`TierSelector` + `CascadeDecision`, pure function, fallback-forces-T3, T1-disabled degrades to T2) + `AgentRunner.run(model_override=...)` + `RouterDecision.tier` field (observability) | 27 |
| 2 | Background mode | `47f1ee6` | `harness/agents/jobs.py` (`JobStore` SQLite aiosqlite, `merge_jobs` + `merge_events` tables, `recover_running()`) + `MergeQueue.enqueue_async/subscribe/get_status` + CLI `--background` + `agents jobs <id>` / `--recent N` | 27 (21 JobStore + 6 async queue) |
| 3 | Memory namespacing | `84f1133` | `UnifiedMemory(agent_id=...)` propagates namespace to 4 adapters + `write()` auto-injects `metadata["agent_id"]` + `#agent/<id>` tag + provenance hop + `AgentSpec.memory_namespace` field + `AgentRunner.unified_memory_factory` | 22 |
| 4 | Docs + integration | (this commit) | `docs/subagents.md` — 3 новые секции (cascade, background, namespacing) + `docs/CHANGELOG.md` closeout + `harness/agents/__init__.py` public API + `harness/cli.py` `--background`/`--cascade` flags + `agents jobs` subcommand | 8 (CLI) |

### Метрики (на 14.06.2026, end of Phase 2.1)

- **Tests:** 370 (Phase 2.0 end) + 27 + 27 + 22 + 8 = **454 mock** + 5 real_llm (no change)
- **Production:** 3 новых файла (`cascade.py`, `jobs.py`, `agents/__init__.py` обновлён) + 5 modified (`config.py`, `router.py`, `runner.py`, `merge_queue.py`, `unified.py`, `spec.py`, `cli.py`) — ~1100 LoC net new
- **Settings:** добавлено 4 cascade поля + 1 model_validator
- **Build deps:** 0 (no aiosqlite — already in Phase 0; no new SQLAlchemy/peewee/etc.)
- **Backward compat:** все 4 built-in работают как в Phase 2.0 (default `MiniMax-M2.7` + namespace `"solomon"`); `MergeQueue.enqueue()` (await-to-completion) сохранён как sync-обёртка
- **Tag:** v0.4.0 (annotated, pushed)

### Architecture decisions (Phase 2.1)

- **`TierSelector` = pure function, no LLM calls** — thresholds + model ids в конструкторе; unit-testable без моков.
- **`AgentRunner.model_override` параметр, не spec-mutation** — `AgentSpec` остаётся frozen; cascade choice применяется per-call без риска гонки.
- **Storage-level isolation для namespace, а не фильтр в `search()`** — каждый `UnifiedMemory(agent_id=...)` пишет в свой `Path(file_dir)/<id>` и свою SQLite базу; cross-namespace утечка невозможна по построению.
- **`UnifiedMemory.write()` auto-inject**, не strict — explicit `metadata["agent_id"]` не перезаписывается, explicit tag не дублируется, `#agent/solomon` НЕ добавляется (backward compat).
- **`JobStore` отдельная таблица от `sessions`** — избегаем зависимости `harness.agents` → `harness.server.db` (trust boundary Phase 2.0).
- **CLI uses `DB_PATH` env var для изоляции тестов** — `BaseSettings` уже читает env, monkeypatch не пробрасывается в subprocess.

### Готово (Phase 2.1)

- [x] Cost-aware T1→T2→T3 cascade (TierSelector + model_override)
- [x] Persistent background mode (JobStore + enqueue_async + subscribe)
- [x] `recover_running()` для resume после рестарта
- [x] Per-agent memory namespacing (UnifiedMemory.agent_id + AgentSpec.memory_namespace)
- [x] CLI `--background` + `agents jobs <id>` / `--recent N`
- [x] CLI `--cascade` (mock-mode с confidence=0.95)
- [x] `docs/subagents.md` 3 новые секции

### Что осталось до Фазы 2.2

- Real GitHub PR integration (заменяет in-process `git merge --ff-only`)
- Parallel cross-repo merge queue (отдельный `asyncio.Lock` per repo)
- Cascade calibration via Phase 5 eval harness
- Auto-migration script для старых memory entries (Phase 2.1.1 follow-up)

### Известные ограничения (Phase 2.1)

- CLI `--background` запускает task в `asyncio.run` lifecycle — на завершение нужен FastAPI worker (background mode задуман для server path)
- Cascade thresholds `0.85` / `0.55` — educated guess, calibration в Phase 5
- `UnifiedMemory` namespace isolation работает только для **новых** записей; старые entries в `<file_dir>/` (без subdirectory) остаются в `solomon` namespace
- `recover_running()` маркит in-flight как `cancelled` (не re-enqueue); ручной resume через `enqueue_async(job_id_с_тем_же_worktree_id)`

## Phase 2.0 — Sub-agents v1.0 (2026-06-14)

### 8 шагов / 8 коммитов за ~3 часа (post-Phase 1, единая сессия)

| # | Шаг | Коммит | Что |
|---|-----|--------|-----|
| 0 | Prerequisites | `fcff4d9` | `harness/cli.py` (заполняет dead `harness = "harness.cli:main"` скрипт), `__main__.py` shim, `.harness/agents/` scaffold, Phase 1 retrospective в CHANGELOG, Settings.subagent_* поля |
| 1 | AgentSpec + frontmatter | `c443403` | `harness/agents/spec.py` — Pydantic schema + hand-rolled YAML reader, `extra="forbid"`, no PyYAML dep — 46 tests |
| 2 | Built-in agents + registry | `3af1de8` | `harness/agents/builtin/{explore,plan,code,review}.md` + `registry.py` с importlib.resources + override-логика — 25 tests |
| 3 | WorktreeSession | `64fb24a` | `harness/agents/worktree.py` — async ctx mgr, crash-safe, idempotency, branch orphan recovery + delete_branch() — 17 tests + 2 conftest fixtures |
| 4 | AgentRunner | `4c73aa1` | `harness/agents/runner.py` — composition point, TOOL_SCHEMAS filter, perms denylist proxy, `external_worktree=` для merge queue — 28 tests |
| 5 | conftest fixtures | (в Step 3) | `git_repo`, `agents_dir` |
| 6 | Router + adversarial verify | `42a17bb` | `harness/agents/router.py` (LLM-as-router, fallback chain) + `verify.py` (N-judge majority, 2-judge unanimous) — 19 + 26 = 45 tests |
| 7 | Merge queue + docs | `7d4d655` | `harness/agents/merge_queue.py` (code → review → verify → ff-merge, asyncio.Lock, timeout), `docs/subagents.md` — 9 tests |

### Метрики (на 14.06.2026, end of Phase 2.0)

- **Tests:** 200 (Phase 1 end) + 46 + 25 + 17 + 28 + 0 + 45 + 9 = **370 mock** + 5 real_llm (no change)
- **Production:** 8 новых файлов в `harness/agents/` (spec, registry, worktree, runner, router, verify, merge_queue + 4 builtin .md) + 1 doc — ~2200 LoC
- **Settings:** добавлено 4 sub-agent поля (agents_dir, subagent_default_model, subagent_judges, subagent_timeout_s)
- **Build deps:** 0 (no gitpython, no pyyaml, no python-frontmatter)
- **Static guarantee (verified by tests):** runner.py не импортирует LLMRouterClassifier / MergeQueue / AdversarialVerify / registry

### Architecture decisions

- **MiniMax M2.7 для всех 4 built-in** — quality first, cost cascade в Phase 2.1
- **Реальный `git worktree`** для всех 4 (на Windows 11 + Git 2.53 поддерживается нативно)
- **WorktreeSession lifecycle**: branch удаляется **только** explicit через `delete_branch()`; merge queue делает это после успешного merge. На crash — orphan branch восстанавливается через `_delete_orphan_branch_if_exists()` в `__aenter__`.
- **Permissions enforcement** на 2 уровнях: schema-level (`read-only` + write tools → reject) + runtime-level (denied proxy short-circuits tool execution).
- **2/3 majority** с relaxation для even panel: 2-judge → unanimous, 3+ → majority.

### Готово (Phase 2.0)

- [x] 4 built-in agents: explore / plan / code / review
- [x] Custom agents через `.harness/agents/<name>.md`
- [x] Real `git worktree` isolation, crash-safe
- [x] LLM-as-router (LLMRouterClassifier) + fallback chain
- [x] Adversarial verify (2/3 majority, 1-5 judges)
- [x] In-process merge queue (code → review → verify → ff-merge)
- [x] `docs/subagents.md` с 4 секциями (built-ins, custom, worktrees, verify)
- [x] `python -m harness agents list / run` functional
- [x] CLI `agents run` с `--no-worktree`, `--repo`, `--worktree-id` опциями

### Что осталось до Фазы 2.1

- Cost-aware T1→T2→T3 cascade (роутер уже возвращает confidence)
- Persistent background mode + progress reporting
- Per-agent memory namespacing в UnifiedMemory
- Hot-reload `.harness/agents/*.md` через file-watcher (Phase 4)
- MemPalaceAdapter for L2.5 (отдельный трек)

### Известные ограничения (Phase 2.0)

- T1→T3 cascade = stub (всегда MiniMax M2.7)
- Background mode = await-to-completion
- Merge queue = single-repo, serialised by Lock
- Нет GitHub PR integration (только in-process ff-merge)

---

## Phase 1 — 4-layer memory (2026-06-14)

### 7 шагов за ~1 день (вторая половина 14.06.2026, post-compact)

| # | Шаг | Коммит | Что |
|---|-----|--------|-----|
| 1 | Memory schema (Pydantic) | `4ac2c64` | `Memory` + 5 layers (L1–L2.5–L3–L4) + 6 sources + provenance chain (FIFO 8) — 21 tests |
| 2 | hmem adapter (L1) | `f6a25b3` | JSONL per agent, prefix-coded — 14 tests |
| 3 | mem0 adapter (L2) | `8a71a50` | per-user semantic, upsert + scored search — 14 tests |
| 4 | hybrid adapter (L3) | `dbea05b` | SQLite per project, recent/tail + delete — 15 tests |
| 5 | file adapter (L4) | `9c29e22` | Markdown + INDEX.md, hidden HTML-коммент для metadata — 16 tests |
| 6 | UnifiedMemory facade | `6f9f1fb` | dual-write policy, `_safe_write` для mirrors — 14 tests |
| 7 | retrieval pipeline | `e3424d3` | BM25 (pure-Python) + IdentityReranker + ContextAssembler — 16 tests |

### Метрики (на 14.06.2026, end of Phase 1)

- **Tests:** 110 новых (200 mock total + 5 real_llm = 205/205) — 7 новых test-файлов
- **Production:** 9 файлов в `harness/memory/` (schema, unified, 4 adapters, 4 retrieval) — ~1900 LoC
- **Pluggable:** BM25 retriever + IdentityReranker — Phase 2 swap-in для Qdrant + bge-reranker-v2-m3
- **Dual-write policy default:** primary=L2 (mem0), mirrors=[L3, L4], L1 — отдельный override
- **Tag:** `v0.2.0` (annotated, pushed)

### Решения (Phase 1)

- **2026-06-14** — Pure-Python BM25 (k1=1.5, b=0.75) вместо rank_bm25: меньше deps, корректный unicode tokeniser (`re.findall(r"[\w]+", text, re.UNICODE)`).
- **2026-06-14** — L2.5 (mempalace KG) = placeholder → fallback на mem0. TODO Phase 2.1+.
- **2026-06-14** — File adapter metadata через hidden HTML-коммент `<!-- memory-metadata: {...} -->`: hand-rolled YAML не справлялся с nested JSON.
- **2026-06-14** — Sub-agent-of-sub-agent ЗАПРЕЩЁН на уровне design (architecture.md:86). Реализуется в Phase 2 через import-level trust boundary.

### Что готово (Phase 1)

- [x] 4-слойная память: hmem, mem0, hybrid (SQLite), file (Markdown)
- [x] Unified facade с dual-write policy
- [x] Pluggable retrieval: BM25 → rerank → assemble
- [x] ContextAssembler с char-budget (default 4KB) + truncation marker
- [x] Provenance chain (FIFO 8 hops)
- [x] v0.2.0 published

### Что осталось до Фазы 2

- [ ] Sub-agent system (Step 1–7) — **текущая фаза**

---

## Phase 0 — Web MVP (2026-06-14)

### 11 шагов за ~3 дня (12–14.06.2026)

| # | Шаг | Коммит | Что |
|---|-----|--------|-----|
| 1 | Backend skeleton | `ed1a44f` | FastAPI + health endpoint |
| 2 | SQLite + Pydantic | `f1359f1` | JSONL + aiosqlite |
| 3 | Sessions REST | `8644186` | CRUD endpoints |
| 4 | Tools + safety | `b3de7fc` | 6 tools, deny patterns, path sandbox |
| 5 | LiteLLM router | `83e99a4` | 3 models, /api/models |
| 6 | Agent loop | `3dbcef8` | async generator, max 5 iters |
| 7 | WebSocket chat | `8e9aa5d` | /api/chat/ws + tests |
| 8 | Smoke tests | `26ec994` | 5 e2e scenarios (mock + real_llm marker) |
| 9 | Frontend scaffold | `8ebdef1` | Vite + React + TS |
| 10 | Chat UI | `be57506` | components + real WS client |
| 11 | Quickstart + docs | `aed8aac` | quickstart, architecture, README, CHANGELOG |
| — | Port fix | `2223742` | 8000 → 8765 (hns conflict) |
| — | Tests refactor | `e482c02`, `aad4dc4` | unused imports, receive_json loop |

### Метрики (на 14.06.2026)

- **Backend:** 17 Python модулей, ~2540 строк (server/, llm/, db/, agent/, routes/, config, main)
- **Frontend:** 10 TS/TSX файлов, ~1140 строк (App, main, api/{client,ws}, 6 components)
- **Tests:** 67 passed (62 unit + 5 e2e smoke, real_llm отдельно через `-m real_llm`)
- **Stack:** Python 3.12, FastAPI, LiteLLM, aiosqlite, Pydantic v2 / React 18, TypeScript 5, Vite 5
- **Storage:** SQLite (index) + JSONL (source of truth), rebuild при старте
- **E2E latency:** WebSocket roundtrip через Vite proxy <100ms (без LLM)

### Что готово

- [x] REST API: health, models, sessions CRUD, messages
- [x] WebSocket chat: streaming tokens, tool_call/tool_result events
- [x] 6 tools: read_file, write_file, edit_file, bash, grep, glob
- [x] Safety: deny-patterns для bash, path-scope под project_root
- [x] Agent loop: max 5 итераций, async generator
- [x] 3 LLM провайдера: MiniMax-M2.7, GLM-4.7, Moonshot-v1-128k
- [x] Frontend: 2-колоночный layout (sessions слева, chat справа)
- [x] Tool call cards в UI
- [x] Quickstart: <10 минут от clone до первого ответа

### Что осталось до Фазы 1

- [ ] Tag `v0.1.0` и push в GitHub
- [ ] Real LLM smoke tests (с правильным provider prefix в litellm)
- [ ] Скриншот UI в `docs/images/` (ручная работа Марка)

### Решения (decisions)

- **2026-06-13** — Порт 8000 → 8765: на Windows 11 + Docker Desktop порт 8000 зарезервирован hns (WSAEACCES). Commit `2223742`.
- **2026-06-13** — Backend-first: сначала API + smoke tests на mock LLM, потом UI.
- **2026-06-13** — JSONL = source of truth, SQLite = индекс. Rebuild при старте.
- **2026-06-13** — Cloud-only LLM в Фазе 0. Локальные (Qwen3-8B) — в Фазе 0.5.

### Файлы документации (Phase 0)

- `README.md` — обзор + статус Фазы 0
- `docs/quickstart.md` — <10 мин от clone до ответа
- `docs/architecture.md` — секция "Phase 0 Web MVP" (добавлена в Step 11)
- `harness/README.md` — структура модуля, endpoints, env vars
- `docs/PHASE-0-SPEC.md` — спецификация (утверждена 13.06.2026)
- `docs/PHASE-0-PLAN.md` — план из 11 шагов
- `docs/roadmap.md` — Фазы 1-5
- `docs/MODEL_REGISTRY.md` — каталог моделей T1/T2/T3
- `docs/CHANGELOG.md` — этот файл
