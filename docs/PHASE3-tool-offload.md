# Phase 3 v1.3.1 — Tool Offload (>25k tokens → L2 scratchpad)

> **Status:** ЗАКРЫТО v1.3.1 (2026-06-15)
> **Tag:** `v1.3.1` (annotated)
> **Tests:** ~1186 mock (от 1146 в v1.3.0, +40 net)
> **Anthropic playbook:** "Offload to file" / "Compress" (close to "Select")

## TL;DR

Phase 3 v1.3.1 закрывает стратегию **"Offload to file"** из
Anthropic context-engineering playbook. Когда tool result превышает
**25 KB** (настраивается), `AgentLoop` записывает полный output в
**L2 scratchpad** и заменяет inline сообщение на **stub** с
указателем (note id + 3-line preview + read hint). Агент может
затем прочитать полное содержимое через
`scratchpad_read_offloaded(id=N)` или найти семантически через
`scratchpad_search_offloaded(query)`.

Главная ценность: 100+ turn сессии с большими tool outputs
(`grep` по большому файлу, `bash` с verbose stdout, `glob` на
большом дереве) больше не сжигают context budget. LLM видит
preview + может pull full body on demand.

## Архитектура (одобрено Марком + Plan agent review)

| Решение | Значение |
|---------|----------|
| **Offload trigger** | Tool message `content` > 25 KB (настраивается `tool_offload_threshold_bytes`, default 25600). Проверка в `AgentLoop._maybe_offload_tool_result()` после каждого `_format_tool_content()`. |
| **Offload storage** | **L2 scratchpad** (existing `ScratchpadStore`, reuse `scratchpad_write_note(level="L2")`). Тег `#tool-offload` + `#tool/{name}`. Storage = `data/agent-jobs.db` (WAL + busy_timeout=5000). L2 = unbounded by design. |
| **Offload format** | Stub: header `[Tool result offloaded: {bytes} bytes, id={note_id}, tool={name}]` + 3-line preview (control chars stripped, max 600 chars) + read hint `scratchpad_read_offloaded(id=N)`. |
| **2 new tools** | `scratchpad_read_offloaded(id, max_bytes?)` — fetch L2 note body (truncated to `tool_offload_read_max_bytes` default). `scratchpad_search_offloaded(query, top_k?)` — reuses v1.3.0 `L2Retriever.curated_search` (filter by `#tool-offload` tag in Python, then hybrid dense+BM25+curator). NO new SQLite LIKE codepath. |
| **Settings (6 new)** | `tool_offload_enabled: bool = True`, `tool_offload_threshold_bytes: int = 25600`, `tool_offload_preview_lines: int = 3`, `tool_offload_preview_max_chars: int = 600`, `tool_offload_read_max_bytes: int = 4096`, `tool_offload_max_ms: int = 2000`. |
| **Trust boundary (factory pattern)** | `runner.py` does NOT import `ToolOffloader`. Runner accepts `offloader_factory: Callable[..., Any] | None` kwarg, mirrors `scratchpad_factory` at `runner.py:231-247`. Factory closure lives in `server/app.py` lifespan. |
| **Session ID resolution** | `AgentLoop` has no `session_id` — read via `getattr(offloader, "_scratchpad", None)` then `getattr(scratchpad, "_session_id", None)`. Mirror pattern at `runtime.py:699`. |
| **Fail-open** | Offload fails (scratchpad None, store error, timeout >max_ms) → keep full content in tool message (no truncation, no error visible to LLM). |
| **Per-call timeout** | `asyncio.wait_for(offloader.offload(...), timeout=tool_offload_max_ms/1000)` — defaults to 2s. Slow / hung SQLite write does not stall the chat loop. |
| **Audit** | When `scratchpad_audit_log=True` — emit `tool_offload` event with `note_id`, `tool_name`, `original_bytes`, `tool_call_id`, `session_id`. `ScratchpadAudit.record` accepts arbitrary event names. |
| **Search reuse** | `_scratchpad_search_offloaded` reuses `self._l2_retriever.curated_search(query, top_k, candidate_k, notes=filtered, router=...)` — same pattern as `_scratchpad_l2_search` at `runtime.py:694-712`. When `l2_retriever is None` → graceful error result. |

## Offload flow

```
Tool execution
  → ToolRuntime.execute(name, args)
    → result = ToolResult(ok, output, error, ...)
  → AgentLoop._maybe_offload_tool_result(content, name, tool_call_id)
    → offloader = getattr(runtime, "_tool_offloader", None)
    → if offloader and offloader.should_offload(content):
        → session_id = getattr(getattr(offloader, "_scratchpad", None), "_session_id", None) or "unknown"
        → asyncio.wait_for(offloader.offload(content, tool_name, session_id, tool_call_id), timeout=2s)
          → note_id = await scratchpad.write_note(L2, content, tags=["#tool-offload", "#tool/{name}"])
        → if note_id: content = offloader.build_stub(content, note_id, tool_name)
  → messages.append({"role": "tool", "content": content})
```

## Stub format (пример)

```
[Tool result offloaded: 31284 bytes, id=42, tool=bash]

total 42
drwxr-xr-x  5 user user   4096 Jun 15 10:23 .
drwxr-xr-x  3 user user   4096 Jun 15 09:45 ..

Read full result via scratchpad_read_offloaded(id=42). Search across offloaded content via scratchpad_search_offloaded(query).
```

## Settings (6 new)

```python
tool_offload_enabled: bool = True                              # master switch
tool_offload_threshold_bytes: int = 25600                     # 25 KB
tool_offload_preview_lines: int = 3                            # stub preview
tool_offload_preview_max_chars: int = 600                      # stub preview cap
tool_offload_read_max_bytes: int = 4096                        # default chunk
tool_offload_max_ms: int = 2000                                # per-call timeout
```

## Trust boundary (сохраняется через все 4 шага)

- `runner.py` does NOT import `ToolOffloader` (preserves `test_runner_does_not_import_scratchpad` symmetry)
- New static test `test_runner_does_not_import_tool_offloader` in `test_agent_runner.py` mirrors the existing one
- All new modules DI'd через constructors (factory callable pattern, mirrors `scratchpad_factory` at `runner.py:231-247`)
- `tool_offloader=None` default в `ToolRuntime` — backward compat
- `offloader_factory=None` default в `AgentRunner.__init__` — backward compat
- Fail-open во всех offload calls (try/except + logger.warning + return None → caller keeps full content)
- Per-call timeout via `asyncio.wait_for(offload, timeout=tool_offload_max_ms/1000)` — keeps LLM loop responsive
- Session ID resolution via `getattr` chain (mirror `runtime.py:699`) — loop reads without direct import
- Search reuses v1.3.0 `L2Retriever.curated_search` (no new SQLite LIKE codepath)
- `ScratchpadAudit.record` accepts arbitrary event names — new `tool_offload` event flows through existing audit infra

## Lessons (для будущих Solomon sessions)

1. **SpyToolRuntime signature sync pattern (recurring)** — `class X(real_X): def __init__(...)` в тестах требует ручной sync при добавлении kwarg в production `__init__`. Lesson: при добавлении kwarg в `ToolRuntime` — grep `tests/` на `class.*Spy|class.*Fake|class.*Stub` subclasses.
2. **getattr chain для session_id в AgentLoop** — loop не имеет `session_id` напрямую, читает через `getattr(offloader, "_scratchpad", None)` → `getattr(scratchpad, "_session_id", None)`. Mirror pattern at `runtime.py:699` (`_scratchpad_l2_search`).
3. **Reuse v1.3.0 L2Retriever, не пиши новый search** — `_scratchpad_search_offloaded` reuses `curated_search` с `notes=filtered_by_tag_in_python`. План v1.3.1 изначально предлагал новый SQLite LIKE, но Plan agent нашёл, что L2Retriever уже умеет.
4. **asyncio.wait_for для per-call timeout** — обернуть `offloader.offload()` в `asyncio.wait_for(..., timeout=2s)` — slow SQLite write не должен stall chat loop.
5. **str.format() escape — НЕ использовать** (recurring). В `ToolOffloader.audit.record` я использовал `record("tool_offload", session_id=...)` — позиционный первый аргумент. Pydantic-style `record(event="...")` безопаснее для MagicMock.
6. **events-based assertion в loop tests** — AgentLoop re-bind'ит `messages` list внутри body (через `redact_dict` в Phase 3). Тесты должны читать `events` (что emit'нул loop), а не `messages` (что увидел caller).

## Файлы (v1.3.1)

**NEW (1):**
- `harness/server/agent/tool_offloader.py` (~280 LoC, ToolOffloader + audit + stub builder)

**MODIFIED (5):**
- `harness/server/agent/runtime.py` (+tool_offloader kwarg + 2 new tool methods + 14-name Literal)
- `harness/server/agent/loop.py` (+_maybe_offload_tool_result helper + asyncio import)
- `harness/server/agent/tools.py` (+2 new TOOL_SCHEMAS: read_offloaded + search_offloaded)
- `harness/agents/runner.py` (+offloader_factory kwarg + wiring in _drive and _stream_drive)
- `harness/config.py` (+6 settings: tool_offload_*)

**TESTS (5 new files + 2 modified):**
- `tests/test_tool_offloader.py` (NEW, 17 tests)
- `tests/test_loop_offload.py` (NEW, 7 tests)
- `tests/test_scratchpad_offload_tools.py` (NEW, 12 tests)
- `tests/test_runner_tool_offload_factory.py` (NEW, 5 tests)
- `tests/test_agent_runner.py` (+test_runner_does_not_import_tool_offloader)
- `tests/test_runner_scratchpad_factory.py` (SpyToolRuntime signature sync)
- `tests/test_tools.py` (test_tool_schemas_contains_twelve_tools → _fourteen_tools)

## Метрики (v1.3.1)

| Параметр | v1.3.0 | v1.3.1 |
|----------|--------|--------|
| HEAD | `d8cede7` | (this tag) |
| Tag | v1.3.0 | v1.3.1 |
| Tests (mock) | 1146 | ~1186 (+40) |
| Tools | 12 | 14 (+2) |
| Settings | 31 | 37 (+6) |
| New modules | — | 1 (tool_offloader.py) |
| New required deps | 0 | 0 |

## Связанные артефакты

- `harness/agents/tool_offloader.py` — ToolOffloader class
- `harness/agents/runner.py` — offloader_factory wiring
- `harness/server/agent/runtime.py` — 2 new tool methods
- `harness/server/agent/loop.py` — offload trigger helper
- `docs/CHANGELOG.md` — Phase 3 v1.3.1 section
- `docs/PHASE3-write.md` — Phase 3 v1.2.0 (Write context)
- `docs/PHASE3-select-compress.md` — Phase 3 v1.3.0 (Select + Compress)

## Out of scope (Phase 3 v1.4.0+ / v1.5.0+)

- Reflection loop + manual `/compact` slash → v1.4.0
- Cross-session handoff through L2 (continuity) → v1.4.0
- Prompt caching (Anthropic cache_control / vLLM prefix cache) → v1.4.0
- Privacy zones + pre-compaction hook → v1.5.0
- Time-based / token-based compaction triggers → v1.5.0
- 12 hooks + observability (Prometheus) → Phase 4
- /api/* → /api/v1/* migration → Phase 4
- eval harness + cascade calibration → Phase 5
