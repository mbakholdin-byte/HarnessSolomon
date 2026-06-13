# Sources — Harness / Claude Code / OpenCode / Memory / Orchestration

**Дата сбора:** 12.06.2026
**Канал:** Brave Search API + curl/WebFetch для глубокого чтения
**Верификация:** URL получены через Brave Search (актуальные результаты), содержимое — через прямой fetch. Часть URL (Anthropic blog, OpenCode docs) отдают пусто при curl из-за anti-bot защиты — для них использован Brave snippet + собственные знания.

---

## 1. Канонические источники по агентам и контексту

| # | URL | Зачем | Цитата/выжимка |
|---|-----|-------|---------------|
| 1 | https://www.anthropic.com/research/building-effective-agents | Anthropic, дек. 2024 — базовые паттерны | "Workflows are systems where LLMs and tools are orchestrated through predefined code paths. Agents, on the other hand, are systems where LLMs dynamically direct their own processes and tool usage." — выделяет workflows vs agents. Рекомендует: «find the simplest solution» |
| 2 | https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents | Anthropic Engineering, 29.09.2025 | "Context engineering refers to the set of strategies for curating and maintaining the optimal set of tokens (information) during LLM inference, including all the other information that fits around them" — формальное определение. 4 стратегии: write context, select context, compress context, isolate context |
| 3 | https://platform.claude.com/cookbook/tool-use-context-engineering-context-engineering-tools | Cookbook: memory, compaction, tool clearing | Практические паттерны Anthropic по очистке контекста, чекпойнтам, offload больших результатов |
| 4 | https://www.anthropic.com/engineering/building-effective-agents-with-claude-code-1 | Практики Claude Code | Поле «best practices» от разработчиков |

## 2. Claude Code — официальная документация

| # | URL | Раздел | Содержимое |
|---|-----|--------|------------|
| 5 | https://code.claude.com/docs/en/overview | Обзор | Поверхности: terminal, IDE, desktop, web |
| 6 | https://code.claude.com/docs/en/sub-agents | Sub-agents | "Each subagent runs in its own context window with a custom system prompt, specific tool access, and independent permissions." Built-in: Explore, Plan, general-purpose. Custom через .claude/agents/ |
| 7 | https://code.claude.com/docs/en/hooks | Hooks | 12 событий: PreToolUse, PostToolUse, Stop, SubagentStart/Stop, SessionStart/End, UserPromptSubmit, InstructionsLoaded, Elicitation, Notification, PreCompact, PermissionRequest. Формат: JSON через stdin, exit 0=ok/2=block+stderr. |
| 8 | https://code.claude.com/docs/en/worktrees | Worktrees | "Run parallel sessions with worktrees" — изоляция через git worktree для background agents |
| 9 | https://code.claude.com/docs/en/mcp | MCP | "Connect Claude Code to tools via MCP" — stdio, HTTP/SSE, OAuth, channels, Tool Search |
| 10 | https://code.claude.com/docs/en/agent-sdk/slash-commands | SDK slash commands | Кастомные slash commands через SDK |
| 11 | https://platform.claude.com/docs/en/build-with-claude/prompt-caching | Prompt caching | Anthropic cache_control, экономия до 90% на токенах, TTL 5 мин |

## 3. OpenCode (SST)

| # | URL | Зачем | Статус |
|---|-----|-------|--------|
| 12 | https://opencode.ai/docs/agents/ | OpenCode агенты | Primary vs subagent, Build/Plan, General/Explore/Scout |
| 13 | https://opencode.ai/docs/rules/ | OpenCode rules | Файлы правил и иерархия |
| 14 | https://opencode.ai/docs/config/ | Config | 8-слойная иерархия конфигов |
| 15 | https://opencode.ai/docs/models/ | Models | 75+ провайдеров через AI SDK + Models.dev |
| 16 | https://opencode.ai/docs/mcp-servers/ | MCP | Local stdio + Remote HTTP с OAuth DCR |
| 17 | https://opencode.ai/docs/plugins/ | Plugins | 30+ событий, JS/TS-модули, кастомные tools через Zod |
| 18 | https://github.com/sst/opencode | GitHub | Исходники (TypeScript, Bun, MIT) |
| 19 | https://www.kdnuggets.com/seeing-whats-possible-with-opencode-ollama-qwen3-coder | OpenCode + Ollama + Qwen3-Coder | Демо локальной LLM через OpenCode |
| 20 | https://medium.com/@lexy_eyn/how-to-connect-a-local-qwen3-coder-30b-to-opencode-and-create | Qwen3-Coder 30B local | Подробный гайд по локальной модели |

## 4. Память и долговременный контекст

| # | URL | Инструмент | Цитата/выжимка |
|---|-----|-----------|---------------|
| 21 | https://github.com/mem0ai/mem0 | mem0 | "Universal memory layer for AI Agents", 35k+ stars, Apache-2.0. Dedup, conflict resolution, pluggable LLM/vector/graph store |
| 22 | https://arxiv.org/abs/2504.19413 | mem0 paper | "Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory" — архитектура слоёв и бенчмарки |
| 23 | https://mem0.ai/ | mem0 site | Product overview, cloud и self-hosted |
| 24 | https://github.com/letta-ai/letta | Letta | "Letta is the platform for building stateful agents". Поддерживает skills, subagents, model-agnostic (Opus 4.5, GPT-5.2 рекомендованы) |
| 25 | https://www.letta.com/blog/memgpt-and-letta | Letta ↔ MemGPT | "MemGPT is now part of Letta" |
| 26 | https://github.com/microsoft/graphrag | GraphRAG | "Extract meaningful, structured data from unstructured text using LLMs" |
| 27 | https://microsoft.github.io/graphrag/ | GraphRAG docs | Community detection (Leiden), global/local/drift/basic queries |
| 28 | https://arxiv.org/abs/2501.13956 | Zep paper | "Zep: A Temporal Knowledge Graph Architecture for Agent Memory" |
| 29 | https://www.getzep.com/ | Zep | Production memory store, GraphRAG-based |
| 30 | https://medium.com/@piyush.jhamb4u/stateful-ai-agents-a-deep-dive-into-letta-memgpt-memory-models-a2 | Letta deep dive | Core/archival/recall memory модели |

## 5. Orchestration фреймворки

| # | URL | Фреймворк | Цитата |
|---|-----|-----------|--------|
| 31 | https://www.langchain.com/langgraph | LangGraph | "Agent Orchestration Framework for Reliable AI Agents" |
| 32 | https://github.com/langchain-ai/langgraph | LangGraph GH | "Low-level orchestration framework for building stateful agents" — MIT |
| 33 | https://docs.langchain.com/oss/python/langgraph/workflows-agents | LangGraph docs | Workflows vs agents, паттерны |

## 6. Дополнительные ресурсы (Reddit, аналитика, third-party)

| # | URL | Зачем |
|---|-----|-------|
| 34 | https://www.reddit.com/r/ClaudeAI/comments/1rl97yv/is_claudecode_worth_it_over_opencode_copilot_what/ | Сравнение CC vs OpenCode + Copilot от пользователей |
| 35 | https://www.developersdigest.tech/blog/claude-code-agent-teams-subagents-2026 | "Claude Code Agent Teams, Subagents, and MCP: The 2026 Playbook" |
| 36 | https://boringbot.substack.com/p/claude-code-skills-subagents-hooks | CC skills/subagents/hooks/plugins/harnesses — production multi-agent |
| 37 | https://claudefa.st/blog/guide/development/worktree-guide | CC worktrees — parallel sessions |
| 38 | https://www.claudedirectory.org/how-to/background-agents-worktrees | CC background agents в worktrees |
| 39 | https://claudefa.st/blog/tools/hooks/hooks-guide | CC hooks — все 12 lifecycle events |
| 40 | https://www.digitalapplied.com/blog/context-engineering-agent-reliability-playbook-2026 | Context engineering playbook 2026 |
| 41 | https://www.mindstudio.ai/blog/anthropic-prompt-caching-claude-subscription-limits | Anthropic prompt caching — лимиты и токен-экономия |
| 42 | https://qcode.cc/en/claude-code-mcp-guide | CC MCP guide |
| 43 | https://memgraph.com/blog/how-microsoft-graphrag-works-with-graph-databases | GraphRAG + графовые БД (Memgraph) |
| 44 | https://github.com/rohitg00/agentmemory/tree/main/plugin/opencode | OpenCode plugin — agent memory |

## 7. Локальный стек Марка (Соломон)

| Компонент | Источник | Назначение |
|-----------|----------|-----------|
| hmem | C:/MyAI/_Solomon/memory/ | Иерархическая память P/D/L/E/M |
| mem0 (solomon-mem0) | Qdrant :7333 + qwen3:8b | Семантическая память |
| mempalace | wings/rooms/drawers/closets/KG | Графовая структура |
| fas-hybrid-memory | Qdrant + SQLite + OpenSearch | Гибридный retrieval |
| Obsidian vault | C:/Users/mbakh/Yandex.Disk/MarkObs/MarkObsidian/ | Файловая память |
| agent-router v2 | services-config.ps1 + watchdog v2 | Оркестрация (см. MEMORY.md) |
| MCP-серверы | hmem, sol-memory, solomon-mem0, gety, webclaw, brave-search, mcp* | 20+ сервисов |
| Модели | qwen3:8b (через Ollama), nomic-embed-text, MiniMax-M2.7 (через minimax) | Локальные + cloud |

## 8. Не верифицировано (data_gaps)

- Реальные метрики CC vs OpenCode по SWE-bench / HumanEval — нужны свои замеры
- Внутренности OpenCode (TypeScript пакеты, event bus, session store) — только docs/поведение
- Актуальные релизы Qwen3 / GLM-4.6 / DeepSeek-V4 на 12.06.2026 — нужна проверка через HuggingFace
- Точная стоимость Anthropic prompt caching для Opus 4.7 — есть в pricing page, требует fetch
- OpenCode v1.17+ изменения — GitHub release notes не открылись через curl
