# Harness-инжиниринг для собственного агентского фреймворка

**Автор:** Соломон
**Дата:** 12.06.2026
**Контекст:** Собственный harness поверх open-source LLM (Qwen, DeepSeek, GLM), сильнее Claude Code и OpenCode
**Целевая аудитория:** Марк — тех. архитектор, разрабатывает open-source-альтернативу
**Формат:** Дорожная карта + сравнение + каталог техник

---

## TL;DR (главный вывод)

Сильный harness = **4 независимых слоя**, каждый решает конкретный класс проблем:

1. **Контекст-инжиниринг** — какие токены видит модель в каждый момент (compact context, structured notes, sub-agent architectures)
2. **Память** — что переживает сессию: 4 уровня (working/session/long-term/episodic+semantic) с dual-write
3. **Оркестрация** — кто какие задачи решает: маршрутизация, параллелизм, изоляция, evaluator-optimizer
4. **Наблюдаемость + хуки** — что мы видим и контролируем: hooks, structured logs, sandbox

CC сильнее в: MCP-инструментах, hooks-выразительности, prompt caching, sub-agent isolation (worktree).
OpenCode сильнее в: open-source, локальных моделях, plugin-системе, мульти-провайдерности.
**Форк может обогнать обоих** в: deep multi-provider routing, BGE-M3 rerank, knowledge-graph RAG (Neo4j уже в стеке), русскоязычный-first UX, hot-reload skills.

---

## Часть 1. Claude Code — пределы возможностей

### Что есть (по официальной документации, code.claude.com/docs)

**Surfaces (поверхности):** Terminal CLI, VS Code, JetBrains, Desktop app, Web (claude.ai/code), iOS.

**Sub-agents (sub-agents):**
- Встроенные: **Explore** (Haiku, read-only), **Plan** (read-only, планирование), **general-purpose** (наследует модель, все tools)
- Кастомные через `.claude/agents/<name>.md` с YAML frontmatter (name, description, tools, model, permissionMode, maxTurns)
- Каждый субагент = **изолированный контекст** + кастомный system prompt + ограниченные tools + независимые permissions
- **Worktree isolation** через git worktree — субагент работает в отдельной ветке, не трогает основной код
- Background mode: авто-denied prompts, fail silently — нужна осторожность

**Hooks (12 событий):**
| Событие | Когда | Что можно |
|---------|-------|-----------|
| PreToolUse | До вызова tool | Блокировать (exit 2), JSON-feedback |
| PostToolUse | После вызова | Логирование, метрики, корректировка |
| Stop / StopFailure | Главный агент остановился | Автосейв, нотификация |
| SubagentStart / SubagentStop | Запуск/остановка субагента | Логирование, перехват |
| SessionStart / SessionEnd | Начало/конец сессии | Загрузка контекста, чекпойнт |
| UserPromptSubmit | До обработки промпта | Инъекция контекста |
| InstructionsLoaded | Какие файлы загружены | Debugging path-scoped rules |
| Elicitation | MCP-диалог | Автоответ |
| Notification | Уведомления | Forwarding в Telegram/Slack |
| PreCompact | Перед сжатием контекста | Сохранение state |
| PermissionRequest | Запрос разрешения | Auto-approve по правилам |

**Формат:** JSON через stdin, exit code 0 = ok, 2 = blocking error (stderr идёт в модель). Это позволяет делать **guardrails** в виде shell-скриптов, не меняя код CC.

**Permissions:**
- Modes: `default`, `acceptEdits`, `auto` (background-classifier), `dontAsk`, `bypassPermissions`, `plan`
- Правила: glob-patterns per tool, deny по subagent (например, `Agent(name=Explore)` в deny)
- `acceptEdits` — авто-одобрение edit'ов в workdir
- `auto` — фоновый классификатор команд, снижает prompts
- `bypassPermissions` — пропускает ВСЕ проверки, включая `.git`, `.claude` (осторожно!)

**MCP-интеграция:**
- Транспорты: stdio, HTTP (streamable-http), SSE (deprecated), WebSocket
- OAuth 2.0 + DCR + CIMD
- **Tool Search** — деферрал схем MCP-tools до момента использования (экономит контекст)
- Per-tool `anthropic/maxResultSizeChars` до 500k символов
- Headers helper для кастомной аутентификации
- Channels — push events от сервера в активную сессию

**Memory layers в самом CC:**
- `CLAUDE.md` (project, user, system) — префикс системного промпта
- `.claude/rules/*.md` с YAML `paths` frontmatter — path-scoped lazy loading
- `.claude/skills/<name>/SKILL.md` — packaged capabilities
- `settings.json` — MCP, permissions, env
- Auto memory (`~/.claude/projects/<project>/memory/`) — заметки от самого агента
- Per-subagent persistent memory (`~/.claude/agent-memory/<name>/`)

**Models:** Opus 4.7, Sonnet 4.6, Haiku 4.5 (aliases: opus/sonnet/haiku), subagent может наследовать или задаваться отдельно.

### Чего НЕ хватает (weak spots)

1. **Нет first-party multi-provider routing** — CC привязан к Anthropic API. Open-source LLM (Qwen/DeepSeek/GLM) поддерживается только через прокси.
2. **Нет persistent cross-session memory вне проекта** — auto memory per-repo, не глобальная.
3. **Нет first-class evaluator-optimizer** — нет встроенного «агент-критик».
4. **Нет KG-RAG** — нет поддержки графового retrieval (Neo4j, Memgraph, Kuzu).
5. **Hooks не hot-reload** — требуют перезапуска сессии для изменений в логике.
6. **Compaction lossy** — nested CLAUDE.md не перезагружается после /compact.
7. **Subagents cannot spawn subagents** — координация только через main.
8. **Background agents auto-deny prompts** — silent fail.
9. **No native eval harness** — нет встроенного SWE-bench-style evaluation.
10. **Cost transparency** — нет per-task cost breakdown в UI.

---

## Часть 2. OpenCode (SST) — пределы возможностей

**Архитектура:** TypeScript на Bun, open-source MIT, 75+ LLM-провайдеров через AI SDK + Models.dev.

### Что есть

**Surfaces:** TUI (Solid-based), Desktop (BETA: macOS, Windows, Linux), Web, `opencode serve` (HTTP на :4096, mDNS opencode.local), GitHub Action, IDE (VS Code, Cursor, Zed).

**Agents (2 уровня):**
- **Primary (Tab для переключения):** Build (все tools), Plan (read-only)
- **Subagents (@mention или auto-delegate):** General (multi-step), Explore (read-only codebases), Scout (read-only external docs)
- **Hidden system agents:** compaction, title, summary — не настраиваются

**Config (opencode.json, JSONC):**
- 8-слойная иерархия: Remote (.well-known) > Global (~/.config/opencode/) > Custom (OPENCODE_CONFIG) > Project (opencode.json) > .opencode/ > Inline (OPENCODE_CONFIG_CONTENT) > Managed > macOS MDM
- Variable substitution: `{env:VAR}`, `{file:path}`

**Plugins (JS/TS-модули):**
- **30+ событий:** `tool.execute.before/after`, `command.executed`, `file.edited`, `session.{created,compacted,deleted,diff,error,idle}`, `message.{updated,removed}`, `permission.{asked,replied}`, `shell.env`, `experimental.session.compacting`
- Кастомные tools через Zod schema в plugin
- npm-распространение или `.opencode/plugins/`

**MCP:** stdio (local) + HTTP (remote) с OAuth DCR. Нет WebSocket, нет Tool Search deferral.

**Models:** 75+ провайдеров, рекомендованные GPT 5.2, Claude Opus 4.5, Sonnet 4.5, Qwen3 Coder, Gemini 3 Pro, локальные Ollama/LMStudio. Per-provider variants (Anthropic high/max, OpenAI none→xhigh, Google low/high).

**Сильные стороны:**
- Open-source — форк возможен
- Local-first — Ollama, LMStudio работают из коробки
- Plugin SDK — расширения без перекомпиляции
- Мульти-провайдер — Anthropic + OpenAI + Google + локальные

### Чего НЕ хватает (weak spots)

1. **Нет Anthropic-style prompt caching** — нет cache_control, дороже при длинных системных промптах
2. **LSP experimental** — goToDefinition/findReferences часто падают
3. **Нет first-class persistent subagent memory** — только sub-session transcripts
4. **Snapshot system** может тормозить на больших репах с submodule
5. **No WebSocket MCP transport**
6. **Нет `auto` permission mode** — нет background-classifier
7. **Нет KG-RAG built-in** — нужно подключать через MCP
8. **No native eval harness**
9. **No first-party enterprise SSO**

---

## Часть 3. Где open-source форк обгоняет обоих

| # | Возможность | CC | OpenCode | Форк может |
|---|-------------|-----|----------|-----------|
| 1 | Multi-provider cost router (Haiku/Sonnet/Local по задаче) | ❌ | ⚠️ manual | ✅ |
| 2 | Knowledge-Graph RAG (Neo4j/Cypher в retrieval) | ❌ | ❌ | ✅ |
| 3 | Cross-encoder rerank (BGE-reranker-v2-m3) | ❌ | ❌ | ✅ |
| 4 | Hot-reload skills/agents без перезапуска | ❌ | ⚠️ plugin reload | ✅ |
| 5 | Reflection loop (агент извлекает уроки автоматически) | ❌ | ❌ | ✅ |
| 6 | Docker-sandbox per agent type (seccomp profiles) | ⚠️ managed | ❌ | ✅ |
| 7 | Worktree merge queue + reviewer subagent | ❌ | ❌ | ✅ |
| 8 | Domain-aware compaction (учитывает файлы, логи, todo) | ⚠️ базовый | ⚠️ hook | ✅ |
| 9 | Hot-reload hooks (file watcher → обновление логики) | ❌ | ❌ | ✅ |
| 10 | Eval harness baked-in (SWE-bench style) | ❌ | ❌ | ✅ |
| 11 | Zettelkasten auto-linking в Obsidian (KG-based) | ❌ | ❌ | ✅ |
| 12 | RU-first локализация (ошибки, коммиты, slash-команды) | ⚠️ EN-first | ⚠️ EN-first | ✅ |
| 13 | Privacy zones в auto-memory (private/* не пишется) | ❌ | ❌ | ✅ |
| 14 | Cross-session semantic search (Qdrant-backed) | ❌ | ❌ | ✅ |
| 15 | Local model auto-fallback (Ollama down → API) | ❌ | ⚠️ manual | ✅ |

**Главный вывод:** форк сильнее в **integration**: CC-фичи + OpenCode-фичи + GraphRAG + локальные модели + RU-first. Это то, что не делает ни один из вендоров.

---

## Часть 4. Дорожная карта собственного harness

### Фаза 0 — Фундамент (1–2 недели)

**Цель:** MVP, повторяющий 80% CC-функциональности на open-source LLM.

1. **CLI-каркас** (Python/Node) с REPL и slash-командами
2. **Tool runtime:** Read, Edit, Write, Bash, Grep, Glob, WebFetch, WebSearch, TodoWrite
3. **Provider abstraction** (model-agnostic через OpenAI-compatible API):
   - Anthropic (Opus 4.7, Sonnet 4.6, Haiku 4.5)
   - OpenAI (GPT 5.x)
   - Ollama (qwen3:8b, qwen2.5-coder:32b, deepseek-r1)
   - minimax (уже в стеке)
4. **System prompt composition:** CLAUDE.md + hmem essentials + custom rules
5. **Hot context** (этот диалог) + **cold context** (mem0) + **graph context** (mempalace)
6. **Settings.json** с permissions (allow/deny/ask per tool, glob)
7. **Hooks v1:** PreToolUse + PostToolUse + Stop (JSON via stdin, exit 2 = block)
8. **Базовый sub-agent через Task tool** (Plan + Explore + general-purpose)
9. **Worktree isolation** для sub-agents (через git worktree)
10. **Checkpoint / resume** (сессионный JSONL)

**Метрика готовности:** 80% пользовательских сценариев CC работают на qwen3:8b локально.

### Фаза 1 — Память (2–3 недели)

**Цель:** 4-слойная память с dual-write и унифицированной схемой.

1. **Unified Memory schema (Pydantic):**
   ```python
   class Memory(BaseModel):
       id: UUID
       content: str
       layer: Literal['working','session','long','episodic','semantic','procedural']
       source: str          # источник (user/agent/auto)
       ts: datetime
       confidence: float    # 0..1
       ttl: Optional[int]   # секунды
       provenance: Dict[str, Any]  # session_id, model, context
       links: List[UUID]    # связанные memory
   ```
2. **Adapters к существующим системам:**
   - hmem (иерархия P/D/L/E/M) — read+write
   - mem0 (семантика) — через MCP
   - mempalace (KG) — wings/rooms/drawers
   - fas-hybrid-memory (Qdrant+SQLite+OpenSearch) — для больших retrieval
3. **Dual-write:** новый факт → файлы (Markdown, INDEX.md) + mem0 + hmem
4. **BGE-M3 embeddings** (multilingual, top в MTEB) + **FRIDA** (RU-специализация) — для PLAST-домена
5. **Cross-encoder rerank** (bge-reranker-v2-m3) между hybrid retrieval и LLM
6. **Consolidation cron:** daily job — summarize старые episodes, удалять raw после 30 дней
7. **Conflict resolution:** cosine > 0.92 + temporal conflict → newer wins + audit log
8. **Reflection loop:** end-of-session → LLM extract lessons → dual-write в hmem(L:) + mem0
9. **Privacy zones:** auto-memory в `private/*` не пишется

**Метрика:** retrieval precision@5 > 0.85 на внутренних тестах; recall не падает после 1000 сессий.

### Фаза 2 — Оркестрация (2–3 недели)

**Цель:** мульти-агент, маршрутизация, параллелизм.

1. **LLM-as-router:** классификатор задачи → выбор агента/модели
2. **Cost-aware routing:** простые задачи → Haiku/qwen3:8b, сложные → Opus 4.7/GLM-4.6
3. **Workflow DSL** (как CC workflow): agent(), parallel(), pipeline(), phase(), budget()
4. **Structured output enforcement:** JSON-schema для sub-agent (через instructor/outlines)
5. **Worktree-merge queue:** субагент делает PR → reviewer-агент (read-only) проверяет → merge в main
6. **Adversarial verify:** для критичных задач — N-judge panel (2/3 majority)
7. **Loop-until-dry** для research-задач
8. **Background agents** с явным progress + health-check

**Метрика:** время на сложную задачу снижается на 40% за счёт параллелизма.

### Фаза 3 — Контекст-инжиниринг (2 недели)

**Цель:** точно то, что Anthropic описывает в "Effective context engineering" — для open-source LLM.

1. **Compact context** — structured notes вместо полного transcript
2. **Select context** — top-K из memory по запросу, не всё подряд
3. **Compress context** — LLMLingua / sliding window для длинных сессий
4. **Isolate context** — субагенты с own context + scratchpad
5. **Tool result offload** — выводы >25k токенов → файл + ссылка
6. **Pre-compaction hooks** — сохранить state (tasks, decisions, todo) в napkin.md
7. **Anthropic prompt caching** для стабильных префиксов (если Anthropic в роутинге) + vLLM prefix cache для локальных

**Метрика:** средняя сессия 100+ turns без потери важного контекста.

### Фаза 4 — Наблюдаемость + Sandbox (1–2 недели)

**Цель:** production-grade контроль.

1. **Hooks v2:** все 12 событий CC + кастомные (`OnCompaction`, `OnRoutingDecision`, `OnMemoryWrite`)
2. **Structured logging:** JSONL per session, индексация в Qdrant
3. **Tracing:** spans для agent→tool→sub-agent→tool, OpenTelemetry-совместимо
4. **Metrics:** per-task cost, latency, tokens, cache hit rate
5. **Docker-sandbox** per agent type:
   - `readonly` (no write, no network)
   - `web-agent` (HTTP egress через mitmproxy)
   - `build-agent` (full access)
6. **Pre-tool validation:** JSON-schema для всех tool inputs (block invalid)
7. **Auto-eval:** SWE-bench-style task runner, regression detection

**Метрика:** bug escape rate < 5% на стандартных сценариях.

### Фаза 5 — UX (ongoing)

1. **TUI** (Python Textual или TypeScript Solid) — терминальный интерфейс
2. **Slash commands** как CC: `/plan`, `/review`, `/compact`, `/memory`
3. **Skills** — packaged capabilities (PLAST, ЕПУТС, tender, agent)
4. **Hot-reload skills** через file watcher
5. **RU-first** — сообщения об ошибках, коммиты, slash-команды на русском
6. **Web UI** (опционально) — обёртка для multi-user

### Общая оценка timeline: **10–14 недель** до production-ready MVP.

---

## Часть 5. Техники для open-source LLM (Qwen/DeepSeek/GLM)

### Компенсация слабого tool-use

Open-source модели часто хуже CC в tool-use. Техники:

1. **JSON-mode enforcement** — `response_format={"type":"json_object"}` или библиотека `instructor` для typed Pydantic
2. **Outlines / guidance / llama.cpp grammars** — гарантируют формат
3. **Retry-loop with error feedback** — при невалидном JSON возвращаем ошибку в LLM, просим исправить
4. **Schema-in-prompt** — минимальный schema, чтобы модель «видела» структуру
5. **Tool description as few-shot example** — один пример использования tool резко поднимает качество

### Компенсация короткого контекста

- **Hierarchical summarization** — Map-Reduce для длинных документов
- **Retrieval-Augmented Context** — top-K чанков через hybrid retrieval
- **Scratchpad pattern** — модель пишет промежуточные заметки в файл, потом финальный ответ

### Лучшие open-source модели для harness (на 06.2026)

| Модель | Контекст | Tool-use | Сильные стороны |
|--------|----------|----------|-----------------|
| Qwen3-Coder-30B-A3B | 128K | отличный | Coding, function calling |
| Qwen3-235B-A22B | 128K | отличный | Reasoning, generalist |
| DeepSeek-V3 | 64K | хороший | Coding, math, дёшево |
| GLM-4.5/4.6 | 128-200K | хороший | CN/RU bilingual, MCP-friendly |
| Llama-3.3-70B | 128K | средний | Meta, стабильный |
| Mistral-Large-2 | 128K | хороший | EU-альтернатива |
| Phi-4 | 16K | средний | Маленький, быстрый |

**Для harness рекомендую:** Qwen3-Coder (coding tasks), GLM-4.6 (general + RU), DeepSeek-V3 (cost-effective batch).

---

## Часть 6. Сравнительная таблица (50 строк)

См. `cc-vs-opencode.md` (детальная развёрнутая таблица).

**Краткая суть:**

| Слой | CC | OpenCode | Форк |
|------|-----|----------|------|
| License | Проприетарный | MIT | MIT (наш выбор) |
| Multi-provider | ❌ | ✅ 75+ | ✅ 75+ |
| Local models | ⚠️ прокси | ✅ Ollama first-class | ✅ + auto-fallback |
| Sub-agents | ✅ 3 встроенных | ✅ 2 primary + 3 sub | ✅ 4 + кастомные |
| Persistent memory | ✅ per-agent | ❌ | ✅ unified schema |
| KG-RAG | ❌ | ❌ | ✅ Neo4j + mempalace |
| Rerank | ❌ | ❌ | ✅ BGE-reranker |
| Hooks | ✅ 12 событий | ⚠️ 30+ plugin events | ✅ оба + hot-reload |
| Worktree isolation | ✅ | ❌ | ✅ + merge queue |
| Eval harness | ❌ | ❌ | ✅ SWE-bench-style |
| RU-first | ❌ | ❌ | ✅ |
| Hot-reload skills | ❌ | ⚠️ plugin | ✅ |
| Prompt caching | ✅ Anthropic | ❌ | ✅ + vLLM prefix |

---

## Часть 7. Ключевые источники

См. `sources.md` (44 URL с цитатами и статусом).

**Топ-5 must-read:**
1. https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents — философия контекста
2. https://www.anthropic.com/research/building-effective-agents — паттерны workflows vs agents
3. https://code.claude.com/docs/en/sub-agents — как устроены субагенты CC
4. https://code.claude.com/docs/en/hooks — все 12 событий с форматом
5. https://arxiv.org/abs/2504.19413 — mem0 paper, архитектура production memory

---

## Заключение

Создание собственного harness поверх open-source LLM — **амбициозная, но реалистичная задача** на 10–14 недель для MVP. Ключевые преимущества форка:

- **Интеграция сильных сторон CC и OpenCode** без их недостатков
- **Графовая память** (Neo4j + mempalace) — то, чего нет ни у одного конкурента
- **RU-first UX** — критично для русскоязычной команды
- **Полный контроль** — hot-reload, sandbox, eval, custom tools

Рекомендую **начать с Фазы 0 (MVP) на Qwen3-Coder локально**, чтобы за 2 недели получить работающий прототип. Затем добавлять слои памяти и оркестрации.

---

## Архитектура Solomon Harness — Phase 0 Web MVP

> Раздел добавлен 14.06.2026 (Step 11). Описывает текущую реализацию после 11 шагов Фазы 0 (backend + frontend + WebSocket chat).

### Компоненты

```ascii
┌─────────────────────┐
│  Browser (React)    │
│  harness/web/       │
│  port 5173          │
└──────────┬──────────┘
           │ HTTP/WS
           ▼
┌─────────────────────┐
│  Vite dev proxy     │
│  /api → :8765       │
│  ws: true           │
└──────────┬──────────┘
           │ HTTP/WS
           ▼
┌─────────────────────┐
│  FastAPI            │
│  harness/server/    │
│  port 8765          │
│  - REST: /api/*     │
│  - WS: /api/chat/ws │
└──┬─────────┬─────┬──┘
   │         │     │
   ▼         ▼     ▼
┌──────┐ ┌──────┐ ┌─────────┐
│SQLite│ │LLM   │ │Tool     │
│JSONL │ │Router│ │Runtime  │
│      │ │      │ │(subproc)│
└──┬───┘ └──┬───┘ └────┬────┘
   │        │          │
   ▼        ▼          ▼
Sessions  3 models   6 tools
          (cloud)    + safety
```

### Слои

1. **Frontend** — React 18 + TypeScript + Vite (`harness/web/src/`)
2. **API Gateway** — FastAPI + Uvicorn + WebSocket (`harness/server/app.py`)
3. **Agent Loop** — async generator, max 5 iterations (`harness/server/agent/loop.py`)
4. **Tool Runtime** — async subprocess с safety patterns (`harness/server/agent/runtime.py`)
5. **LLM Router** — LiteLLM wrapper для 3 cloud провайдеров (`harness/server/llm/router.py`)
6. **Session Store** — SQLite (index) + JSONL (source of truth) (`harness/server/db/`)

### Endpoints (Phase 0)

| Метод | Путь | Назначение |
|-------|------|------------|
| `GET` | `/api/health` | Healthcheck |
| `GET` | `/api/models` | Каталог моделей + `available` flag |
| `GET` | `/api/sessions` | Список сессий |
| `POST` | `/api/sessions` | Создать сессию |
| `GET` | `/api/sessions/{id}` | Метаданные сессии |
| `PATCH` | `/api/sessions/{id}` | Переименовать / сменить модель |
| `DELETE` | `/api/sessions/{id}` | Удалить сессию |
| `GET` | `/api/sessions/{id}/messages` | История сообщений |
| `WS` | `/api/chat/ws` | Streaming chat (tool-aware) |

### Storage layout

```
harness/data/
├── harness.db              # SQLite — индекс сессий (aiosqlite)
└── sessions/
    └── {session_id}.jsonl   # append-only источник истины
```

На старте, если БД пустая, но в `sessions/*.jsonl` есть данные — вызывается `rebuild_from_jsonl()` (см. `app.py:23-29`).

### Tool runtime

6 встроенных tools (`harness/server/agent/tools.py`):

- `read_file` — прочитать файл (с path-scope под `project_root`)
- `write_file` — создать/перезаписать
- `edit_file` — точечный patch (find/replace)
- `bash` — subprocess с deny-patterns
- `grep` — ripgrep-обёртка
- `glob` — pattern-поиск файлов

**Safety** (`safety.py`): deny-patterns (`rm -rf /`, `del /s C:\`, etc.), path-scope под `settings.project_root`, лимит на размер вывода.

### LLM catalog

3 облачные модели (`harness/server/llm/models.py`):

| ID | Provider | Tier | Context | Pricing in/out ($/1M) | Env var |
|----|----------|------|---------|----------------------|---------|
| `MiniMax-M2.7` | minimax | T3 | 200K | 0.30 / 0.60 | `MINIMAX_API_KEY` |
| `glm-4.7` | zhipuai | T3 | 128K | 0.10 / 0.10 | `ZHIPUAI_API_KEY` |
| `moonshot-v1-128k` | moonshot | T3 | 128K | 0.20 / 0.20 | `MOONSHOT_API_KEY` |

LiteLLM вызывается с префиксом провайдера: `litellm.completion(model="minimax/MiniMax-M2.7", ...)`.

### Frontend ↔ Backend контракт

- REST: `harness/web/src/api/client.ts` (axios-free, `fetch`).
- WebSocket: `harness/web/src/api/ws.ts` — custom клиент с JSON-сообщениями типа:
  - клиент → сервер: `{type: "user", content: "..."}`
  - сервер → клиент: `{type: "token" | "tool_call" | "tool_result" | "done" | "error", ...}`

### Запуск (кратко)

```bash
# Backend (FastAPI, порт 8765)
python -m harness

# Frontend (Vite dev, порт 5173)
cd harness/web && npm install && npm run dev
```

Подробнее: `docs/quickstart.md`.
