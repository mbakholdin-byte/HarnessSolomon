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
- `docs/MODEL_REGISTRY.md` — каталог моделей (T1/T2/T3)
- `docs/architecture.md` — архитектура
- `docs/quickstart.md` — быстрый старт (в разработке, появится в конце Фазы 0)

## Статус

**Фаза 0 — Web MVP** (1–2 недели, июнь 2026)

- [ ] Backend: FastAPI + LiteLLM (MiniMax-M2.7 / GLM-4.7 / Kimi K2.6)
- [ ] Tools: read_file, edit_file, write_file, bash, grep, glob
- [ ] Agent loop: max 5 итераций
- [ ] WebSocket streaming
- [ ] Frontend: Vite + React/TS chat UI
- [ ] Sessions: SQLite + JSONL
- [ ] Safety: bash deny, path scope
- [ ] Quickstart docs

**Спецификация:** `docs/PHASE-0-SPEC.md` (утверждена 13.06.2026)
**План:** `docs/PHASE-0-PLAN.md` (11 шагов, 16–26 ч работы)

## Стек

- **Backend:** Python 3.12+, FastAPI, uvicorn, LiteLLM, aiosqlite, Pydantic v2
- **Frontend:** Vite, React 18, TypeScript, react-markdown
- **LLM Фаза 0:** только облачные (MiniMax-M2.7, GLM-4.7, Kimi K2.6)
- **Локальные модели (Qwen3 8B/30B):** Фаза 0.5
- **Embeddings (BGE-M3, FRIDA):** Фаза 1
- **Memory (hmem, mem0, mempalace, hybrid):** Фаза 1

## Лицензия

MIT
