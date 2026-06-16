# Дорожная карта собственного Harness

**Проект:** Solomon Harness
**Цель:** open-source агентская оболочка с multi-model (T1/T2/T3), сильнее Claude Code и OpenCode
**Дата:** 12.06.2026 (v1.0), 13.06.2026 (v1.1 — multi-model), 13.06.2026 (v1.2 — Web + облачный MVP), 15.06.2026 (v2.0 — sync v0.4.0), 15.06.2026 (v2.10 — Phase 3 v1.5.0 closeout), **15.06.2026 (v2.11 — Phase 4 status audit)**, **16.06.2026 (v3.0 — Phase 3 = 12/12 FINAL, Phase 4 = 0/12 NOT STARTED)**
**Автор:** Соломон
**Заказчик:** Марк
**Текущий tag:** `v1.5.0` (HEAD `ec8beaf`, 15.06.2026)
**Следующий review:** после Plan agent review + ExitPlanMode на Phase 4 Step 0 → coding → tag v1.6.0

> **Этот файл** — короткий canonical reference внутри репо.
> **Полный source of truth** roadmap (90 KB, актуальный) — `C:\MyAI\_output\2026-06\12.06 Harness-Claude-Code-Architecture\roadmap.md` (v2.11).
> Содержимое Phase 0–3 тут сокращено; детали см. в `_output/.../roadmap.md`.

---

## Изменения v1.2 (13.06.2026)

1. **Web вместо CLI** — Фаза 0: FastAPI backend + React/TS frontend, не Textual CLI
2. **Только облачные API в Фазе 0** — MiniMax-M2.7 (primary), GLM-4.7, Kimi K2.6 через LiteLLM
3. **Локальные модели (Qwen3 8B/30B, Gemma 4)** — перенесены в Фазу 0.5
4. **JSONL — source of truth**, SQLite — индекс (rebuild на старте)
5. **Multi-provider с первого дня** через LiteLLM, без локального Ollama

**Детальная спецификация Фазы 0:** `docs/PHASE-0-SPEC.md`
**Пошаговый план Фазы 0:** `docs/PHASE-0-PLAN.md`

---

## Видение v1.1 (Vision Statement)

Создать **production-ready** агентскую оболочку, которая:
1. Поддерживает **multi-model** (T1/T2/T3 — локальные + облачные open-source LLM) с cost-aware router
2. Работает **локально** на Qwen3 8B / Qwen3-Coder 30B / Gemma 4 12B — **без облака** (Tier 1 + Tier 2)
3. Использует **MiniMax M3 (1M context) / GLM-4.7 / Kimi K2.6** для Tier 3 (сложные задачи, vibe, long-context)
4. Имеет **4-слойную память** с унифицированной схемой (hmem / mem0 / hybrid / file)
5. Поддерживает **мульти-агент** с worktree-изоляцией
6. Имеет **RU-first** UX, **прозрачную стоимость** per-task и **vision** (Gemma 4 / M3)
7. Интегрируется с **существующим стеком Марка** (Qdrant, Neo4j, mem0, hmem, mempalace, OpenCode/Alex/Mavis)
8. Конкурирует с CC и OpenCode по **feature parity** + превосходит в **observability, eval, KG-RAG, multi-model, RU-first**

### Что изменилось в v1.1 (vs v1.0)

| # | Было (v1.0) | Стало (v1.1) |
|---|-------------|---------------|
| 1 | «Работает локально на Qwen3-Coder 30B без облака» | **Multi-model: T1 (8B) + T2 (30B) локально, T3 (API) — облако опционально** |
| 2 | Только open-source LLM в принципе | **Гибрид:** local-first, но с автоматическим fallback на облачные T3 |
| 3 | Qwen3-Coder 30B = default | **Cost-aware router:** Qwen3 8B (T1) / Qwen3-Coder 30B (T2) / GLM-4.7 + M3 + Kimi (T3) по сложности |
| 4 | Нет vision | **Gemma 4 12B (multimodal local) + MiniMax M3 (vision API)** — распознавание схем/документов |
| 5 | 128K context | **1M context через MiniMax M3** (подтверждено Марком 13.06.2026) |
| 6 | DeepSeek в каталоге | **DeepSeek исключён** (решение Марка) |

---

## Стратегические принципы (v1.1)

1. **Multi-model first** — единый LiteLLM-абстрактор для локальных + облачных, cost-aware router с первого дня
2. **Open-source first** — MIT license, не зависеть от Anthropic/OpenAI
3. **Local-first** — должен работать без облака на T1 (8B) + T2 (30B) моделях; облако — опционально для T3
4. **Composability** — модули независимы, можно менять по одному
5. **Progressive enhancement** — MVP за 2 недели, фичи добавляются инкрементально
6. **Dual-write** — изменения в памяти дублируются в файлы (для human-review)
7. **Observability by default** — каждое действие логируется, метрики экспортируются
8. **Cost transparency** — пользователь видит, сколько стоит каждый task
9. **Model-portability** — смена провайдера не ломает workflow; capability detection обязателен
10. **Inter-agent compatibility** — работает в одной связке с Alex / Mavis / OpenCode через `orchestrator-inbox/`
7. **Cost transparency** — пользователь видит, сколько стоит каждый task

---

## Фаза 0 — MVP (недели 1–2) — **Web, только облачные API**

### Цель
Запустить работающий Web-MVP: chat-интерфейс в браузере, multi-provider через облачные API, 6 tools, session persistence.

**Полная спецификация:** `docs/PHASE-0-SPEC.md`
**План:** `docs/PHASE-0-PLAN.md`

### Scope
- **Backend:** FastAPI + uvicorn, LiteLLM (3 облачных провайдера: MiniMax-M2.7, GLM-4.7, Kimi K2.6)
- **Frontend:** Vite + React 18 + TypeScript, chat UI с streaming через WebSocket
- **Tools (6):** read_file, edit_file, write_file, bash, grep, glob — выполняются server-side
- **Agent loop:** max 5 итераций per task
- **Sessions:** SQLite (метаданные) + JSONL (source of truth) в `data/`
- **Safety:** bash deny по regex, paths в пределах project_root
- **Без memory, без sub-agents, без hooks** — это Фазы 1, 2, 4

### Definition of Done
- [ ] Backend на :8000, Frontend на :5173, Vite proxy /api → :8000
- [ ] 5 smoke-сценариев проходят на MiniMax-M2.7 (mock-тесты + 1+ real-LLM):
  1. Read файл + ответ
  2. Edit файл по описанию
  3. Grep + анализ результатов
  4. WebFetch через bash (curl) + summarize
  5. Multi-turn диалог с сохранением истории
- [ ] 3 модели доступны через `/api/models` (с флагом `available` по наличию ключа)
- [ ] WebSocket streaming: token + tool_call + tool_result + message_done + session_done
- [ ] Сессия сохраняется в SQLite + JSONL, resume по session_id
- [ ] Safety: deny опасных bash-команд (`rm -rf /`, `git push --force` и т.д.)
- [ ] `docs/quickstart.md` пошагово от запуска до первого сообщения
- [ ] README обновлён, ссылка на quickstart

### Стек
- **Backend:** Python 3.12+, FastAPI, uvicorn, websockets, aiosqlite, Pydantic v2, LiteLLM
- **Frontend:** Vite, React 18, TypeScript, react-markdown
- **LLM:** LiteLLM (MiniMax, ZhipuAI, Moonshot) — без локальных
- **Хранилище:** SQLite + JSONL (dual-write)

---

## Фаза 0.5 — Multi-Model Support (неделя 3)

### Цель
Поддержка каталога моделей (см. `docs/MODEL_REGISTRY.md`) — T1/T2/T3, локальные + облачные.

### Scope
- [ ] `config/models.yaml` с провайдерами (Ollama, Zhipu, Moonshot, Alibaba, MiniMax)
- [ ] Provider abstraction (interfaces: `LocalProvider`, `CloudProvider`)
- [ ] Per-model capability flags (vision, tool-use, json-mode, long-context)
- [ ] Model loader с auto-fallback chain
- [ ] CLI: `/model <name>` для переключения, `/models` для списка
- [ ] Per-model pricing + cost logging

### Definition of Done
- [ ] Все 3 категории моделей доступны:
  - T1 (Qwen3 8B локально)
  - T2 (Qwen3-Coder 30B локально)
  - T3 (GLM-4.7 / Kimi K2.6 / MiniMax M2.7 облако)
- [ ] Auto-fallback работает (T3 → T2 → T1)
- [ ] Per-task cost logging

---

## Фаза 1 — Память (недели 4–6)

### Файлы
```
harness/
├── pyproject.toml
├── README.md
├── harness/
│   ├── __init__.py
│   ├── cli.py
│   ├── agent.py
│   ├── tools/
│   │   ├── read.py
│   │   ├── edit.py
│   │   ├── write.py
│   │   ├── bash.py
│   │   ├── grep.py
│   │   ├── glob.py
│   │   ├── webfetch.py
│   │   ├── websearch.py
│   │   └── todowrite.py
│   ├── providers/
│   │   ├── base.py
│   │   ├── anthropic.py
│   │   ├── openai.py
│   │   ├── ollama.py
│   │   └── litellm_router.py
│   ├── permissions.py
│   ├── session.py
│   └── settings.py
└── tests/
    └── test_smoke.py
```

---

## Фаза 1 — Память (недели 4–6)

### Цель
4-слойная память с dual-write, retrieval и rerank.

### Scope
1. **Unified Memory schema** (Pydantic):
   - `Memory` с полями: id, content, layer, source, ts, confidence, ttl, provenance, links
2. **Adapters:**
   - `hmem` adapter (read/write через hmem MCP)
   - `mem0` adapter (read/write через mem0 MCP)
   - `mempalace` adapter (KG operations)
   - `fas-hybrid-memory` adapter (для retrieval)
   - `file` adapter (Markdown + INDEX.md)
3. **Dual-write:** запись в 2+ слоя одновременно
4. **Retrieval pipeline:**
   - Query → hybrid (BM25 + vector) → top-50
   - Cross-encoder rerank (bge-reranker-v2-m3) → top-10
   - Context assembly → LLM
5. **Compaction:** авто-сжатие при 80% контекста
6. **Pre-compaction hook:** сохранить state в napkin.md
7. **Reflection loop:** end-of-session → LLM extract lessons → dual-write
8. **Privacy zones:** `private/*` не пишется в auto-memory
9. **Embedding models:** BGE-M3 (general) + FRIDA (RU-домен)

### Definition of Done
- [ ] Memory schema стабилизирована, миграция на неё
- [ ] Все 5 адаптеров работают в dual-write
- [ ] Retrieval precision@5 > 0.85 на тестовом наборе
- [ ] Compaction не теряет важные факты (golden tests)
- [ ] Reflection loop создаёт lessons при завершении задачи
- [ ] Privacy zones работают
- [ ] Документация: `docs/memory.md`

### Стек
- + Qdrant (векторное хранилище)
- + Neo4j (графовое)
- + BGE-M3 (embeddings)
- + bge-reranker-v2-m3 (rerank)
- + instructor (typed outputs)

### Файлы
```
harness/memory/
├── schema.py            # Pydantic models
├── unified.py           # Unified Memory facade
├── adapters/
│   ├── hmem.py
│   ├── mem0.py
│   ├── mempalace.py
│   ├── hybrid.py
│   └── file.py
├── retrieval/
│   ├── hybrid.py
│   ├── rerank.py
│   └── assembly.py
├── consolidation.py
├── reflection.py
└── privacy.py
```

---

## Фаза 1.6 — Scope-gated API (отложенный, для будущей интеграции) ⭐ NEW

### Цель
Шаблон scope-gated API для будущей интеграции нашего harness с другими оболочками (или для standalone-режима вне Claude Code). Позаимствован из Odysseus (`pewdiepie-archdaemon/odysseus`, `integrations/claude/`).

### Scope
- [ ] `GET /api/v1/capabilities` — клиент узнаёт, что разрешено
- [ ] `Bearer`-токен в env (`SOL_HARNESS_URL`, `SOL_HARNESS_API_TOKEN`)
- [ ] Server-side scope check на каждый endpoint
- [ ] Scope-store: token → список capabilities (`memory.read`, `memory.write`, `tools.bash`, ...)
- [ ] Helper-script `sol-api.py` с subcommand: `todos list`, `memories search QUERY`, `tools run NAME`
- [ ] `SKILL.md` bundle для Claude Code / Codex (установка одной командой)

### Зачем
- **Prompt-injection protection**: модель не может «угадать» scope, сервер сам объявляет capabilities
- **Multi-tenant**: разные токены — разные права
- **Self-hosted standalone**: если пользователь запускает harness **без** Claude Code (через Web UI или CLI), scope-gated API даёт ту же модель безопасности

### Когда делать
- **Не блокирует MVP** — делаем в Фазе 2+ когда появится `agent-router`
- **Сейчас**: зафиксировать паттерн в `docs/techniques-catalog.md` (раздел 11)

---

## Фаза 2 — Sub-agents (недели 8–10)

### Цель
Мульти-агент с маршрутизацией, изоляцией, parallel execution.

### Scope
1. **Sub-agent system** по CC-style:
   - Определение через `.harness/agents/<name>.md` + YAML frontmatter
   - Изолированный контекст
   - Свои tools, permissions, model
2. **Worktree isolation** через git worktree
3. **Built-in агенты:**
   - `explore` (read-only, Haiku/qwen3:8b)
   - `plan` (read-only, генерация плана)
   - `code` (full access, Sonnet/qwen3-coder-30b)
   - `review` (read-only, ревью diff)
4. **LLM-as-router:** классификатор задачи → выбор агента
5. **Cost-aware routing:** простые → локальные, сложные → cloud
6. **Background mode:** async + progress reporting
7. **Adversarial verify:** для критичных задач — 2/3 majority judges
8. **Merge queue:** субагент делает PR → reviewer-agent → merge

### Definition of Done
- [ ] 4 built-in агента работают
- [ ] Custom агент через .md-файл
- [ ] Worktree isolation чистая (нет merge conflicts с main)
- [ ] Router распределяет задачи по сложности
- [ ] Adversarial verify ловит hallucination (golden tests)
- [ ] Merge queue работает end-to-end
- [ ] Документация: `docs/subagents.md`

### Стек
- + Git worktree (built-in)
- + Pydantic AI или кастомный agent loop

### Файлы
```
harness/agents/
├── base.py              # Agent класс
├── registry.py          # Загрузка .md-файлов
├── runner.py            # Async runner
├── router.py            # LLM-as-router
├── worktree.py          # Git worktree integration
├── merge_queue.py
└── builtin/
    ├── explore.md
    ├── plan.md
    ├── code.md
    └── review.md
```

---

## Фаза 3 — Контекст-инжиниринг (недели 11–13)

### Цель
Реализовать 4 стратегии Anthropic (write/select/compress/isolate) для open-source LLM.

### Scope
1. **Write context:** scratchpad.md, notes.md, plan.md
2. **Select context:** retrieval-based, top-K не всё
3. **Compress context:**
   - Hierarchical summarization (L0/L1/L2/L3)
   - LLMLingua integration
   - Sliding window + attention sinks
4. **Isolate context:** sub-agents (уже из Фазы 2)
5. **Tool result offload:** >25k токенов → файл
6. **Pre-compaction hook:** save state, hot-list
7. **Compaction strategies:**
   - Time-based (каждые N turns)
   - Token-based (при 80% context)
   - Manual (`/compact`)
8. **Prompt caching:**
   - Anthropic cache_control (если Anthropic)
   - vLLM prefix cache (если локальные)

### Definition of Done
- [ ] 100+ turn сессия не теряет контекст
- [ ] Compaction trigger настраивается
- [ ] Tool offload работает (golden tests)
- [ ] Prompt caching снижает стоимость на 50%+ (метрики)
- [ ] Документация: `docs/context.md`

### Файлы
```
harness/context/
├── manager.py           # Compaction + offload
├── summarizer.py        # Hierarchical summary
├── compressor.py        # LLMLingua wrapper
├── offload.py           # Tool result offload
├── cache.py             # Prompt caching
└── hooks.py             # PreCompact hook
```

---

## Фаза 4 — Hooks & Observability (недели 14–15)

### Цель
12 hook-событий + структурированное логирование + метрики.

### Scope
1. **Hooks (все 12 событий CC + кастомные):**
   - PreToolUse, PostToolUse, Stop, SubagentStart/Stop
   - SessionStart/End, UserPromptSubmit, PreCompact
   - InstructionsLoaded, Elicitation, Notification, PermissionRequest
   - + OnMemoryWrite, OnRoutingDecision, OnCompaction
2. **Hook формат:** JSON via stdin, exit 0=ok/2=block
3. **HTTP hooks:** внешние endpoint'ы для интеграций
4. **LLM-as-hook:** промпт-хук для решений на основе LLM
5. **Structured logging:** JSONL per session, индексация в Qdrant
6. **Tracing:** OpenTelemetry-совместимые spans
7. **Metrics:** per-task cost, latency, tokens, cache hit rate
8. **Health checks:** liveness, readiness, deep

### Definition of Done
- [ ] Все 12+ hook событий работают
- [ ] Hot-reload hooks через file watcher
- [ ] Structured logs индексируются
- [ ] Метрики экспортируются в Prometheus
- [ ] Health checks отвечают на /health
- [ ] Документация: `docs/hooks.md`, `docs/observability.md`

### Файлы
```
harness/hooks/
├── registry.py          # Все 12 событий
├── runner.py            # JSON in/out
├── http.py              # HTTP hooks
├── llm_hook.py          # LLM-as-hook
└── builtin/
    ├── log.py
    ├── validate.py
    ├── block_dangerous.py
    ├── inject_context.py
    └── autosave.py

harness/observability/
├── tracer.py
├── metrics.py
├── health.py
└── exporter.py
```

---

## Фаза 5 — Eval & Production Hardening (недели 16–17)

### Цель
Eval harness + production-grade надёжность.

### Scope
1. **Eval harness:**
   - SWE-bench-style task runner
   - A/B test models
   - Regression detection
   - Golden tests
2. **Sandbox:**
   - Docker per agent type
   - seccomp profiles
   - Network policies (mitmproxy)
3. **Security:**
   - JSON-schema validation для всех tool inputs
   - PII redaction
   - Action audit log
4. **Performance:**
   - Streaming responses
   - Caching (prompt, semantic, result)
   - Rate limiting per provider
5. **Cost optimization:**
   - Token usage tracking
   - Cost per task
   - Budget alerts

### Definition of Done
- [ ] Eval harness прогоняет 50+ тестов за < 1 час
- [ ] Regression detection ловит деградации
- [ ] Docker sandbox работает
- [ ] PII redaction не пропускает чувствительные данные
- [ ] Cost tracking виден пользователю
- [ ] Документация: `docs/eval.md`, `docs/security.md`

### Файлы
```
harness/eval/
├── runner.py
├── benchmark.py
├── ab_test.py
└── regression.py

harness/sandbox/
├── docker.py
├── seccomp.py
└── network.py
```

---

## Фаза 6 — UX и интеграции (после MVP, ongoing)

### Цель
UX, web-обёртка, интеграции с IDE.

### Scope
1. **TUI improvements:** syntax highlighting, progress bars, command palette
2. **Web UI:** React/Vue фронт для multi-user
3. **IDE интеграции:** VS Code, JetBrains расширения
4. **Slash commands library:** встроенные (PLAST, ЕПУТС, tender, agent)
5. **RU-first:** все сообщения, коммиты, slash-команды
6. **GitHub Action:** headless run в CI

### Когда делать
- TUI: сразу после Фазы 0
- Web UI: после Фазы 4
- IDE: по запросу
- Slash library: параллельно с Фазой 2

---

## Метрики успеха

### Технические
- **Context retention:** 95% важной информации переживает 100+ turn сессию
- **Retrieval precision@5:** > 0.85
- **Retrieval recall@20:** > 0.90
- **Compaction loss:** < 5% (golden tests)
- **Tool-use success rate:**
  - T1 (Qwen3 8B): > 80%
  - T2 (Qwen3-Coder 30B): > 92%
  - T3 (GLM-4.7): > 96%
- **Sub-agent merge success:** > 90% без конфликтов
- **Eval pass rate:** > 80% на SWE-bench-style tasks
- **Model routing accuracy:** > 90% правильных выборов модели

### Пользовательские
- **Time-to-task:** снижение на 40% vs CC на аналогичных задачах
- **Cost per task:** прозрачный, < $0.50 на типичный coding-task
- **Local availability:** работает offline на qwen3-coder-30b
- **RU support:** все сообщения + коммиты на русском

### Качественные
- **Harness adoption:** используется Марком ежедневно в течение 30 дней
- **Sub-agent ecosystem:** 10+ кастомных агентов в библиотеке
- **Community:** 3+ внешних контрибьютора (если open-source)

---

## Риски и митигация

| Риск | Вероятность | Импакт | Митигация |
|------|-------------|--------|-----------|
| Open-source LLM не дотягивает по tool-use | Средняя | Высокий | Compensating techniques (instructor, retries) + fallback на Opus для сложных задач |
| Scope creep | Высокая | Средний | Строгие Definition of Done per фаза, не добавлять фичи без одобрения |
| MCP-серверы ломаются | Средняя | Высокий | Health checks + auto-restart, fallback на mock-данные |
| Memory corruption | Низкая | Высокий | Dual-write в файлы (source of truth), регулярные backups |
| Vendor lock-in на конкретную БД | Низкая | Средний | Использовать стандартные клиенты (qdrant-client, neo4j-driver), не ORM |
| Privacy утечки в auto-memory | Средняя | Высокий | Privacy zones, manual review UI, opt-in для auto-memory |

---

## Ресурсы

### Время
- **Фаза 0:** 2 недели (1 разработчик full-time)
- **Фазы 1–5:** по 2–3 недели каждая
- **Итого до production-ready:** 12–14 недель
- **Поддержка после:** ongoing, 0.5 FTE

### Инфраструктура
- **Локально:** 64GB RAM + RTX 4090 (для qwen3-coder-30b)
- **Облако:** Anthropic API key (для Opus/Sonnet fallback), OpenAI key
- **Хранилища:** Qdrant, Neo4j, OpenSearch (всё уже в стеке Марка)

### Знания
- Anthropic docs (CC, prompt caching, context engineering)
- LangGraph, Letta, mem0 GitHub
- 44 URL в `sources.md`

---

## Следующие шаги (что делать прямо сейчас)

1. **Создать репо:** `C:\MyAI\06_Harness\` (рядом с 05_TaskGraph)
2. **Скопировать структуру из Фазы 0** в `pyproject.toml`
3. **Установить зависимости:** litellm, textual, pydantic, instructor
4. **Проверить Ollama:** `ollama pull qwen3:8b` (уже сделано)
5. **Сделать smoke test:** 5 базовых сценариев из Definition of Done
6. **Записать в napkin.md:** решение о старте, какие фичи в MVP

---

**Roadmap версия:** 1.0
**Дата:** 12.06.2026
**Следующий review:** после завершения Фазы 0 (через 2 недели)
