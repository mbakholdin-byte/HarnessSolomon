# Solomon Harness

**Open-source агентская оболочка поверх open-source LLM (Qwen, DeepSeek, GLM, Llama).**

Сильнее Claude Code и OpenCode за счёт:
- **4-слойной памяти** (working/session/long-term/episodic+semantic) с dual-write
- **KG-RAG** через Neo4j (графовая память, multi-hop reasoning)
- **Cross-encoder rerank** (BGE-reranker-v2-m3)
- **Eval harness** baked-in (SWE-bench-style)
- **RU-first** UX
- **Hot-reload** skills, hooks и privacy zones через file watcher
- **Cost-aware routing** (Haiku-class → локальные, Opus-class → cloud)
- **Docker-sandbox** per agent type с seccomp
- **Production observability** — JSONL logs, Prometheus metrics, OTel traces, per-task cost
- **Hooks framework** — 16 событий, 4 транспорта, 12 builtin хуков, hot-reload
- **Scope-gated API** — 10 RBAC scopes, Bearer token auth, capabilities discovery

## Быстрый старт

```bash
# 1. Установить
git clone https://github.com/mbakholdin-byte/HarnessSolomon.git
cd HarnessSolomon
python -m pip install -e .

# 2. Минимум один API ключ
export MINIMAX_API_KEY="sk-..."
# или ZHIPUAI_API_KEY / MOONSHOT_API_KEY

# 3. Запустить backend (порт 8765, не 8000!)
python -m harness
# → Uvicorn running on http://0.0.0.0:8765

# 4. Проверить
curl http://localhost:8765/api/health
# {"status":"ok","version":"1.21.0",...}

# 5. (Опционально) Frontend
cd harness/web && npm install && npm run dev
# → http://localhost:5173
```

Подробности: [`docs/quickstart.md`](docs/quickstart.md) (<10 минут до первого ответа).

## Документация

### Основная
- [`docs/quickstart.md`](docs/quickstart.md) — быстрый старт (<10 мин)
- [`docs/architecture.md`](docs/architecture.md) — high-level архитектура и слои
- [`docs/migration.md`](docs/migration.md) — гайд миграции v0.x → v1.0
- [`docs/CHANGELOG.md`](docs/CHANGELOG.md) — история изменений

### Подсистемы
- [`docs/hooks.md`](docs/hooks.md) — **Hooks framework** (16 events, 4 transports, 12 builtin, hot-reload, rate limit + circuit breaker)
- [`docs/observability.md`](docs/observability.md) — **Observability** (JSONL, Prometheus, OTel, health, cost, admin endpoints)
- [`docs/api.md`](docs/api.md) — **REST/WS API reference** (`/api/v1/*` endpoints, scopes, RBAC)
- [`docs/scope-api.md`](docs/scope-api.md) — **Scope-gated API** (10 RBAC scopes, tokens, capabilities)
- [`docs/elicitation.md`](docs/elicitation.md) — **Elicitation** (3 транспорта: WebSocket / long-poll / SSE)
- [`docs/webhooks.md`](docs/webhooks.md) — **Outbound webhooks** (auto-disable, DLQ, secret rotation)
- [`docs/cli.md`](docs/cli.md) — **CLI reference** (`harness serve`, `hooks`, `observability`, `auth`, `webhooks dlq`, …)

### Каталоги
- [`docs/MODEL_REGISTRY.md`](docs/MODEL_REGISTRY.md) — каталог моделей (T1/T2/T3)
- [`docs/MODEL_SUPPORT.md`](docs/MODEL_SUPPORT.md) — статус поддержки провайдеров
- [`docs/roadmap.md`](docs/roadmap.md) — дорожная карта

## Статус

**Текущая версия:** v1.21.0 (Phase 4.11)
**Ближайший релиз:** v1.0.0-rc1 (Phase 4.14 final closeout)

### Завершённые фазы

| Фаза | Версия | Дата | Описание |
|------|--------|------|----------|
| Phase 0 — Web MVP | v0.1.0 | 14.06.2026 | FastAPI + LiteLLM, 6 tools, WebSocket chat, React UI |
| Phase 1 — Memory | v1.0.0–v1.5.0 | 15.06.2026 | 4-слойная память, dual-write, privacy zones, scope-gated API |
| Phase 2 — Orchestration | v1.x | 15.06.2026 | Sub-agents, MergeQueue, PR integration, stacked PRs |
| Phase 3 — Context Engineering | v1.0.0–v1.5.0 | 15.06.2026 | Compaction, scratchpad, reflection, offloader |
| Phase 4.0 — Hooks framework | v1.6.0 | 16.06.2026 | 14 events, 4 transports, 5 builtin hooks |
| Phase 4.1 — Observability | v1.7.0–v1.7.2 | 16.06.2026 | JSONL, Prometheus, OTel, health, cost, API versioning |
| Phase 4.2 — Hot-reload | v1.8.0–v1.9.0 | 16.06.2026 | FileWatcher, agents/hooks/privacy hot-reload |
| Phase 4.3 — Elicitation + Notification | v1.10.0–v1.12.0 | 16.06.2026 | 2 new events, WebSocket transport, webhook+desktop fanout |
| Phase 4.4 — CLI inspection | v1.13.0 | 17.06.2026 | `harness hooks`, `harness observability` CLI |
| Phase 4.4+ — Hook wiring | v1.14.0 | 17.06.2026 | 11 production trigger points wired |
| Phase 4.5 — PermissionRequest | v1.15.0 | 17.06.2026 | Hook-driven permission override, long-poll elicitation |
| Phase 4.6 — Audit + schemas + Slack/Teams | v1.16.0 | 17.06.2026 | NDJSON audit CLI, Pydantic schemas, Slack+Teams notify |
| Phase 4.7 — Permission wiring + tail + diff | v1.17.0 | 17.06.2026 | 5 file tools gated, live tail, stats diff |
| Phase 4.8 — Elicitation history + retry/DLQ + circuit breaker | v1.18.0 | 17.06.2026 | Decision store, notify retry+DLQ, rate limiter |
| Phase 4.9 — Per-tool + per-model metrics + deep probes | v1.19.0 | 18.06.2026 | Latency histograms, cost breakdown, 8 deep probes |
| Phase 4.10 — Hook pattern library | v1.20.0 | 18.06.2026 | 8 production-ready hook JSON specs |
| Phase 4.11 — SSE Elicitation + admin endpoints | v1.21.0 | 18.06.2026 | SSE transport, observability admin, 2 new scopes |
| Phase 4.12 — Permission + 410 Gone + Follower | v1.22.0 | 19.06.2026 | Scratchpad perms, legacy 410, --follow improvements |
| Phase 4.13 — Webhook hardening | v1.23.0 | 19.06.2026 | Auto-disable, DLQ replay, secret rotation |

### Тестовое покрытие

- **2474+ tests** passing (Phase 4.12 v1.22.0)
- 0 регрессий
- Trust boundary AST-enforced на всех observability/hooks модулях

## Стек

- **Backend:** Python 3.12+, FastAPI, uvicorn, LiteLLM, aiosqlite, Pydantic v2
- **Frontend:** Vite, React 18, TypeScript, react-markdown
- **LLM:** MiniMax-M2.7, GLM-4.7, Moonshot-v1-128k (cloud); Qwen3 8B/30B (local, Phase 0.5)
- **Хранилища:** SQLite (WAL mode), Qdrant (embeddings), Neo4j (KG-RAG)
- **Observability:** JSONL logs, Prometheus (`/metrics`), OpenTelemetry (OTLP export)

## Лицензия

MIT
