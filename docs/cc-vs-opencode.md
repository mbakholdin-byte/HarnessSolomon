# Claude Code vs OpenCode — детальное сравнение

**Дата:** 12.06.2026
**Источники:** code.claude.com/docs, opencode.ai/docs, github.com, arxiv.org, anthropic.com

## Легенда

- ✅ есть, зрелое
- ⚠️ есть, ограниченно
- ❌ нет
- 🔧 форк может обогнать обоих

## Архитектура

| Возможность | Claude Code | OpenCode | Форк может |
|-------------|-------------|----------|-----------|
| Open-source | ❌ proprietary | ✅ MIT | ✅ |
| Язык реализации | TypeScript/native | TypeScript/Bun | любой |
| Локальные модели | ⚠️ через прокси | ✅ Ollama first-class | ✅ + auto-fallback |
| Multi-provider routing | ❌ Anthropic-only | ✅ 75+ через AI SDK | ✅ cost-aware |
| MCP клиент | ✅ stdio/HTTP/SSE/WS | ✅ stdio/HTTP | ✅ + WS |
| MCP Tool Search (deferral) | ✅ | ❌ | ✅ |
| Docker sandbox | ⚠️ managed | ❌ | ✅ seccomp profiles |
| Hot-reload конфига | ❌ | ⚠️ plugin reload | ✅ file watcher |
| Web UI | ✅ claude.ai/code | ✅ opencode web | ✅ |
| TUI | ✅ | ✅ (Solid) | ✅ |
| Desktop app | ✅ | ⚠️ BETA | ✅ |
| IDE интеграция | ✅ VS Code/JetBrains | ✅ VS Code/Cursor/Zed | ✅ |

## Sub-agents

| Возможность | Claude Code | OpenCode | Форк |
|-------------|-------------|----------|------|
| Встроенные типы | Explore, Plan, general-purpose (3) | Build, Plan, General, Explore, Scout (5) + hidden 3 | 4+ кастомные |
| Изолированный контекст | ✅ | ✅ | ✅ |
| Worktree isolation | ✅ git worktree | ⚠️ через plugin | ✅ + merge queue |
| Спавн субагентов | ❌ (subagent → main only) | ⚠️ Task glob | ✅ с лимитом глубины |
| Persistent memory | ✅ ~/.claude/agent-memory/ | ❌ sub-session only | ✅ unified |
| Рекурсия | ❌ | ⚠️ через Task | ✅ configurable depth |
| Background mode | ✅ + auto-deny | ⚠️ sub-session | ✅ + progress |
| Resume по ID | ✅ agent_id → .jsonl | ⚠️ sub-session nav | ✅ |

## Память

| Возможность | Claude Code | OpenCode | Форк |
|-------------|-------------|----------|------|
| CLAUDE.md / AGENTS.md | ✅ walk-up, hot | ✅ | ✅ + import graph |
| Path-scoped rules | ✅ .claude/rules/ + paths frontmatter | ✅ .opencode/rules/ + instructions array | ✅ |
| Auto memory (агент пишет сам) | ✅ per-project | ❌ | ✅ + privacy zones |
| Vector retrieval | ⚠️ через MCP | ⚠️ через MCP | ✅ Qdrant first-class |
| Graph RAG | ❌ | ❌ | ✅ Neo4j + mempalace |
| Cross-encoder rerank | ❌ | ❌ | ✅ BGE-reranker-v2-m3 |
| Hybrid BM25+vector+graph | ⚠️ базовый | ⚠️ базовый | ✅ RRF fusion |
| Compaction | ✅ ~95% auto, override env | ⚠️ hidden agent | ✅ + domain-aware |
| Pre-compaction hooks | ✅ | ✅ experimental | ✅ |
| TTL/decay | ❌ | ❌ | ✅ importance-weighted |
| Conflict resolution | ❌ | ❌ | ✅ LLM judge + newest wins |
| Consolidation cron | ❌ | ❌ | ✅ |
| Reflection loop | ❌ | ❌ | ✅ end-of-session |

## Hooks

| Возможность | Claude Code | OpenCode | Форк |
|-------------|-------------|----------|------|
| Количество событий | 12 | 30+ | 12+ кастомных |
| PreToolUse | ✅ JSON via stdin | ✅ plugin event | ✅ |
| PostToolUse | ✅ | ✅ | ✅ |
| Stop / SubagentStop | ✅ | ✅ session.compacted | ✅ |
| UserPromptSubmit | ✅ | ✅ message.updated | ✅ |
| SessionStart/End | ✅ | ✅ | ✅ |
| PreCompact | ✅ | ✅ experimental.session.compacting | ✅ |
| InstructionsLoaded | ✅ | ❌ | ✅ |
| Elicitation (MCP) | ✅ auto | ❌ | ✅ |
| Hot-reload hooks | ❌ | ⚠️ plugin restart | ✅ file watcher |
| HTTP hooks | ✅ | ❌ | ✅ |
| LLM-as-hook | ✅ (prompt hook) | ❌ | ✅ |
| Exit-code 2 = block | ✅ | ❌ (throw) | ✅ |

## Permissions

| Возможность | Claude Code | OpenCode | Форк |
|-------------|-------------|----------|------|
| Glob rules per tool | ✅ | ✅ | ✅ |
| Permission modes | 6 (default/acceptEdits/auto/dontAsk/bypass/plan) | per-tool ask/allow/deny | все 6 + кастомные |
| Background classifier | ✅ `auto` mode | ❌ | ✅ LLM-judge |
| Sandbox | ⚠️ managed | ❌ | ✅ Docker per-agent |
| Subagent restrictions | ✅ Agent(name) in deny | ✅ task permission glob | ✅ + cost ceiling |
| Audit log | ✅ via PostToolUse | ⚠️ | ✅ structured JSONL |

## Models

| Возможность | Claude Code | OpenCode | Форк |
|-------------|-------------|----------|------|
| Anthropic native | ✅ Opus 4.7, Sonnet 4.6, Haiku 4.5 | ✅ | ✅ |
| OpenAI | ⚠️ через прокси | ✅ GPT 5.2 | ✅ |
| Google Gemini | ⚠️ | ✅ 3 Pro | ✅ |
| Open-source | ⚠️ прокси | ✅ 75+ (Qwen, DeepSeek, GLM, Llama, Mistral) | ✅ + auto-fallback |
| Per-agent model | ✅ subagent frontmatter | ✅ | ✅ |
| Variants (reasoning effort) | ✅ effort: low/medium/high/xhigh | ✅ per-provider variants | ✅ |
| Small model (titles) | ❌ | ✅ small_model config | ✅ |
| Cost tracking | ❌ | ❌ | ✅ per-task |
| Local cache (vLLM) | ❌ | ❌ | ✅ |
| Prompt caching | ✅ Anthropic | ❌ | ✅ + vLLM prefix |

## Tools

| Tool | Claude Code | OpenCode | Форк |
|------|-------------|----------|------|
| Read | ✅ | ✅ | ✅ |
| Edit / Write | ✅ | ✅ | ✅ |
| Bash | ✅ PowerShell on Win | ✅ | ✅ |
| Grep / Glob | ✅ ripgrep | ✅ ripgrep | ✅ |
| WebFetch | ✅ + 15min cache | ✅ | ✅ |
| WebSearch | ✅ | ✅ Exa AI | ✅ |
| Agent / Task | ✅ (renamed in 2.1.63) | ✅ | ✅ |
| TodoWrite | ✅ | ✅ | ✅ |
| NotebookEdit | ✅ | ❌ | ⚠️ по запросу |
| Skill | ✅ | ✅ | ✅ hot-reload |
| AskUserQuestion | ✅ | ✅ question | ✅ |
| Plan mode | ✅ EnterPlanMode/Exit | ✅ | ✅ |
| LSP | ❌ (только через MCP) | ⚠️ experimental | ✅ first-class |
| apply_patch | ❌ | ✅ | ✅ |
| ScheduleWakeup | ✅ | ❌ | ⚠️ по запросу |

## Context management

| Возможность | Claude Code | OpenCode | Форк |
|-------------|-------------|----------|------|
| Auto-compact | ✅ ~95% | ✅ hidden agent | ✅ настраиваемый |
| Manual /compact | ✅ | ✅ | ✅ |
| Pre-compaction hook | ✅ | ✅ | ✅ |
| Project CLAUDE.md survives | ✅ | ✅ | ✅ |
| Tool result offload | ✅ >25k → file | ⚠️ manual | ✅ auto |
| Hierarchical summary | ❌ | ❌ | ✅ L0/L1/L2 |
| LLMLingua compression | ❌ | ❌ | ✅ |
| Attention sinks | ❌ | ❌ | ✅ StreamLLM |
| Working memory offload | ⚠️ через scratchpad | ⚠️ | ✅ explicit file |

## Production readiness

| Возможность | Claude Code | OpenCode | Форк |
|-------------|-------------|----------|------|
| Eval harness | ❌ | ❌ | ✅ |
| Observability (OTel) | ❌ | ⚠️ plugin | ✅ |
| Tracing per session | ✅ | ✅ | ✅ |
| Metrics export | ❌ | ❌ | ✅ Prometheus |
| Health checks | ❌ | ⚠️ | ✅ watchdog v2 |
| Cost analytics | ❌ | ❌ | ✅ |
| A/B model swap | ❌ | ❌ | ✅ mid-session |
| Regression detection | ❌ | ❌ | ✅ |

## UX

| Возможность | Claude Code | OpenCode | Форк |
|-------------|-------------|----------|------|
| Slash commands | ✅ | ✅ | ✅ |
| Skills | ✅ | ✅ | ✅ + hot-reload |
| RU-first | ❌ | ❌ | ✅ |
| RU commit messages | ❌ | ❌ | ✅ auto |
| Плагины | ⚠️ marketplace | ✅ npm | ✅ |
| Кастомные tools | ✅ MCP / Agent SDK | ✅ Zod in plugin | ✅ |
| GUI diff streaming | ✅ VS Code/JetBrains | ⚠️ | ✅ |
| Mobile (iOS) | ✅ | ❌ | ⚠️ web |

## Стоимость и доступ

| Возможность | Claude Code | OpenCode | Форк |
|-------------|-------------|----------|------|
| Free tier | ❌ subscription | ✅ | ✅ |
| Bring your own key | ❌ | ✅ | ✅ |
| Self-hosted | ❌ | ✅ | ✅ |
| Open source | ❌ | ✅ MIT | ✅ MIT |
| Vendor lock-in | ⚠️ Anthropic | ❌ | ❌ |
| Cloud LLM cost | mid-high | low (BYOK) | low (BYOK + local) |

---

## Итог

**Главные преимущества CC:** prompt caching, MCP Tool Search, worktree isolation, 12 hook-событий, sub-agent persistent memory.

**Главные преимущества OpenCode:** open-source, 75+ моделей, plugin SDK, multi-provider.

**Где форк обгоняет обоих:** KG-RAG, cross-encoder rerank, eval harness, RU-first, hot-reload, cost router, Docker-sandbox, full observability.
