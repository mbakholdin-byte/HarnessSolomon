# Solomon Harness

**Open-source агентская оболочка поверх open-source LLM (Qwen, DeepSeek, GLM, Llama).**

Сильнее Claude Code и OpenCode за счёт:
- **4-слойной памяти** (working/session/long-term/episodic+semantic) с dual-write
- **KG-RAG** через Neo4j (графовая память, multi-hop reasoning)
- **Cross-encoder rerank** (BGE-reranker-v2-m3)
- **Eval harness** baked-in (SWE-bench-style)
- **RU-first** UX
- **Hot-reload** skills и hooks через file watcher
- **Cost-aware routing** (Haiku-class → локальные, Opus-class → cloud)
- **Docker-sandbox** per agent type с seccomp

## Документация

- `docs/roadmap.md` — дорожная карта (16–17 недель до production)
- `docs/PHASE-0-SPEC.md` — спецификация Фазы 0 (Web MVP)
- `docs/PHASE-0-PLAN.md` — пошаговый план Фазы 0 (11 шагов)
- `docs/hooks.md` — **Phase 4.0 Hooks framework** (14 events, 4 transports, 5 builtin) ⬅️ новое в v1.6.0
- `docs/MODEL_REGISTRY.md` — каталог моделей (T1/T2/T3)
- `docs/architecture.md` — архитектура
- `docs/quickstart.md` — быстрый старт (<10 мин до первого ответа)
- `docs/CHANGELOG.md` — история изменений

## Статус

**Фаза 0 — Web MVP** ✅ (завершено 14.06.2026)

- [x] Backend: FastAPI + LiteLLM (MiniMax-M2.7 / GLM-4.7 / Moonshot-v1-128k)
- [x] Tools: read_file, edit_file, write_file, bash, grep, glob
- [x] Agent loop: max 5 итераций
- [x] WebSocket chat endpoint с streaming
- [x] Frontend: Vite + React 18 + TypeScript
- [x] Chat UI с sessions, models, messages, tool calls
- [x] 67/67 тестов зелёные (62 unit + 5 e2e smoke, real_llm отдельно через `-m real_llm`)
- [x] Quickstart: <10 минут до первого ответа

**Phase 3 — Context Engineering** ✅ (завершено 15.06.2026, v1.0.0–v1.5.0, 12/12)

**Phase 4.0 — Hooks framework** ✅ (завершено 16.06.2026, v1.6.0, 1/12)

- [x] 14 events (PreToolUse, PostToolUse, Stop, SubagentStart/Stop, SessionStart/End, UserPromptSubmit, PreCompact, InstructionsLoaded, PermissionRequest, OnMemoryWrite, OnRoutingDecision, OnCompaction)
- [x] 4 transports (builtin / subprocess / http / llm)
- [x] 5 builtin hooks (log, validate, block_dangerous, inject_context, autosave)
- [x] HookAuditSink (NDJSON observability, opt-in)
- [x] PreToolUse + PostToolUse wired в ToolRuntime
- [x] 1697 tests passing, 0 regressions
- [x] `docs/hooks.md` — полная документация (665 строк, 11 секций)

**Next:** Phase 4.1 — Observability (OpenTelemetry + Prometheus), Phase 4.2 — Hot-reload hooks (file watcher)

## Стек

- **Backend:** Python 3.12+, FastAPI, uvicorn, LiteLLM, aiosqlite, Pydantic v2
- **Frontend:** Vite, React 18, TypeScript, react-markdown
- **LLM Фаза 0:** только облачные (MiniMax-M2.7, GLM-4.7, Moonshot-v1-128k)
- **Локальные модели (Qwen3 8B/30B):** Фаза 0.5
- **Embeddings (BGE-M3, FRIDA):** Фаза 1
- **Memory (hmem, mem0, mempalace, hybrid):** Фаза 1

## Лицензия

MIT
