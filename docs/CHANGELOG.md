# Changelog — Solomon Harness

## Phase 3 v1.3.0 — Select + Compress (ЗАКРЫТО v1.3.0, 2026-06-15)

**Phase 3 v1.3.0 — 4 шага / 4 коммита / +48 net new тестов (1098 → 1146) / 0 new required deps / 0 breaking changes**

### Что закрыто

- **L2 vector store (Qdrant + SQLite fallback)** — `L2VectorStore` Protocol + `QdrantL2Store` (optional, requires `[memory]` extra) + `SqliteL2Store` (zero-dep fallback). `make_l2_store()` factory с best-effort probe.
- **L2 retrieval (hybrid dense+BM25 RRF)** — `L2Retriever` class. In-memory BM25 + dense через L2VectorStore + RRF fusion (k=60, fetch_k=20).
- **LLM-curator top-K re-rank** — `curated_search(query, top_k, candidate_k, router)` — pull top-50 candidates, ask T1 LLM to score 0-100, re-rank. Curator failure → fall back to plain hybrid.
- **2 new tools** — `scratchpad_l2_search` (hybrid + curator) + `scratchpad_l2_promote_to_l1` (hierarchical summary → write as L1).
- **2 new settings** — `scratchpad_l2_qdrant_url` (default None → SQLite) + `scratchpad_l2_qdrant_collection` (default `scratchpad_l2`).
- **ToolRuntime extension** — 3 new kwargs: `l2_retriever`, `l2_router`, `l2_curator_model`. 2 new methods + Literal updated to 12 names.

### Trust boundary

- `runner.py` continues to NOT import `L2Retriever` / `QdrantL2Store` / `LLMRouter`
- `l2_retriever=None` default в `ToolRuntime` — backward compat
- Fail-open во всех L2 retrieval calls (try/except + logger.warning + return empty/plain hybrid)
- Qdrant probe — best-effort, dead Qdrant → SQLite fallback
- Static test `test_runner_does_not_import_scratchpad` продолжает проходить

### Lessons

1. **str.format() escape с literal JSON** — `{` в примерах JSON парсится
   как format spec. Использовать `.replace("__PH__", value)` для промптов
   с JSON-примерами.
2. **Missing JSON field = skip, не default** — `item.get("score", 0.0)`
   пройдёт range check, но не отражает намерение LLM. Явный
   `if "score" not in item: continue`.
3. **SpyToolRuntime signature sync** — `class X(real_X): __init__` в
   тестах требует ручной синхронизации при добавлении kwarg.
4. **Qdrant optional** — мёртвый Qdrant → SQLite fallback автоматически.
   Без жёстких deps.
5. **Hierarchical summary без отдельного LLM call** — `write_note(level="L1")`
   с bullet-list L2 notes = и есть summary. Note content IS the summary.

### Commits

- `c51d9f6` Step 0 — L2 vector store (Qdrant + SQLite fallback)
- `2ffbdba` Step 1 — L2 retrieval (BM25 + dense hybrid RRF)
- `ed12a95` Step 2 — LLM-curator top-K re-rank
- `2721d69` Step 3 — L2 search + promote-to-L1 tools

### Out of scope (Phase 3 v1.3.1+)

- Tool result offload >25k tokens → v1.3.1
- Cross-session handoff через L2 (continuity) → v1.4.0
- Reflection loop + manual /compact slash → v1.4.0
- Privacy zones + pre-compaction hook → v1.5.0
- HTTP endpoints `/api/v1/context/search` → Phase 4
- Prometheus counters для L2 events → Phase 4

## Phase 3 v1.2.1 — L0 → system prompt injection (ЗАКРЫТО v1.2.1, 2026-06-15)

**Phase 3 v1.2.1 — 3 шага / 3 коммита / +50 net new тестов / 0 new required deps / 0 breaking changes**

### Что закрыто

- **L0 → system prompt** — hot context (notes уровня L0) автоматически
  инжектится в system message на каждом turn, чтобы LLM видела
  горячие факты/план/состояние без round-trip `scratchpad_read_notes`.
- **Composition (двойная защита)** — `build_system_prompt_for()` принимает
  `l0_section=`, `AgentLoop.run()` также применяет его через
  `runtime._l0_section` (defence in depth для прямых вызовов).
- **Setting** — `scratchpad_inject_l0_to_system_prompt: bool = True`
  (default ON, opt-out).

### Trust boundary

- `runner.py` continues to NOT import `ScratchpadStore` / `Note` /
  `NoteLevel` — verified by `test_runner_does_not_import_scratchpad`
  (v1.2.0 static test, unchanged)
- L0 fetch через `await scratchpad.read_notes("L0", limit=50)` —
  store accepts str OR `NoteLevel`
- `loop.py` НЕ импортирует scratchpad модули — доступ через
  `getattr(self.runtime, "_l0_section", None)`
- Fail-open во всех L0 read calls (try/except + logger.warning +
  l0_section=None)

### Lessons

1. **`getattr(runtime, "new_attr", None)` для defence-in-depth** —
   `loop.py` читает `_l0_section` через getattr, чтобы можно было
   конструировать `ToolRuntime` в тестах без поля
2. **Composition через `*` kwargs** — `build_system_prompt_for(spec,
   project_root, tools, *, l0_section=None)` сохраняет backward compat
3. **SpyToolRuntime signature sync** — при добавлении нового kwarg в
   `ToolRuntime.__init__` обновлять сигнатуру в `SpyToolRuntime`
4. **Default ON для hot layer** — `scratchpad_inject_l0_to_system_prompt`
   default True, потому что L0 = hot = "must be visible by default"

### Commits

- `298c51a` Step 0 — L0 helper + setting + runner wiring
- `8dca82b` Step 1 — AgentLoop applies l0_section (defence in depth)
- `9ade7a7` Step 2 — E2E integration + fail-open + setting toggle
- (this commit) Step 3 — Docs + tag v1.2.1

### Out of scope (Phase 3 v1.3.0+)

- L1 injection в system prompt (L1 — per-session plan, не "hot")
- L2 dense+BM25 retrieval → v1.3.0
- Cross-session handoff через L2 → v1.3.0

## Phase 3 v1.2.0 — Write context (ЗАКРЫТО v1.2.0, 2026-06-15)

**Phase 3 v1.2.0 — 5 шагов / 5 коммитов / +44 net new тестов (1032 → 1076) / 0 new required deps / 0 breaking changes**

Реализует **"Write context"** стратегию из Anthropic context-engineing
playbook: persistent per-`(session_id, agent_id)` scratchpad для
заметок и плана задачи.

**Step 0 — Scratchpad module + storage** (`499a6fd`)
- `harness/agents/scratchpad.py` — `Note`, `PlanStep` dataclasses, `NoteLevel` (L0/L1/L2), `PlanStatus` enum
- `harness/agents/scratchpad_store.py` — `ScratchpadStore` (2 tables, WAL + busy_timeout=5000)
- 4 settings: `scratchpad_enabled`, `scratchpad_max_notes_per_session`, `scratchpad_l0_max_bytes`, `scratchpad_audit_log`
- 17 tests (dataclass marshalling + schema + L0 cap + plan basics)

**Step 1 — Tools + audit + denylist** (`39ee284`)
- `harness/context/scratchpad_audit.py` — JSONL mirror (mirror `CompactionAudit`)
- 4 tool schemas в `TOOL_SCHEMAS` (`scratchpad_write_note` / `_read_notes` / `_plan_step` / `_mark_done`)
- 4 `_method` в `ToolRuntime` + extended `ToolName` Literal + `scratchpad` + `scratchpad_audit` kwargs
- `_READ_ONLY_DENY` обновлён: 3 write tools в denylist, `read_notes` остаётся доступным
- 10 tests (schemas + dispatch + fail-open + denylist)

**Step 2 — AgentRunner factory + session_id threading** (`42bd0ff`)
- `scratchpad_factory: Callable[[AgentSpec, str | None], Any] | None = None` kwarg в `AgentRunner.__init__`
- `scratchpad_audit: Any = None` kwarg
- `session_id: str | None = None` kwarg в `run()` / `stream()` / `_drive()` / `_stream_drive()`
- Fail-open в `_drive` / `_stream_drive`: factory exception → `logger.warning` + `scratchpad=None`
- Trust boundary test: `test_runner_does_not_import_scratchpad` (grep-forbidden)
- 6 factory tests + 1 trust boundary test

**Step 3 — CLI + observability** (`d0575db`)
- `harness context {read,write,plan}` subcommand (mirror `_cmd_agents_jobs` style)
- 3 handlers: `_cmd_context_read`, `_cmd_context_write`, `_cmd_context_plan`
- 7 tests (parser + read/write/plan/mark-done/help)

**Step 4 — Docs + tag v1.2.0**
- `docs/PHASE3-write.md` (~330 LoC, 6 sections: Overview / Architecture / Settings / Tools / CLI / Storage / Trust boundary / Lessons / Out of scope / Files)
- `docs/CHANGELOG.md` (this section)
- `_output/.../roadmap.md` (Phase 3 v1.2.0 row → done, 6/12 closed)
- `tests/test_phase3_v1_2_integration.py` (5 e2e tests)
- Annotated tag `v1.2.0`

**Trust boundary preserved**: `runner.py` continues to NOT import
`ScratchpadStore` / `Note` / `PlanStep` / `ScratchpadAudit` (verified
by `test_runner_does_not_import_scratchpad`).

## Phase 3.5 — Persistent Compact Store (ЗАКРЫТО v1.1.0, 2026-06-15)

**Phase 3.5 (v1.1.0) — 4 шага / 4 коммита / +58 net new тестов (968 → 1026) / 0 new required deps / 0 breaking changes**

Расширение Phase 3. Persistent compact cache: на cache hit — summariser LLM call skip, zero cost, instant reconnect.

**Что закрыто:**

1. **Persistent compact store** — `harness/agents/compact_store.py` (NEW, ~200 LoC). SQLite `compact_store` table в существующей `agent-jobs.db` (sibling `merge_jobs`/`merge_events`/`webhook_events`). Keyed on `(session_id, source_hash)`. Auto-versioned per session. WAL + `busy_timeout=5000` для contention с JobStore.
2. **Compactor DI + cache lookup** — `ContextCompactor` принимает `store: CompactStore | None = None` + `session_id` kwarg в `maybe_compact()`. Cache hit → return rebuilt (zero LLM cost). Cache miss → existing slow path + persist.
3. **UnifiedMemory wiring** — закрыт Phase 3 placeholder `app.py:117`. `UnifiedMemory` + `CompactStore` инстанциируются в lifespan и DI'ятся в compactor. Best-effort init (failure → `None`).
4. **Observability** — `harness/context/compaction_audit.py` (NEW, ~70 LoC). `CompactionAudit` с JSONL mirror в `data/audit/compaction-YYYY-MM-DD.ndjson`. Mirrors `RedactionAudit` pattern. Opt-in via `compaction_audit_log=True` (default OFF).
5. **Settings (3 new)** — `compaction_persistent_store` (default True), `compaction_cache_max_versions` (default 5, `ge=1`), `compaction_audit_log` (default False). Validator rejects `cache_max_versions < 1` when `persistent_store=True`.

**Архитектурные решения:**

- **Source hash cache key** — `sha256(json.dumps(messages, sort_keys=True))[:16]`. Новая история → новый hash → автоматическая cache invalidation (no explicit invalidator needed). Collision risk ~2^-64 (negligible).
- **Fail-open** — cache lookup и persist failures логируются и fall through к slow path. Compactor never raises из-за cache. 8 из 25 cache тестов проверяют error paths.
- **Lifespan construction** — `CompactStore(settings.db_path.parent / "agent-jobs.db")`. Тот же файл, что JobStore + WebhookEventStore (sibling tables). WAL mode + `busy_timeout=5000` для contention.
- **Reconstruction** — cache хранит только summary, не полный message list. Reconstruct через `_rebuild_from_cache(messages, cached.summary)` = sliding window + inject summary.
- **Backward compat** — `store=None` default preserves pre-Phase-3.5 in-memory behavior. `session_id` kwarg in `maybe_compact` is keyword-only (backward compat: positional args unchanged).
- **Trust boundary preserved** — `runner.py` continues to NOT import `CompactStore` или `CompactionAudit` (verified by `test_agent_runner.py:516-575`).

**Trust boundary verification (Step 0..4):**

- `runner.py`: 0 top-level imports of new modules (static test passes)
- `merge_queue.py`: 0 top-level imports
- `outbound.py`: 0 top-level imports
- `webhook_handler.py`: 0 top-level imports (Phase 3 redaction sink #9 intact)
- `compaction.py`: `TYPE_CHECKING` import only, runtime uses injected `store`/`audit`
- `app.py`: lazy imports in lifespan only

**Out of scope (Phase 4+):**

- API endpoint `POST /api/v1/sessions/{id}/compact` (manual operator trigger)
- Background worker (cron-style scan for over-threshold sessions)
- Cross-session handoff через L2 (continuity across sessions)
- Pruning implementation для `compaction_cache_max_versions`
- Prometheus counters для cache hit rate
- Audit log rotation (currently append-only)
- Compaction policy DSL (per-session settings override)
- Compaction replay/rollback UI

**Step 0 — CompactStore module** (`5a6fe6b`)
- `harness/agents/compact_store.py` (NEW)
- `tests/test_compact_store.py` (25 tests)
- Schema migration idempotency, lookup/insert/list_for_session/count
- 993 → 968+25 = 993 passed, 0 regressions

**Step 1 — Compactor DI + cache lookup** (`f9a5d0a`)
- `harness/context/compaction.py`: `store=` param, `_source_hash`, `_rebuild_from_cache`, `_persist_compact`
- `harness/server/agent/session.py`: pass `session_id=self.session_id` в `maybe_compact`
- `tests/test_compactor_cache.py` (12 tests): cache hit/miss, source_hash determinism, persistent_store=False, lookup/persist errors, session_id kwargs, rebuild
- 993 → 1005 passed, 0 regressions

**Step 2 — UnifiedMemory wiring + app.py:117 closure** (`5741dbf`)
- `harness/config.py`: 3 new settings + validator
- `harness/server/app.py`: lifespan instantiates UnifiedMemory + CompactStore, DI в compactor
- `tests/test_phase35_wiring.py` (11 tests): settings defaults/overrides/validation, lifespan integration
- 1005 → 1016 passed, 0 regressions

**Step 3 — Observability + audit** (`122857a`)
- `harness/context/compaction_audit.py` (NEW)
- `harness/context/compaction.py`: audit call sites (cache_hit, run, persist_failed)
- `harness/server/app.py`: instantiate CompactionAudit в lifespan
- `tests/test_compactor_observability.py` (10 tests): structured logs, JSONL audit, fallback to logger
- 1016 → 1026 passed, 0 regressions

**Step 4 — Docs + tag v1.1.0** (TBD)
- `docs/PHASE3.5.md` (NEW, ~250 LoC operator guide)
- `docs/CHANGELOG.md`: this section
- `docs/roadmap.md`: Phase 3.5 → ЗАКРЫТО v1.1.0
- `C:\MyAI\_output\2026-06\12.06 Harness-Claude-Code-Architecture\roadmap.md`: Phase 3.5 row sync
- `C:\Users\mbakh\.claude\projects\C--MyAI\memory\harness-phase-3-5-complete-2026-06-15.md`: full summary
- `MEMORY.md` index: entry added
- Tag `v1.1.0` annotated

---

## Phase 3 — Compaction + Embeddings + Privacy (ЗАКРЫТО v1.0.0, 2026-06-15)

**Phase 3 (v1.0.0) — 4 шага / 4 коммита / +140 net new тестов (822 → 962) / 0 new required deps / 2 new optional deps (`onnxruntime`, `numpy` via `[embeddings]` extra)**

Production milestone. Phase 3 closes three critical production gaps in a single release:
context overflow on long sessions, lexical-only memory search, and PII/secrets leaking to
external sinks (LLM provider, GitHub PR, webhook receivers).

### Шаги

- **Step 0 (commit `phase-3-step-0-foundation`)** — 15 new Pydantic v2 settings (8 compaction + 4 embeddings + 3 privacy); `qwen3:8b` added to `MODELS` catalog (T1, ctx=32768, $0); `harness/redaction/` NEW package (`patterns.py` with 12 stdlib regex — EMAIL, PHONE, IPV4, GITHUB_TOKEN, AWS_ACCESS_KEY, AWS_SECRET, OPENAI_KEY, ANTHROPIC_KEY, ENV_ASSIGNMENT, JWT, PEM_PRIVATE_KEY, SLACK_TOKEN; `engine.py` with `redact/scan/redact_dict` — pure, idempotent; `audit.py` with `RedactionAudit` for JobStore + JSONL mirror). **+40 tests** (`test_redaction.py`, `test_config_phase3.py`).
- **Step 1 (commit `phase-3-step-1-compaction`)** — `harness/context/` NEW package (`compaction.py` with `ContextCompactor` — sliding window + LLM summary, tool-pair preservation, `keep_recent_turns` floor, T1 primary + T2 fallback, JSON serialised for cross-process handoff; `prompts.py` with `SUMMARY_SYSTEM_PROMPT`); insertion into `loop.py:189-197` (after system prompt, before completion) + `session.py:55-106` (on history load); `AgentLoop.__init__(compactor=)` + `ChatSession.__init__(compactor=)` DI; `server/app.py` lifespan instantiates `ContextCompactor` and stores in `app.state.compactor`; `routes/chat.py` picks it up at WS connect. Summary persisted to L2 with tag `#compact`. **+25 tests.**
- **Step 2 (commit `phase-3-step-2-privacy`)** — redaction wired at all 9 sinks: LLM messages (runner + loop), PR title, PR body, commit msg, branch name, JobStore prompt, outbound webhook payload, `read_file` tool output, inbound webhook payload (post-HMAC verify, pre-persistence). `redact_dict` extended to accept lists at top level (OpenAI message lists). **+13 tests.**
- **Step 3 (commit `phase-3-step-3-embeddings`)** — `harness/memory/embeddings/` NEW package (`base.py` Protocol, `onnx_backend.py` lazy-loaded `OnnxEmbedder` for multilingual-e5-small with mean-pooling + L2-normalise + asymmetric `query:` / `passage:` prefixes + `asyncio.Lock` thread-safety, `privacy.py` `PrivacyAwareEmbedder` wrapper); `harness/memory/retrieval/dense.py` `DenseRetriever` (cosine over `metadata.embedding`, filters mismatched `embedding_version`); `harness/memory/retrieval/hybrid.py` `HybridRetriever` (RRF k=60 fusion); `harness/memory/retrieval/versioning.py` `EMBEDDING_MODEL_VERSION` constant; `UnifiedMemory` extended with optional `embedder=` kwarg, `write()` embeds-on-write (best-effort), new `search_scored()` method. **+24 tests.**

### Final metrics

- **Test count**: 822 → 962 mock tests (0 regressions, +140 new)
- **New files**: 14 (context/, redaction/, memory/embeddings/, retrieval/dense,hybrid,versioning, docs/PHASE3.md)
- **Modified files**: 12 (config.py, models.py, loop.py, session.py, runner.py, merge_queue.py, cli.py, outbound.py, webhook_handler.py, runtime.py, unified.py, routes/chat.py, server/app.py, test_models.py)
- **New LoC**: ~1200 production + ~900 tests
- **New required deps**: 0
- **New optional deps**: `onnxruntime>=1.18`, `numpy>=1.26` via `pip install -e ".[embeddings]"`
- **Tag**: `v1.0.0`

### Архитектурные решения (Phase 3)

- **3 одновременных фичи** в одном релизе потому что каждая — critical production gap, и они не конфликтуют (compaction работает на `messages` shape, privacy — на `messages` content, embeddings — на `Memory` storage). Тег v1.0.0 = production milestone.
- **Compactor returns NEW list** — Phase 0 contract: caller passes list in, loop mutates in place; compactor does NOT mutate. Loop rebinds `messages = compactor.maybe_compact(...)` before completion. Sliding window: drop oldest non-system, preserve tool-call ↔ tool-result pairs, `keep_recent_turns` floor.
- **T1 (Qwen3 8B local) summariser** = free + offline-capable + good enough for 200-400-word summary of dropped turns. T2 fallback for fresh installs without Ollama.
- **Privacy default ON** (opt-out) = safe baseline для open-source tool. 12 stdlib `re` patterns (zero deps). Category-labeled placeholders (`<EMAIL>`, `<GITHUB_TOKEN>`) — LLM benefits from category for reasoning.
- **9 sink points** — every external surface (LLM, GitHub PR, Git commit, webhooks, file I/O) is a redaction point. Redaction happens AFTER compaction so we don't double-process.
- **ONNX local embeddings** — `intfloat/multilingual-e5-small` (RU+EN, 384-dim, ~120MB disk). PrivacyAwareEmbedder wraps OnnxEmbedder and runs redaction BEFORE embedding (defense in depth).
- **DenseRetriever pre-computes matrix** from `metadata.embedding` (no re-embed at construction). Filters mismatched `embedding_version` so model swaps don't corrupt retrieval.
- **HybridRetriever via RRF k=60** — standard cheap hybrid that beats either retriever alone. Documents in BOTH retrievers rank above those in only one.
- **UnifiedMemory `search()` unchanged** (backward compat) — new `search_scored()` method for dense retrieval. Breaking change rejected in implementation to keep Phase 2.5 callers working.
- **Trust boundary preserved** — `runner.py` continues to not import `LLMRouterClassifier`/`MergeQueue`/`AdversarialVerify`. Compactor + Privacy + Embeddings all DI'd through constructors, not top-level imports.
- **`asyncio.Lock` in OnnxEmbedder** — `tokenizers` is not thread-safe; we serialise calls to keep a single instance safe under concurrent asyncio use.

### Out of scope (явно, Phase 3.5+)

- Privacy bypass via base64/hex encoding (documented in `docs/PHASE3.md` known limitations).
- Multi-tenant `gh` config.
- Embedding re-computation migration tool (when model version bumps).
- Real-time redaction UI dashboard.
- Cross-session compaction handoff (in-session only for v1.0.0).
- Full plug-in custom pattern loader (Phase 5).
- ONNX `directml` GPU provider (Windows GPU optional, requires DX11/12 driver).

## Phase 2.5 — Cross-Repo Stacks + Outbound Webhooks + Auto-Label + Rate Limit (ЗАКРЫТО v0.9.0, 2026-06-14)

**Phase 2.5 (v0.9.0) — 4 шага / 4 коммита / +58 net new тестов (759 → 817) / 0 new deps**

### Шаги

- **Step 0 (commit `phase-2.5-step-0-outbound`)** — 9 новых settings (`auto_add_label`, `pr_rate_limit_*`, `outbound_webhook_*`); `harness/agents/outbound.py` (NEW, `OutboundWebhookDispatcher`: httpx + fire-and-forget + 4 event kinds + bounded retries); `pr_templating.py:parse_codeowners_for_diff` (pure, fnmatch-based, closes Phase 2.4 TODO at `merge_queue.py:820`). **+35 tests.**
- **Step 1 (commit `phase-2.5-step-1-rate-limit-label`)** — `_gh_with_retry` wrapper (403/429 + `Retry-After` + exponential backoff + jitter) оборачивает все `gh` calls в `pr_integration.py`; `add_pr_label` через `gh pr edit --add-label`; auto-label wired в `_run_pr_phase` + per-slice в `_run_stack_phase` (best-effort, log + continue). `gh_subprocess_stub` defaults to success for `auth status` + `pr edit` (backward compat для pre-2.5 тестов). **+9 tests.**
- **Step 2 (commit `phase-2.5-step-2-cross-repo`)** — `merge_jobs.stack_repos` TEXT (JSON list, NULL для non-cross-repo); 4 SELECT queries + `_parse_stack_repos`; `MergeJob.stack_repos: list[Path] | None` с validation `len == split_into`; `_run_stack_phase` per-slice `WorktreeSession` через `repo_slice` (1 worktree per repo); CLI `--stack-repos`; API `_EnqueueRequest` + `_JobRecordSchema`. **+3 tests.**
- **Step 3 (commit `phase-2.5-step-3-outbound-wiring`)** — `MergeQueue` DI `outbound: OutboundWebhookDispatcher | None`; `_emit()` fires outbound (fire-and-forget); `_run_pr_phase` emits `pr_waiting_review` после `wait_for_checks` если `review_required`; `WebhookHandler` DI `outbound=`; `dispatch_event` fires `stack_merged` after parent promotion; `server/app.py` lifespan wires `OutboundWebhookDispatcher` + `aclose()` on shutdown. **+11 tests.**

### Final metrics

- **Commits:** 4 (Step 0..3)
- **Tests:** 817 mock + 5 real_llm = 822 total (was 759 pre-Phase-2.5, +58 net new)
- **Commits в `06_Harness/`:** 63 (59 → 63, +4 Phase 2.5)
- **New files:** 4 (`outbound.py`, `test_outbound.py`, `test_codeowners_parser.py`, `test_merge_queue_outbound.py`)
- **New LoC:** ~1500 production + ~700 tests
- **New deps:** 0 (httpx уже в Phase 0; stdlib `asyncio`, `random`, `fnmatch`, `re`, `json`)

### Архитектурные решения (Phase 2.5)

- **`OutboundWebhookDispatcher` как singleton в `app.state`** — конструктивно в `server/app.py` lifespan, инжектится в `MergeQueue` + `WebhookHandler` через DI. `webhook_handler.py` НЕ импортирует `pr_integration` / `outbound` at module top — trust boundary preserved.
- **N WorktreeSession-ов для cross-repo stacks** — Phase 2.4 reuse 1 worktree для N branches; Phase 2.5: 1 worktree per repo (cross-repo не может шарить worktree — разные `.git`). Per-repo `RepoLockRegistry` lock acquired sequentially. Trade-off: медленнее, но семантически правильно.
- **`_gh_with_retry` оборачивает public API, не `_gh`** — `merge_queue` импортирует `create_pr`, `merge_pr` (public), `merge_queue.py` не видит `_gh` напрямую. Tests monkeypatch `_gh` (Phase 2.2 pattern) — unchanged.
- **Auto-add label = best-effort** — failure не блокирует `enable_auto_merge`. Real branch-protection error будет виден в `enable_auto_merge` если label был единственным blocker.
- **Per-`_emit` outbound fire-and-forget** — `_emit` НЕ `await` outbound delivery. Slow receiver не блокирует job lifecycle. `OutboundWebhookDispatcher.fire()` creates asyncio task.
- **`stack_repos` JSON serialised in TEXT column** — `json.dumps(list)` on write, `json.loads` on read with defensive defaults (NULL/empty/invalid → `None`). Backward compat: NULL = single-repo job.
- **CODEOWNERS → reviewers** — `parse_codeowners_for_diff` closes Phase 2.4 TODO. Pure function, no network. O(files × patterns) типично <1ms.

### Ограничения (явно OUT OF SCOPE, Phase 2.6+)

- Cross-repo stacks с разными PR strategies per repo.
- Outbound webhook HMAC signing (Phase 4).
- Auto-add multiple labels.
- Stacked stack (3+ уровня вложенности).
- Outbound persistent retry queue (Phase 4).

### Backward compat

Все 759 Phase 1.6+2.2+2.3+2.4 теста + 35+9+3+11 Phase 2.5 = 817 passed без изменений в production code Phase 2.5. Default path (`pr_mode="off"`, no stack, no outbound) = unchanged. Single-repo stacks (Phase 2.4 default) = unchanged. CLI `--split-into` без `--stack-repos` = Phase 2.2/2.4 behaviour.

### Tag

`v0.9.0` annotated + push

---

## Phase 2.4 — Stacked PRs + Review Templating + Approved Short-Circuit (ЗАКРЫТО v0.8.0, 2026-06-14)

**Phase 2.4 (v0.8.0) — 4 шага / 4 коммита / 86 net new тестов (673 → 759) / 0 new deps**

Расширяет Phase 2.3 тремя крупными фичами (per roadmap `12.06 Harness-Claude-Code-Architecture/roadmap.md:875`):

1. **Stacked / multi-PR per job** — 1 task = N dependent PRs. PR-B's `base_branch` = PR-A's branch (GitHub stacked-PR convention). 4 strategies (`auto`/`files`/`directory`/`size`); max 8 slices; pure-function planner; N branches в одном worktree (без worktree proliferation).
2. **PR body templating** — `harness/agents/templates/pr_body.md` (default) + custom override via `settings.pr_template_path`. Auto-extracts issue numbers from task text (`Closes #N` / `Refs #N`). `create_pr` теперь поддерживает `body_file: Path` для длинных templates (>ARG_MAX).
3. **`pull_request_review.approved` short-circuit** — закрывает Phase 2.3 explicit no-op. На `approved` event: вызывает injected `merge_pr` (или `enable_auto_merge` если `job.auto_merge=True`) → `merged` (или `pr_auto_merge_enabled`). Также: parent-orchestrator row в стэке промоутится в `merged` после последнего child PR merge (через `JobStore.all_stack_children_merged` + `_maybe_promote_stack_parent`).

**Ключевые архитектурные решения:**

- **`pr_stack_id` + `stack_position` + `stack_size` + `depends_on_pr_number`** в `merge_jobs` (4 новые колонки, idempotent migration в `_apply_phase22_migrations` per Phase 2.3 pattern). Index `idx_merge_jobs_stack_id` после ALTER.
- **Parent row at `stack_position=0`** — orchestrator, `pr_number=NULL`. `find_job_by_pr_number` фильтрует `pr_number IS NOT NULL` (back-compat: orchestrator row не возвращается на webhook lookups).
- **N branches в 1 worktree** — `git -C <wt> checkout -B harness/<id>/step-<N>` для каждого slice. Push через `git push -u origin <branch>` перед `create_pr`. WorktreeSession не поддерживает mid-life branch switching — но `WorktreeSession` это просто checkout, git handles it.
- **Pure-function SplitPlanner** — `harness/agents/pr_split.py:plan_splits()` без I/O, testable без git. 4 strategies, deterministic output, sort-stable.
- **DI для trust boundary** — `WebhookHandler(store, secret, *, merger=None, auto_merger=None)`. `merge_pr` / `enable_auto_merge` инжектятся в lifespan, НЕ в module top-level. Phase 2.3 no-op сохранён (default constructor без merger = no-op).
- **Per-repo `RepoLockRegistry`** — стэк в 1 repo = serialised. Cross-repo stacks не поддерживаются (явно в docs).
- **PR review flow** — `pr_waiting_review` (status уже существовал из Phase 2.2) теперь достижим: после `wait_for_checks` success, если `review_decision == "review_required"`, job переходит в `pr_waiting_review` и poll'ит каждые 30с (timeout 24ч, settings). Approved → `merging_pr`; changes_requested → `failed`.

**Step 0 (commit `61ea636`) — Schema + SplitPlanner:**
- `merge_jobs` +4 stack cols, `_PR24_ALTER_COLUMNS`, `idx_merge_jobs_stack_id`
- `JobStore.create()` +4 stack kwargs, `load()` / `find_job_by_pr_number()` / `list_recent()` → `_row_to_record()` helper
- `find_jobs_by_stack_id(stack_id)` ordered by position
- `all_stack_children_merged(stack_id)` для parent promotion
- `harness/agents/pr_split.py` (NEW, ~250 LoC) — pure planner
- 8 settings: `pr_split_strategy`, `pr_split_max_files_per_slice`, `pr_split_min_slices`, `pr_split_max_slices`, `pr_template_path`, `pr_issue_link_re`, `pr_review_timeout_s`, `pr_review_poll_interval_s`
- 31 net new tests (22 pr_split + 9 job_store)

**Step 1 (commit `8de5d87`) — PR body templating:**
- `harness/agents/pr_templating.py` (NEW, ~250 LoC) — `extract_issue_numbers`, `render_pr_body` (pure)
- `harness/agents/templates/pr_body.md` (default template, 30 LoC, 7 placeholders)
- `pr_integration.create_pr` +`body_file: Path | None = None` → `gh pr create --body-file <path>`
- `_run_pr_phase` заменяет inline f-string на `render_pr_body()`
- `MergeJob` +5 stack fields (split_into, stack_id, stack_position, stack_size, depends_on_pr_number, slice_files)
- 21 net new tests (8 extract_issue_numbers + 13 render_pr_body + body_file)

**Step 2 (commit `6ef1cdf`) — Stacked PR orchestration:**
- `_run_stack_phase` (~280 LoC) — split → branch → commit → push → create_pr per slice
- Helpers: `_get_diff_files`, `_commit_slice`, `_push_branch`, `_cancel_stack`
- Sync `_run_job` reject `split_into > 1` (background-only)
- `_run_job_async` branch: `split_into > 1` → `_run_stack_phase` (else `_run_pr_phase`)
- `JobStore.create` +`pr_url` +`pr_number` (persist child slice at create_pr moment)
- CLI: `--split-into`, `--split-strategy`, `--stack-files`, +4 internal hidden flags
- API: `GET /stacks/{stack_id}` returns parent + children; `_JobRecordSchema` +4 stack fields; `_EnqueueRequest` +3 stack fields
- 12 net new tests (5 _run_stack_phase + 1 sync reject + 6 CLI)

**Step 3 (commit `c359ae7`) — Approved short-circuit + multi-PR webhook:**
- `WebhookEvent` +`pr_numbers: list[int]` (check_run fan-out)
- `parse_github_payload("check_run", ...)` — extract ALL linked PRs (was [0])
- `dispatch_event` refactored: fan-out per-PR, aggregate results
- `_on_review_approved` — calls injected `merger` (or `auto_merger` for `auto_merge=True`), transitions to `merged` / `pr_auto_merge_enabled` / `failed`
- `_maybe_promote_stack_parent` — flip parent to `merged` when all children merged
- `WebhookHandler` DI: `merger`, `auto_merger` callable injection
- `server/app.py` lifespan wires `merge_pr` + `enable_auto_merge` from `pr_integration`
- 11 net new tests (3 parse + 4 approved + 2 fan-out + 2 stack promotion)

**Step 4 (this commit) — CLI split-plan + docs + closeout:**
- `harness agents split-plan` subcommand — dry-run preview, prints plan
- `docs/merge-queue.md` +"Stacked PRs (Phase 2.4 v0.8.0)" раздел (~140 строк: strategy table, quick start, recovery, API additions, limitations)
- `docs/CHANGELOG.md` +this section
- 11 net new tests (8 dry-run + 2 dispatcher + 1 subprocess)

**Roadmap status:**

| Фаза | Статус | Tag |
|------|--------|-----|
| Phase 0+0.5+0.6 Web MVP | ✅ | v0.1.0 |
| Phase 1 (4-layer memory) | ✅ частично | v0.2.0 |
| Phase 1.6 (scope-gated API) | ✅ ЗАКРЫТО | v0.6.0 |
| Phase 2.0+2.1 (sub-agents v1.1) | ✅ | v0.4.0 |
| Phase 2.2 (real GH PR) | ✅ ЗАКРЫТО | v0.5.0 |
| Phase 2.3 (PR webhooks + auto-merge) | ✅ ЗАКРЫТО | v0.7.0 |
| **Phase 2.4 (stacked + templating + approved)** | ✅ **ЗАКРЫТО** | **v0.8.0** (NEW) |
| Phase 3 (context engineering) | ⏳ | — |
| Phase 4 (hooks + observability) | ⏳ | — |
| Phase 5 (eval + hardening) | ⏳ | — |
| Phase 6 (UX + IDE) | ⏳ | — |

**Следующие кандидаты (по roadmap приоритету):**
- **Phase 2.5** — cross-repo stacks, outbound webhooks, auto-add `harness-auto-merge` label. ~1-2 нед.
- **Phase 3** (compaction + embeddings + privacy) — 2-3 нед, закрывает 4 carryover из Phase 1.
- **Phase 4** (12 hooks + observability + `/api/*` → `/api/v1/*` migration) — 2-3 нед, production hardening.

**Final test count:** 759 mock + 5 real_llm = 764 total. Commits в `06_Harness/`: 59 (55 → 59). New deps: 0.

**Backward compat:** все 748 Phase 1.6+2.2+2.3+2.4 тестов проходят без изменений. Default path (`pr_mode="off"`, no stack) = unchanged. Production deployment: `HARNESS_WEBHOOK_SECRET` env + `AUTH_REQUIRED=true` (default). CLI `--split-into` = backward-compatible (Phase 2.2 single-PR behavior when `split_into is None` or `≤ 1`).

---

## Phase 2.3 — PR Webhooks + Auto-Merge (ЗАКРЫТО v0.7.0, 2026-06-14)

**Phase 2.3 (v0.7.0) — 4 шага / 4 коммита за ~3 часа (post-Phase 1.6, единая сессия)**

| # | Шаг | Коммит | Что | +Tests |
|---|-----|--------|-----|--------|
| 0 | Webhook store + settings | `a77b678` | `harness/agents/webhook_store.py` (NEW, ~180 LoC) — `WebhookEventStore` (aiosqlite, `webhook_events` table, `UNIQUE(delivery_id)` для idempotency, `is_duplicate` / `record_event` / `mark_processed` / `get_event` / `count_unprocessed`); `jobs.py` +1 status (`pr_auto_merge_enabled`), `find_job_by_pr_number(pr_number)` для webhook dispatch, idx на `pr_number` (idempotent миграция в `_apply_phase22_migrations`); 6 settings (`webhook_secret`, `webhook_path`, `webhook_max_payload_kb`, `auto_merge_label`, `auto_merge_method`, `auto_merge_delete_branch`); валидация `auto_merge_method ∈ {squash, merge, rebase}`; `conftest.isolated_settings` ставит `webhook_secret` для тестов | 12 |
| 1 | HMAC + parsing | — (combined in next commit) | `harness/agents/webhook_handler.py` (NEW, ~280 LoC) — `verify_github_signature` (HMAC-SHA256 через `hmac.compare_digest`, timing-safe); `WebhookVerificationError` с `reason` (missing_signature / bad_signature / missing_secret); `WebhookEvent` (Pydantic) с 8 полями; `parse_github_payload` для 3 event types (pull_request / check_run / pull_request_review); `WebhookHandler.handle_raw` (verify → duplicate check → parse → record) + `dispatch_event` (lookup by pr_number → update JobStore) | 27 |
| 2 | Auto-merge phase | `16f58be` | `pr_integration.py` +`enable_auto_merge` / `disable_auto_merge` (gh wrappers); `MergeJob` +`auto_merge` / `auto_merge_method` / `auto_merge_label`; `_run_pr_phase` — после `wait_for_checks` success: `enable_auto_merge()` → status `pr_auto_merge_enabled` (ждём webhook) **vs** fallback на direct `merge_pr` (Phase 2.2 behavior) при branch protection not configured; CLI флаги `--auto-merge` / `--pr-auto-merge` / `--auto-merge-method` / `--auto-merge-label`; `--pr-auto-merge` shortcut = `--pr --auto-merge`; `--pr-auto-merge` без `--background` → exit 2 | 16 |
| 3 | Webhook route + docs | — | `harness/server/routes/agents_webhooks.py` (NEW, ~150 LoC) — `POST /api/v1/agents/webhooks/github` (HMAC verify → handle_raw → dispatch); читает `X-Hub-Signature-256` / `X-GitHub-Event` / `X-GitHub-Delivery` (case-insensitive); mount на `settings.webhook_path` (default `/api/v1/agents/webhooks/github`); lifespan wires `WebhookEventStore` + `WebhookHandler` на `app.state`; `docs/merge-queue.md` +раздел "Webhooks (Phase 2.3 v0.7.0)" — setup, event-to-status mapping, HMAC security, idempotency, CLI examples, ngrok testing | 12 |

### Метрики (на 14.06.2026, end of Phase 2.3)

- **Tests:** 606 (Phase 1.6 end) + 12 + 27 + 16 + 12 = **673 mock** + 5 real_llm
- **Production:** 4 новых файла (`webhook_store.py`, `webhook_handler.py`, `routes/agents_webhooks.py`, раздел в `merge-queue.md`) + 6 модифицированных (`jobs.py`, `config.py`, `pr_integration.py`, `merge_queue.py`, `cli.py`, `app.py`, `conftest.py`, `merge-queue.md`, `CHANGELOG.md`) — ~900 LoC net new
- **Settings:** +6 (webhook_secret, webhook_path, webhook_max_payload_kb, auto_merge_label, auto_merge_method, auto_merge_delete_branch)
- **Job statuses:** +1 (pr_auto_merge_enabled) — 14 total
- **HTTP routes:** +1 (`POST /api/v1/agents/webhooks/github`) — `/api/v1/*` total 10 routes
- **CLI flags:** +4 (`--auto-merge`, `--pr-auto-merge`, `--auto-merge-method`, `--auto-merge-label`)
- **Backward compat:** Phase 1.6 v0.6.0 + Phase 2.2 v0.5.0 тесты работают unchanged (`auth_required=False`, `--pr` defaults, default `pr_mode=off`)
- **New deps:** 0 (hmac + hashlib stdlib; aiosqlite/pydantic/fastapi из Phase 0-1)
- **Tag:** v0.7.0 (annotated)

### Architecture decisions (Phase 2.3)

- **HMAC-SHA256 для inbound webhooks** (стандарт GitHub, не Phase 1.6 tokens) — tokens для outbound, webhooks для inbound
- **`UNIQUE(delivery_id)` constraint** — canonical idempotency для GitHub redeliveries; `is_duplicate` fast-path avoids HMAC + parse на redelivery
- **Anti-enumeration** — same 503 для "secret not configured" и 401 для "bad signature" (no error-message side channels)
- **No scope check на webhook route** — HMAC IS the auth (Phase 1.6 tokens для outbound); trust boundary preserved (`webhook_handler.py` не импортирует из `harness/server/auth/*`)
- **Auto-merge fallback** — `enable_auto_merge` fails (branch protection not configured) → queue сразу вызывает `gh pr merge` (Phase 2.2 behavior); user не теряет job
- **`pr_auto_merge_enabled` is in-flight** — Phase 2.3 добавил его в `_RUNNING_STATUSES`; `recover_running()` marks as cancelled после restart (matches other PR-phase statuses)
- **3 base event types** (pull_request, check_run, pull_request_review) — `pull_request_review.approved` = no-op (Phase 2.4 review flow)
- **WebhookEventStore отдельная таблица** в том же DB file что и JobStore (`agent-jobs.db`) — atomic creation, но логическое разделение для ops queries
- **Bridge через `app.state`** — webhook handler и event store — lifespan-time singletons, `request.app.state` для route access
- **Default `webhook_path = "/api/v1/agents/webhooks/github"`** — операторы могут override через `HARNESS_WEBHOOK_PATH` env
- **CLI `--pr-auto-merge` shorthand** — `--pr --auto-merge` shortcut (no double-flag-typing)
- **Webhooks полностью opt-in** — empty `webhook_secret` → 503 на route, но остальной сервер работает

### Готово (Phase 2.3)

- [x] Webhook receiver принимает `pull_request` / `check_run` / `pull_request_review` с HMAC-SHA256
- [x] Bad signature → 401, missing signature → 401, `webhook_secret=""` → 503
- [x] Redelivery (duplicate `X-GitHub-Delivery`) → 200 + `{"processed": false, "detail": "duplicate..."}` (idempotency)
- [x] `pull_request` `closed+merged` → job marked `merged` (was `pr_auto_merge_enabled`)
- [x] `check_run` `failure` → job marked `failed` с error message
- [x] `pull_request_review` `changes_requested` → job marked `failed`
- [x] Unknown event types → 200 + logged + ignored (no crash)
- [x] `MergeQueue._run_pr_phase` с `auto_merge=True` → `pr_auto_merge_enabled` (ждёт webhook), не `merged`
- [x] `auto_merge=True` + branch protection not configured → fallback на direct merge → `merged` (backward compat)
- [x] CLI `--pr-auto-merge` shortcut = `--pr --auto-merge`
- [x] CLI `--pr-auto-merge` без `--background` → exit 2 (same constraint as `--pr`)
- [x] `docs/merge-queue.md` раздел "Webhooks" с setup, payload examples, HMAC, troubleshooting
- [x] 0 new deps (`git diff pyproject.toml` пуст)
- [x] Trust boundary preserved: `webhook_handler.py` НЕ импортирует из `harness/server/auth/*`; `routes/agents_webhooks.py` НЕ импортирует из `harness/agents/*` (only через `request.app.state`)
- [x] Per commit: `pytest -m "not real_llm" -q` зелёный, `git status` clean
- [x] Tag `v0.7.0` annotated + push

### Carryover в Phase 2.4+

- **Stacked / multi-PR per job** (split one task into N dependent PRs, dependency graph)
- **PR review templating** (CODEOWNERS-aware reviewers, issue-link auto-resolve, pull_request_template.md injection)
- **Multi-tenant `gh` config** (different GitHub identities per tenant)
- **Web UI для PR** (clickable pr_url + status badges в React)
- **Cross-PR dependency tracking** (PR-B waits for PR-A merge)
- **GitHub rate limit handling** (automatic backoff, 403 detection)
- **Outbound webhooks** (Phase 4 hooks — notify external systems о job state changes)
- **Custom event mappings** (config-driven webhook → action mapping)
- **Pull_request_review `approved` short-circuit** (currently no-op — Phase 2.4 для `pr_waiting_review` status)

---

## Phase 1.6 — Scope-gated API v1.0 (ЗАКРЫТО v0.6.0, 2026-06-14)

**Phase 1.6 (v0.6.0) — 6 шагов / 6 коммитов за ~3.5 часа (post-Phase 2.2, единая сессия)**

| # | Шаг | Коммит | Что | +Tests |
|---|-----|--------|-----|--------|
| 0 | Prerequisites | `eff5725` | `harness/server/auth/{scopes,tokens,db}.py` — `Scope` enum (6 значений), `parse_scopes` / `has_scope` / `format_scopes`, `TokenStore` (aiosqlite, SHA-256 hashed), `TokenRecord` (frozen dataclass); 4 settings (`auth_db_path`, `auth_token_bytes`, `auth_default_scopes`, `auth_required`) | 24 |
| 1 | FastAPI deps | `4d30871` | `harness/server/auth/deps.py` — `get_token_store` (503), `get_current_token` (401 with `WWW-Authenticate: Bearer`), `require_scope(*required)` factory (403 with `missing required scope: X (have: A, B)`); ANY match; case-insensitive `bearer`; same msg для not-found/revoked (anti-enumeration); `auth_required=False` short-circuit | 13 |
| 2 | Capabilities + apply | `3f30bf0` | `harness/server/auth/route_registry.py` (NEW) — `EndpointSpec` + `collect_endpoints(app)` walks mounted routes, finds `require_scope` deps via `_required_scopes` marker attribute (на dep callable); `harness/server/routes/capabilities.py` (NEW) — `GET /api/v1/capabilities` (public, returns server_version + auth_required + scopes_available[6] + endpoints[]); `agents_jobs.py` — `Depends(_agents_read)` на всех 3 GET routes | 9 |
| 3 | CLI auth + bootstrap | `9567012` | `harness auth {create,list,revoke,whoami,test}` — 5 handlers, `_dispatch_auth` runs bootstrap только для read-only commands, `_bootstrap_admin_token_if_needed` mints `bootstrap-admin` с ALL_SCOPES при `auth_required=True` И `len(list_active)==0`; `--bootstrap` flag для admin tokens; revoke supports hash OR label; `whoami` debug; `test` urllib-based smoke against local server; stdout reconfigure UTF-8; ASCII `...` | 18 |
| 4 | Memory + sessions v1 | `246f54f` | `harness/server/agent/memory_v1.py` (NEW, bridge) — `search()` / `write_note()` / `stats()`; `harness/server/routes/memory_v1.py` (NEW) — `GET /api/v1/memory/search` (memory.read), `POST /api/v1/memory/notes` (memory.write), `GET /api/v1/memory/stats` (memory.read); `harness/server/routes/sessions_v1.py` (NEW) — `GET /api/v1/sessions?recent=N` (sessions.read, thin wrapper) | 15 |
| 5 | POST + docs + tag | (this commit) | `POST /api/v1/agents/jobs` — enqueue sub-agent job, requires `agents.write` (+ `agents.pr` compound when `pr_mode != "off"`); validates `prompt` non-empty, `agent` в known specs, `model` в catalog; `docs/scope-api.md` (NEW, ~280 строк); CHANGELOG closeout; v0.6.0 tag | 8 |

### Метрики (на 14.06.2026, end of Phase 1.6)

- **Tests:** 518 (Phase 2.2 end) + 24 + 13 + 9 + 18 + 15 + 8 = **606 mock** + 5 real_llm
- **Production:** 13 новых файлов (`auth/{__init__,scopes,tokens,db,deps,route_registry}.py`, `routes/{capabilities,memory_v1,sessions_v1}.py`, `agent/memory_v1.py`, `docs/scope-api.md`) + 4 модифицированных (`config.py`, `app.py`, `cli.py`, `routes/agents_jobs.py`) — ~2400 LoC net new
- **Settings:** +4 (auth_db_path, auth_token_bytes, auth_default_scopes, auth_required)
- **Scopes:** 6 (agents.read, agents.write, agents.pr, memory.read, memory.write, sessions.read)
- **HTTP routes:** +6 (`GET /api/v1/capabilities`, `GET /api/v1/memory/search`, `POST /api/v1/memory/notes`, `GET /api/v1/memory/stats`, `GET /api/v1/sessions`, `POST /api/v1/agents/jobs`) — `/api/v1/*` total 9 routes (3 agents + 4 memory + 1 sessions + 1 capabilities)
- **CLI subcommands:** +1 (`harness auth` with 5 sub-subcommands)
- **Backward compat:** legacy `/api/*` routes (sessions, chat, models, health) остаются open; `auth_required=False` (default в test suite) → существующие Phase 0-2.2 тесты работают unchanged
- **New deps:** 0 (aiosqlite + pydantic + fastapi из Phase 0-1)
- **Tag:** v0.6.0 (annotated, pushed)

### Architecture decisions (Phase 1.6)

- **SQLite aiosqlite persistent store** — multi-tenant, prod-ready; SHA-256 хэш (256-bit opaque tokens, не passwords)
- **`secrets.token_urlsafe(32)`** — 43-char URL-safe plaintext, показывается ОДИН раз
- **`has_scope` = ANY match** — token со scope A может вызвать endpoint, требующий A OR B; compound checks (e.g. `agents.write` + `agents.pr` для `pr_mode != "off"`) — explicit в route body
- **Anti-enumeration:** same 401 message для "not found" vs "revoked" — атакующий не может угадывать token hashes по status code
- **`auth_required=False` master switch** — test suite + dev mode без токенов; prod = `True` (default)
- **Bootstrap only for read-only commands** — `create` / `revoke` никогда не триггерят bootstrap (никаких "сюрпризов" для пользователя)
- **Marker attribute `_required_scopes` на dep callable** — introspection для capabilities endpoint без fragile signature parsing (closure args не видны)
- **Bridge module `harness/server/agent/memory_v1.py`** — routes не импортируют `UnifiedMemory` напрямую (trust boundary + future microservice split)
- **Legacy `/api/*` routes stay open** — gradual migration в Phase 4+ с deprecation headers
- **0 new deps** — всё на aiosqlite + pydantic + fastapi из Phase 0-1

### Готово (Phase 1.6)

- [x] `GET /api/v1/capabilities` returns 200 без auth + полный JSON (server_version, auth_required, scopes_available, endpoints)
- [x] Token created via `harness auth create` — plaintext printed once, hash persisted, scopes enforced
- [x] `Authorization: Bearer <token>` — valid token → 200, missing → 401, malformed → 401, revoked → 401
- [x] Все `/api/v1/agents/jobs*` routes require `agents.read` (GET) или `agents.write` (POST) + `agents.pr` (POST с pr_mode != off)
- [x] `GET /api/v1/memory/search` requires `memory.read`
- [x] `POST /api/v1/memory/notes` requires `memory.write`
- [x] `GET /api/v1/sessions` requires `sessions.read`
- [x] `harness auth list/revoke/whoami/test` работают через CLI
- [x] Bootstrap admin token создаётся при первом запуске с `auth_required=True` (read-only commands only)
- [x] Token store — SQLite, persistent, переживает restart
- [x] `auth_required=False` → всё open (dev mode escape hatch)
- [x] Legacy `/api/*` (sessions, chat, models, health) **остаются open** (Phase 1.6 не ломает Web UI)
- [x] 0 new deps (sqlite3 + aiosqlite уже есть)
- [x] Trust boundary: `harness/server/auth/` НЕ импортирует из `harness/agents/` (static check)
- [x] `docs/scope-api.md` создан, покрывает 5+ секций + troubleshooting

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

### Step 3 — CLI `harness auth` subcommand + bootstrap (commit `9567012`)

| # | Что | Файлы | +Tests |
|---|-----|-------|--------|
| Step 3 | `harness/cli.py` — `auth` subparser (create/list/revoke/whoami/test), 5 handlers, `_dispatch_auth` runs bootstrap только для read-only commands, `_bootstrap_admin_token_if_needed` mints `bootstrap-admin` с ALL_SCOPES при `auth_required=True` И `len(list_active)==0`; `--bootstrap` flag для admin tokens; revoke supports hash (64 hex) OR label; `whoami` debug; `test` smoke against local server; **stdout reconfigure UTF-8** для Windows compat; **ASCII `...`** вместо `…` для subprocess piping | MODIFIED: `cli.py` +6 subparser + 5 handlers (~280 LoC), NEW: `tests/test_cli_auth.py` (~340 LoC, 18 tests) | 18 |

### Step 4 — Memory + sessions v1 routes (this commit)

| # | Что | Файлы | +Tests |
|---|-----|-------|--------|
| Step 4 | `harness/server/agent/memory_v1.py` (NEW, ~150 LoC) — bridge между `routes/memory_v1.py` и `UnifiedMemory`: `search()`, `write_note()`, `stats()` + lazy default слот; `harness/server/routes/memory_v1.py` (NEW, ~135 LoC) — `GET /api/v1/memory/search` (memory.read), `POST /api/v1/memory/notes` (memory.write), `GET /api/v1/memory/stats` (memory.read); `harness/server/routes/sessions_v1.py` (NEW, ~55 LoC) — `GET /api/v1/sessions?recent=N` (sessions.read, thin wrapper over `db_sqlite.list_sessions`); `harness/server/app.py` mount 2 новых router | NEW: 3 файла + `tests/test_memory_v1_routes.py` (~310 LoC, 15 tests) | 15 |

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
