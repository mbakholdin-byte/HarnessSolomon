# Quickstart — Solomon Harness

**Версия:** v1.22.0+ (Phase 4.12)
**Время до первого ответа:** <10 минут

## Требования

- Python 3.12+
- Node.js 18+ (для frontend)
- Один из API ключей: `MINIMAX_API_KEY` / `ZHIPUAI_API_KEY` / `MOONSHOT_API_KEY`

## 1. Backend

### 1.1. Клонировать и установить

```bash
git clone https://github.com/mbakholdin-byte/HarnessSolomon.git
cd HarnessSolomon
python -m pip install -e .

# Опционально: dev / observability / memory extras
pip install -e ".[observability]"   # prometheus-client + opentelemetry
pip install pytest pytest-asyncio websockets httpx
```

### 1.2. Настроить API ключ

```bash
# Минимум один из:
export MINIMAX_API_KEY="sk-..."
# или
export ZHIPUAI_API_KEY="..."
# или
export MOONSHOT_API_KEY="..."
```

> В PowerShell: `$env:MINIMAX_API_KEY = "sk-..."`

### 1.3. (Опционально) Включить observability

```bash
# Prometheus metrics на /metrics (default: off)
export OBSERVABILITY_PROMETHEUS_ENABLED=true
pip install prometheus-client

# OpenTelemetry traces (export в Jaeger/Tempo, default: off)
export OBSERVABILITY_OTLP_ENABLED=true
export OBSERVABILITY_OTLP_ENDPOINT=http://localhost:4317
pip install opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp
```

JSONL logs и cost tracking включены по умолчанию (`observability_jsonl_enabled=true`, `observability_cost_enabled=true`).

### 1.4. Запустить backend

```bash
python -m harness
# → [harness] Uvicorn running on http://0.0.0.0:8765
# → [harness] token_store: .../harness-scope.db (auth_required=True)
# → [harness] hook_runner: enabled (registry_size=12)
# → [harness] hot_reload: enabled (debounce=200ms)
```

> ⚠️ Порт 8765 (не 8000!) — на Windows 11 + Docker Desktop порт 8000 зарезервирован hns (WSAEACCES). Подробности: `_output/2026-06/14.06 Port-Allocation-and-Services/ports-map.md`

### 1.5. Проверить

```bash
curl http://localhost:8765/api/health
# {"status":"ok","version":"1.22.0","project_root":"..."}

curl http://localhost:8765/api/models
# [{"id":"MiniMax-M2.7", ...}, {"id":"glm-4.7", ...}, {"id":"moonshot-v1-128k", ...}]

# Health probes (Phase 4.1)
curl http://localhost:8765/health/live
curl http://localhost:8765/health/ready
curl http://localhost:8765/health/deep   # 8 subsystem probes (Phase 4.9)
```

## 2. Auth (Phase 1.6)

По умолчанию `/api/v1/*` endpoints требуют Bearer token. Первый read-only CLI-вызов создаёт bootstrap-admin токен автоматически.

```bash
harness auth list
# [harness] bootstrap-admin token created (label=bootstrap-admin).
# [harness] SAVE THIS — it will not be shown again:
#   YsVQ3gfLHK_GYoe8kUvKVZh4B2GcUFtcxvwkN0OM9JM

# Проверить токен
harness auth test YsVQ3gfLHK_GYoe8kUvKVZh4B2GcUFtcxvwkN0OM9JM
# ok: http://127.0.0.1:8765/api/v1/capabilities -> 200

# Создать scoped token (минимум нужных scopes)
harness auth create --label "opencode-mcp" --scopes "agents.read,memory.read,sessions.read"
```

Open dev mode (без auth): `AUTH_REQUIRED=false harness serve`.

См. [`docs/scope-api.md`](scope-api.md) — полный список 10 RBAC scopes.

## 3. Frontend

### 3.1. Установить зависимости

```bash
cd harness/web
npm install
```

### 3.2. Запустить dev server

```bash
npm run dev
# Vite ready in 200ms
# ➜  Local: http://localhost:5173/
```

> ⚠️ Vite 5 на Node 18+ дефолтно слушает на `[::1]:5173` (IPv6 localhost). Используй `http://localhost:5173`, не `http://127.0.0.1:5173` (не работает).

### 3.3. Открыть в браузере

Перейди на http://localhost:5173 — увидишь chat UI с 2 колонками (список сессий слева, чат справа).

## 4. Первый чат

1. Нажми `+ New chat` — создастся новая сессия
2. Выбери модель (MiniMax-M2.7 / glm-4.7 / moonshot-v1-128k) в правом верхнем углу
3. Напиши "Привет" в InputBar
4. Нажми Send или Enter
5. Через ~5-30 секунд получишь ответ через WebSocket streaming

## 5. Smoke tests

```bash
# Все тесты (mock-only, без API ключа)
pytest tests/ -q
# 2474+ passed

# e2e smoke (требует реальный LLM API ключ)
pytest tests/test_smoke.py -v -m real_llm
```

Маркер `real_llm` пропускается автоматически, если ни один из ключей не выставлен.

## 6. Observability (опционально)

```bash
# JSONL logs (local read, no server required)
harness observability log --tail 20

# Prometheus /metrics scrape (server должен быть запущен)
harness observability metrics --filter "harness_llm"
curl http://localhost:8765/metrics

# Live tail of in-process counters (no HTTP)
harness observability metrics --follow

# Health probe
harness observability health --level deep

# Diff двух JSON snapshots (для regression testing)
harness observability stats --json > before.json
# ... запуск тестов ...
harness observability stats --json > after.json
harness observability stats --diff before.json after.json
```

См. [`docs/observability.md`](observability.md).

## 7. Hooks (опционально)

```bash
# Список builtin + project hooks (no server)
harness hooks list

# Hot-reload status
harness hooks status

# Fire hook event для теста
harness hooks dispatch PreToolUse --payload '{"tool_name":"bash","arguments":{"command":"ls"}}'

# Live tail audit log
harness hooks audit --follow --filter "PreToolUse"
```

См. [`docs/hooks.md`](hooks.md).

## 8. Troubleshooting

| Проблема | Решение |
|----------|---------|
| `WinError 10013 WSAEACCES on port 8000` | Используй порт 8765 (default в Phase 0) |
| `Vite: 127.0.0.1:5173 не работает` | Используй `localhost:5173` (IPv6) |
| `litellm.BadRequestError: LLM Provider NOT provided` | Используй формат `minimax/MiniMax-M2.7` (с префиксом провайдера) |
| `Backend тесты падают на async` | `pip install pytest-asyncio` |
| `WebSocket не подключается` | Проверь что Vite proxy `ws: true` в `harness/web/vite.config.ts` |
| `ToolRuntime отказывает в bash` | Это safety — проверь deny patterns в `harness/server/agent/safety.py` и PermissionRequest hook |
| `401 missing Authorization header` на `/api/v1/*` | `AUTH_REQUIRED=false` для dev mode, либо создай токен через `harness auth list` |
| `403 missing required scope: X` | Mint token с нужным scope: `harness auth create --scopes "...,X"` |

## 9. Дальше

- **Архитектура:** [`docs/architecture.md`](architecture.md)
- **API reference:** [`docs/api.md`](api.md)
- **Migration guide v0.x → v1.0:** [`docs/migration.md`](migration.md)
- **Hooks:** [`docs/hooks.md`](hooks.md)
- **Observability:** [`docs/observability.md`](observability.md)
- **Elicitation (3 транспорта):** [`docs/elicitation.md`](elicitation.md)
- **Webhooks (outbound hardening):** [`docs/webhooks.md`](webhooks.md)
- **CLI reference:** [`docs/cli.md`](cli.md)
- **История изменений:** [`docs/CHANGELOG.md`](CHANGELOG.md)
