# Phase 3 v1.3.0 — Select + Compress (L2 retrieval + LLM-curator + hierarchical summary)

> **Status:** ЗАКРЫТО v1.3.0 (2026-06-15)
> **Tag:** `v1.3.0` (annotated)
> **Tests:** 1146 mock (от 1098 в v1.2.1, +48 net)

## TL;DR

Phase 3 v1.3.0 закрывает две стратегии Anthropic context-engineering
playbook — **Select** (retrieval-based, top-K) и **Compress**
(hierarchical summary). Главная ценность: scratchpad L2 archive,
заявленный в v1.2.0 как "unbounded" и "discoverable", теперь
**действительно** discoverable через два новых tool'а:

- `scratchpad_l2_search(query, top_k?)` — dense+BM25 hybrid RRF
  + LLM-curator re-rank (опционально)
- `scratchpad_l2_promote_to_l1(query, max_notes?)` — fetch top-N
  L2 notes → bullet-point summary → записать как L1 plan note

Агенты теперь могут спросить "что мы решили про X 3 дня назад?" и
получить top-5 релевантных L2 notes за один tool call.

## Архитектура (одобрено Марком)

| Решение | Значение |
|---------|----------|
| L2 storage | **Qdrant** primary, **SQLite fallback**. Settings: `scratchpad_l2_qdrant_url` (None → SQLite). `make_l2_store()` factory с best-effort probe. |
| L2 indexing | Автоматический при `write_note(level="L2")` через `L2VectorStore.upsert`. Payload: `session_id, agent_id, level, created_at, tags`. |
| Select strategy | **Hybrid dense+BM25** через новый `L2Retriever` (RRF k=60, fetch_k=20). Затем **LLM-curator** через T1 (Qwen3 8B) — top-50 → top-10. Curator failure → fall back на plain hybrid. |
| Compress strategy | **Hierarchical summary** — `scratchpad_l2_promote_to_l1` fetches top-N L2 notes, bullet-summarizes в L1 plan note. Не нужен отдельный LLM call — note content IS the summary. |
| Tools | 2 new: `scratchpad_l2_search` + `scratchpad_l2_promote_to_l1` (12 tools всего). |
| Settings | 2 new: `scratchpad_l2_qdrant_url` (None = SQLite), `scratchpad_l2_qdrant_collection` (default `scratchpad_l2`). |
| Trust boundary | `runner.py` continues to NOT import `L2Retriever` / `QdrantL2Store` / `LLMRouter`. L2 retrieval через DI factory callable. |
| Fail-open | Qdrant недоступен → log warning + SQLite fallback. LLM-curator raises → return top-K from hybrid only. Scratchpad store raises → ToolResult(ok=False), chat loop intact. |

## L2 → system prompt flow (composition)

```
Agent.run()
  → runner._drive()
    → build L0 section (v1.2.1)
    → build system message with L0
    → build L2 retriever (factory DI)
    → ToolRuntime(scratchpad, l0_section, l2_retriever, l2_router)
    → AgentLoop.run() — applies l0_section (v1.2.1)
  → LLM sees: system (with L0 hot) + tools (12, including L2 search)
```

При вызове `scratchpad_l2_search`:
1. ToolRuntime → `L2Retriever.curated_search(query, top_k, candidate_k, notes, router)`
2. Hybrid RRF → top-50 candidates
3. Если `l2_router` есть → curator prompt → LLM scores → re-rank → top-K
4. Иначе → plain hybrid top-K
5. Return JSON list с `{id, content, tags, score}`

## Trust boundary (сохраняется через все шаги)

- `runner.py` continues to NOT import `L2Retriever` / `QdrantL2Store` / `LLMRouter`
- All new modules DI'd через constructors (factory callable pattern, mirror `unified_memory_factory`)
- `l2_retriever=None` дефолт в `ToolRuntime` — backward compat
- Fail-open во всех L2 retrieval calls (try/except + logger.warning + return empty/None)
- Static test `test_runner_does_not_import_scratchpad` продолжает проходить

## Settings (2 new)

```python
scratchpad_l2_qdrant_url: str | None = None     # default OFF → SQLite
scratchpad_l2_qdrant_collection: str = "scratchpad_l2"
```

## Lessons (для будущих Solomon sessions)

1. **str.format() escape с literal JSON** — `\.format()` интерпретирует
   `{` в примерах JSON. Решение: использовать `.replace("__PLACEHOLDER__", value)`
   или экранировать `{{` `}}`. Lesson: для промптов с JSON-примерами
   всегда использовать `.replace()`, не `.format()`.
2. **Missing field в JSON-parsing = skip** — `item.get("score", 0.0)`
   даст 0.0, что проходит range check. Правильно: явный
   `if "score" not in item: continue`. Lesson: при парсинге LLM
   output — "missing field" и "default 0" — разные вещи.
3. **SpyTest класс сигнатура синхронизации** — `SpyToolRuntime.__init__`
   нужно обновлять при каждом добавлении kwarg в `ToolRuntime.__init__`.
   Lesson: `class X(real_X): def __init__(...)` в test'ах требует
   ручной синхронизации.
4. **Qdrant optional, SQLite fallback** — даже если оператор настроил
   `scratchpad_l2_qdrant_url`, мёртвый Qdrant → автоматический SQLite
   fallback. Это даёт операционную устойчивость без жёстких deps.
5. **factory DI signature `(AgentSpec, str | None)`** — единая
   сигнатура для per-call construction. Mirror `unified_memory_factory`.
6. **Python str.format() vs JSON** — `{` в `[{"id": 42}]` парсится как
   format spec. Лучше `.replace()`.

## Files (v1.3.0)

**2 NEW:**

- `harness/agents/l2_vector_store.py` (~440 LoC, Protocol + Qdrant + SQLite + factory)
- `harness/agents/l2_retriever.py` (~440 LoC, BM25 + dense + RRF + curator)

**5 MODIFIED:**

- `harness/agents/scratchpad_store.py` (+~30, integration in write_note/delete_note)
- `harness/server/agent/tools.py` (+~80, 2 new tool schemas)
- `harness/server/agent/runtime.py` (+~250, 2 methods + 3 kwargs + Literal)
- `harness/agents/runner.py` (+~10, l2_retriever_factory kwarg — planned Step 4)
- `harness/config.py` (+~30, 2 new settings)

**5 NEW TESTS:**

- `tests/test_l2_vector_store.py` (12 tests) — SqliteL2Store + QdrantL2Store + factory + integration
- `tests/test_l2_retrieval.py` (8 tests) — BM25 + dense + RRF
- `tests/test_l2_curator.py` (15 tests) — curator prompt + JSON parsing + curated_search
- `tests/test_scratchpad_l2_tools.py` (9 tests) — 2 tools + edge cases
- `tests/test_tools.py` (+1, twelve tools)
- `tests/test_runner_scratchpad_factory.py` (+1, SpyToolRuntime signature sync)

**External synced:**

- `C:\MyAI\_output\2026-06\12.06 Harness-Claude-Code-Architecture\roadmap.md`
  (Phase 3 v1.3.0 row → done, 9/12 closed)
- Annotated tag `v1.3.0`

## Next steps (Phase 3 v1.3.1+)

- Tool result offload >25k tokens → v1.3.1
- Cross-session handoff через L2 (continuity) → v1.4.0
- Reflection loop + manual /compact slash → v1.4.0
- Privacy zones + pre-compaction hook → v1.5.0
- HTTP endpoints `/api/v1/context/search` → Phase 4
- Prometheus counters для L2 events → Phase 4
