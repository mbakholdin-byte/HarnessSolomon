# Quickstart — Solomon Harness

**Версия:** Phase 0 MVP
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

### 1.3. Запустить backend

```bash
python -m harness
# Uvicorn running on http://0.0.0.0:8765
```

> ⚠️ Порт 8765 (не 8000!) — на Windows 11 + Docker Desktop порт 8000 зарезервирован hns (WSAEACCES). Подробности: `_output/2026-06/14.06 Port-Allocation-and-Services/ports-map.md`

### 1.4. Проверить

```bash
curl http://localhost:8765/api/health
# {"status":"ok","version":"0.1.0","project_root":"..."}

curl http://localhost:8765/api/models
# [{"id":"MiniMax-M2.7", ...}, {"id":"glm-4.7", ...}, {"id":"moonshot-v1-128k", ...}]
```

## 2. Frontend

### 2.1. Установить зависимости

```bash
cd harness/web
npm install
```

### 2.2. Запустить dev server

```bash
npm run dev
# Vite ready in 200ms
# ➜  Local: http://localhost:5173/
```

> ⚠️ Vite 5 на Node 18+ дефолтно слушает на `[::1]:5173` (IPv6 localhost). Используй `http://localhost:5173`, не `http://127.0.0.1:5173` (не работает).

### 2.3. Открыть в браузере

Перейди на http://localhost:5173 — увидишь chat UI с 2 колонками (список сессий слева, чат справа).

## 3. Первый чат

1. Нажми `+ New chat` — создастся новая сессия
2. Выбери модель (MiniMax-M2.7 / glm-4.7 / moonshot-v1-128k) в правом верхнем углу
3. Напиши "Привет" в InputBar
4. Нажми Send или Enter
5. Через ~5-30 секунд получишь ответ через WebSocket streaming

## 4. Smoke tests

```bash
# Все тесты (mock-only, без API ключа)
pytest tests/ -q
# 62 passed
```

> Step 8 (e2e smoke tests `test_smoke.py`) идёт параллельно и в текущем срезе ещё не слит. После его завершения будет `67 passed` (62 unit + 5 e2e).

## 5. Real LLM tests

```bash
export MINIMAX_API_KEY="sk-..."
pytest tests/test_smoke.py -v -m real_llm
# 5 passed (5 mock skipped)
```

Маркер `real_llm` пропускается автоматически, если ни один из ключей (`MINIMAX_API_KEY` / `ZHIPUAI_API_KEY` / `MOONSHOT_API_KEY`) не выставлен.

## 6. Troubleshooting

| Проблема | Решение |
|----------|---------|
| `WinError 10013 WSAEACCES on port 8000` | Используй порт 8765 (default в Phase 0) |
| `Vite: 127.0.0.1:5173 не работает` | Используй `localhost:5173` (IPv6) |
| `litellm.BadRequestError: LLM Provider NOT provided` | Используй формат `minimax/MiniMax-M2.7` (с префиксом провайдера) |
| `Backend тесты падают на async` | `pip install pytest-asyncio` |
| `WebSocket не подключается` | Проверь что Vite proxy `ws: true` в `harness/web/vite.config.ts` |
| `ToolRuntime отказывает в bash` | Это safety — проверь deny patterns в `harness/server/agent/safety.py` |

## 7. Дальше

- **Фаза 1** (4-слойная память): `docs/roadmap.md`
- **Фаза 2** (sub-agents): `docs/roadmap.md`
- **Фаза 3** (context compaction): `docs/roadmap.md`
- **Архитектура Phase 0**: `docs/architecture.md`
- **История изменений**: `docs/CHANGELOG.md`
