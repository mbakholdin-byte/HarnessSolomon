# Changelog — Solomon Harness

## Phase 1.6 — Scope-gated API v1.0 (Steps 0-3 / 6, in progress, 2026-06-14)

### Step 0 — Token store + scopes enum + settings (commit `eff5725`)

| # | Что | Файлы | +Tests |
|---|-----|-------|--------|
| Step 0 | `harness/server/auth/{__init__,scopes,tokens,db}.py` — `Scope` enum (6 значений), `parse_scopes` / `has_scope` / `format_scopes`, `TokenStore` (aiosqlite, SHA-256 hashed), `TokenRecord` (frozen dataclass) | NEW: 4 файла (~530 LoC), `harness/config.py` +4 settings, `harness/server/app.py` lifespan wiring, `tests/conftest.py` `auth_store` + `make_token` fixtures, `tests/test_token_store.py` (NEW, ~190 LoC) | 8 (scopes) + 6 (token store) = 14 |

### Step 1 — FastAPI deps (`get_current_token`, `require_scope`) (commit `4d30871`)

| # | Что | Файлы | +Tests |
|---|-----|-------|--------|
| Step 1 | `harness/server/auth/deps.py` (NEW, ~155 LoC) — `get_token_store` (503 on missing), `get_current_token` (401 on missing/malformed/wrong/revoked), `require_scope(*required)` factory (403 with informative detail on missing scope, ANY match, 401 bubbles up); `auth_required=False` short-circuits both deps for dev mode | NEW: `deps.py` + `tests/test_auth_deps.py` (~290 LoC, 13 tests) | 13 |

### Step 2 — Capabilities endpoint + apply to /api/v1/agents (commit `3f30bf0`)

| # | Что | Файлы | +Tests |
|---|-----|-------|--------|
| Step 2 | `harness/server/auth/route_registry.py` (NEW, ~110 LoC) — `EndpointSpec` dataclass + `collect_endpoints(app)` walks `app.routes`, finds `require_scope` deps via `_required_scopes` marker attribute; `harness/server/routes/capabilities.py` (NEW, ~70 LoC) — `GET /api/v1/capabilities` (public, returns server_version, auth_required, scopes_available, endpoints); `harness/server/routes/agents_jobs.py` — `Depends(_agents_read)` на всех 3 GET routes; `harness/server/app.py` — mount `capabilities_router` with `/api/v1` prefix | NEW: 2 файла, MODIFIED: `agents_jobs.py` + `app.py` + `tests/test_agents_api.py` (Phase 2.2 baseline fix), `tests/test_capabilities.py` (NEW, 9 tests) | 9 |

### Step 3 — CLI `harness auth` subcommand + bootstrap (this commit)

| # | Что | Файлы | +Tests |
|---|-----|-------|--------|
| Step 3 | `harness/cli.py` — `auth` subparser (create/list/revoke/whoami/test), 5 handlers, `_dispatch_auth` runs bootstrap только для read-only commands, `_bootstrap_admin_token_if_needed` mints `bootstrap-admin` с ALL_SCOPES при `auth_required=True` И `len(list_active)==0`; `--bootstrap` flag для admin tokens; revoke supports hash (64 hex) OR label; `whoami` debug; `test` smoke against local server; **stdout reconfigure UTF-8** для Windows compat; **ASCII `...`** вместо `…` для subprocess piping | MODIFIED: `cli.py` +6 subparser + 5 handlers (~280 LoC), NEW: `tests/test_cli_auth.py` (~340 LoC, 18 tests) | 18 |

**Settings added (Phase 1.6):**
- `auth_db_path: Path` — `data/harness-scope.db` (sibling of `agent-jobs.db`)
- `auth_token_bytes: int = 32` — 256 bits of entropy
- `auth_default_scopes: str = ""` — CLI fallback when `--scopes` is omitted
- `auth_required: bool = True` — master switch (dev mode = False)

**Scope enum (6 значений):**
- `agents.read`, `agents.write`, `agents.pr` (Phase 2.3+ routes)
- `memory.read`, `memory.write`
- `sessions.read`

**Architecture decisions (Step 0):**
- **SQLite aiosqlite store** — persistent, multi-tenant, no new deps (aiosqlite уже в Phase 0)
- **SHA-256 хэш, не bcrypt/argon2** — у нас opaque tokens с 256 бит энтропии (32 random bytes), pre-image resistance не нужна; SHA-256 fixed 64-char column → tight indexes; fast `lookup()` (важно для per-request auth check)
- **`secrets.token_urlsafe(32)`** — 43-char URL-safe plaintext (без padding); default `auth_token_bytes=32` = 256 bits
- **Plaintext shown ONCE** — at `create()` time; never persisted, never logged, never returned by `list_active()`
- **`has_scope` = ANY match** — token со scope A может вызвать endpoint, требующий A OR B; "kitchen sink" semantics избегаем
- **`_reset_init_flag()` test helper** — needed because the init flag is process-level (path-keyed init добавляет сложности для unit-тестов)
- **`auth_required` master switch** — позволяет test suite + dev mode работать **без** токенов; `auth_required=True` в prod
- **Default `auth_default_scopes=""`** — empty token requires explicit `--scopes`; `bootstrap-admin` token (Step 3) — единственный путь к ALL_SCOPES

**Out of scope (Step 0):** FastAPI deps (Step 1), `GET /api/v1/capabilities` (Step 2), `harness auth` CLI (Step 3), `memory_v1` + `sessions_v1` routes (Step 4), `POST /api/v1/agents/jobs` (Step 5).

**Tag at end of Phase 1.6:** v0.6.0

## Phase 2.2 — Sub-agents v1.2: GitHub PR + parallel cross-repo queue (ЗАКРЫТО v0.5.0, 2026-06-14)

### 5 шагов / 5 коммитов за ~2.5 часа (post-Phase 2.1, единая сессия)

| # | Шаг | Коммит | Что | +Tests |
|---|-----|--------|-----|--------|
| 0 | Prerequisites | `125dbde` | 5 PR fields (`repo`, `pr_url`, `pr_number`, `target_branch`, `pr_mode`) + 5 PR-phase statuses (`pr_creating`, `pr_open`, `pr_waiting_checks`, `pr_waiting_review`, `merging_pr`) + `ALTER TABLE` migration + 5 PR settings + `gh_subprocess_stub` fixture | 7 |
| 1 | Per-repo Lock registry | `92ff3f7` | `harness/agents/repo_locks.py` (NEW) — `RepoLockRegistry` keyed by `str(Path(repo).resolve())`, guards per-repo `asyncio.Lock` + insertion guard; `MergeQueue._lock` replaced with `self._locks` registry + back-compat alias; per-repo serialisation in `enqueue` and `_run_job_async` | 11 |
| 2 | gh CLI wrapper | `2dd594c` | `harness/agents/pr_integration.py` (NEW) — `GHUnavailable`, Pydantic `PRCreateResult`/`PRStatus`/`PRMergeResult`, module-level `_gh` injection point, `check_gh_available` / `create_pr` / `get_pr_status` / `wait_for_checks` / `merge_pr` via `asyncio.create_subprocess_exec` | 20 |
| 3 | PR lifecycle in MergeQueue | `9b4d46b` | `MergeJob` +`pr_mode`/`pr_target_branch`/`repo_override`, `MergeResult` +`pr_url`/`pr_number`/`pr_skipped`, `_run_pr_phase()` (pr_creating→pr_open→pr_waiting_checks→merging_pr→merged), GHUnavailable fallback на local ff-merge при `pr_strategy='auto'`, `recover_running()` catches new PR-phase statuses | 12 |
| 4 | CLI + FastAPI + docs | (this commit) | CLI: `--pr`/`--pr-draft`/`--pr-ready`/`--pr-target` flags, `--pr` без `--background` → exit 2, dedup `_cmd_agents`, расширенный `_cmd_agents_jobs` output с PR-колонками; FastAPI: lifespan JobStore + MergeQueue singleton + новый router `/api/v1/agents/jobs/{id}` + list + health; `docs/merge-queue.md` (NEW, 250 строк) | 14 (7 CLI + 7 API) |

### Метрики (на 14.06.2026, end of Phase 2.2)

- **Tests:** 454 (Phase 2.1 end) + 7 + 11 + 20 + 12 + 14 = **518 mock** + 5 real_llm
- **Production:** 12 новых/изменённых файлов (`pr_integration.py`, `repo_locks.py`, `routes/agents_jobs.py` — new; `merge_queue.py`, `jobs.py`, `config.py`, `cli.py`, `app.py`, `subagents.md` — modified; `docs/merge-queue.md` — new) — ~1600 LoC net new
- **Settings:** +5 (PR strategy + PR defaults)
- **Built-in .md:** 0 (sub-agent surface unchanged)
- **CLI subcommands:** 3 (list/run/jobs)
- **CLI flags:** 7 (Phase 2.1) + 4 (Phase 2.2: --pr, --pr-draft, --pr-ready, --pr-target) = 11
- **HTTP routes:** +3 (`/api/v1/agents/jobs/{id}`, `/jobs?recent=N`, `/health`)
- **New deps:** 0 (gh CLI binary assumed on host; aiosqlite/pydantic/fastapi from Phase 0-1)
- **Backward compat:** all 4 built-in agents работают без `gh` installed; legacy JobStore DBs migrated via `ALTER TABLE` (idempotent); `self._lock` alias kept on `MergeQueue` for Phase 2.1 callers
- **Tag:** v0.5.0 (annotated, pushed)

### Architecture decisions (Phase 2.2)

- **`gh` CLI вместо `PyGithub`** — 0 new deps; token via `env=` (не argv); авторизация через `gh auth status` + `GITHUB_TOKEN` env var.
- **`RepoLockRegistry` keyed by `Path.resolve()`** — симлинки/relative paths нормализуются; insertion guard защищает от race в asyncio single-thread.
- **PR-ветка в `_run_pr_phase()` helper, не в `_run_job_async`** — Phase 2.1 sync/async duplication pattern preserved; тестируется изолированно.
- **`pr_strategy="auto"` fallback на local ff-merge** — local dev без `gh` работает, не блокирует flow.
- **`--pr` БЕЗ `--background` → exit 2** — sync path не может `await` PR lifecycle (CI polls, `wait_for_checks`); явная ошибка лучше silent fallback.
- **Schema migration через `PRAGMA table_info`** — не полагаемся на SQLite 3.35+ `IF NOT EXISTS` для `ADD COLUMN`; каждая колонка проверяется отдельно.
- **FastAPI wiring в lifespan** — JobStore + MergeQueue singleton; при отсутствии LLM API keys — `app.state.merge_queue = None`, routes возвращают 503, остальной сервер работает.
- **`MergeJob.repo_override`** — per-job override для cross-repo parallelism; default = `self.runner.repo` (Phase 2.1 single-repo backward compat).

### Готово (Phase 2.2)

- [x] PR открывается автоматически после успешного code+review (`pr_mode="draft"` + happy `gh`)
- [x] Merge queue ждёт CI checks (`statusCheckRollup.state` polling) + auto-merges при success
- [x] 2+ репо обрабатываются параллельно через `RepoLockRegistry` (Step 1 stress test + Step 3 test_concurrent_jobs_on_different_repos)
- [x] Merge failure: branch preserved, `status=failed`, `error` populated, `pr_url` сохраняется в store
- [x] `pr_strategy="auto"` fallback на local merge при отсутствии `gh` / remote
- [x] Все 4 built-in agents работают без `gh` installed (backward compat)
- [x] CLI `agents run --pr` БЕЗ `--background` → exit 2 с понятной ошибкой
- [x] FastAPI: `GET /api/v1/agents/jobs/<id>` returns 200/404 + `GET /api/v1/agents/jobs?recent=N` lists + `GET /api/v1/agents/health`
- [x] CLI `agents jobs` output включает `pr_url`, `pr_number`, `repo`, `pr_mode` columns when present
- [x] `docs/merge-queue.md` создан, покрывает все секции (CLI, settings, status table 13 значений, per-repo locks, HTTP API, gh auth troubleshooting)
- [x] 0 new deps (`git diff pyproject.toml` пуст)
- [x] Trust boundary preserved (`grep -rn "from harness.server" harness/agents/` пуст)

### Что осталось до Фазы 2.3

- Webhook receiver для inbound PR events (`POST /api/v1/agents/webhooks/github`)
- Auto-merge labels (branch protection + `gh pr merge --auto`)
- PR review templating (CODEOWNERS-aware reviewers, issue-link auto-resolution)
- Multi-PR-per-job / stacked PRs
- Multi-tenant `gh` config (multiple users с разными GitHub identities)
- Rich PR UI в Web frontend (clickable `pr_url`, status badges)
- Cross-PR dependency tracking
- `gh` rate limit handling (GitHub API rate limit, automatic backoff)

### Известные ограничения (Phase 2.2)

- CLI `--background` запускает task в `asyncio.run` lifecycle — на завершение нужен FastAPI worker (это работает через `GET /api/v1/agents/jobs/<id>`)
- Cascade thresholds `0.85` / `0.55` — educated guess, calibration в Phase 5
- `UnifiedMemory` namespace isolation работает только для **новых** записей; старые entries в `<file_dir>/` (без subdirectory) остаются в `solomon` namespace (Phase 2.1.1 follow-up)
- `recover_running()` маркит in-flight как `cancelled` (Phase 2.1 behaviour preserved); ручной resume через `enqueue_async(job_id_с_тем_же_worktree_id)`
- `pr_strategy="auto"` + transient network blip: `check_gh_available` срабатывает один раз, transient во время `gh pr create` → `failed` (не silent fallback)
- `gh` polling interval (`pr_poll_interval_s=15`) жёстко лимитирует скорость реакции на CI changes; webhook receiver в Phase 2.3 снимет это

---

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
