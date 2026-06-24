# Solomon Harness — Краткое руководство пользователя

**Версия:** 1.36.0-dev (HEAD `e5c5a7e`)
**Дата:** 2026-06-24
**Аудитория:** разработчик, который хочет запустить Harness и создать своего агента за 10 минут.

---

## 1. Что это

**Solomon Harness** — open-source агентская оболочка (MIT). Локально-первая, мульти-провайдерская, с 4-слойной памятью, RBAC, Web UI и CLI. Альтернатива Claude Code / OpenCode, не зависящая от Anthropic API.

| Слой | Что внутри |
|------|------------|
| **Backend** | FastAPI + uvicorn, :8765, ~25 500 LoC |
| **Frontend** | Vite + React + TS, mounted на `/ui` |
| **CLI** | `harness` (`serve`/`agents`/`auth`/`hooks`/`plugins`/...) |
| **Memory** | L1 hmem + L2 mem0 + L3 hybrid + L4 file |
| **LLM** | Tier Router: T1 (Qwen3 8B local) → T2 (GLM-4.7) → T3 (MiniMax-M2.7) → T4 escalate |

---

## 2. Установка и первый запуск

### Требования
- Windows 11 / Linux / macOS
- Python 3.12+
- Git
- (опционально) Node.js 20+ — для пересборки Web UI

### Шаги

```bash
# 1. Клонировать репо
git clone https://github.com/mbakholdin-byte/HarnessSolomon.git
cd HarnessSolomon

# 2. Создать venv
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux/macOS

# 3. Установить зависимости
pip install -e ".[dev,memory,observability]"

# 4. (опционально) Настроить UI
cd web
npm install
npm run build
cd ..

# 5. Запустить
harness serve --host 0.0.0.0 --port 8765
```

**Открыть:**
- API health: <http://127.0.0.1:8765/api/v1/health> → должно вернуть `200 OK`
- Web UI: <http://127.0.0.1:8765/ui/> (если `npm run build` выполнен)
- Если UI показывает белый экран → **Ctrl+Shift+R** (Chrome кэш со старым bundle hash)

### Проверка

```bash
curl -s http://127.0.0.1:8765/api/v1/health
# {"status":"ok","version":"1.32.0",...}

harness --help
# serve, agents, elicitation, context, auth, reload, sessions, hooks, observability, plugins
```

---

## 3. Подключение модели (API ключ)

Harness использует **LiteLLM** под капотом, поэтому принимает ключи любого провайдера, который поддерживает LiteLLM.

### 3.1. Способ 1 — файл `.env` в корне репо

Создайте `C:\MyAI\06_Harness\.env` (или `~/.harness/.env`) со следующим содержимым:

```ini
# === MiniMax (Anthropic-compatible) ===
MINIMAX_API_KEY=sk-ant-ваш-ключ

# === ZhipuAI (GLM) ===
ZHIPUAI_API_KEY=ваш-ключ

# === Moonshot (Kimi) ===
MOONSHOT_API_KEY=sk-ваш-ключ

# === OpenAI (опционально) ===
OPENAI_API_KEY=sk-ваш-ключ

# === Anthropic (опционально) ===
ANTHROPIC_API_KEY=sk-ant-ваш-ключ
```

### 3.2. Способ 2 — переменные окружения

```powershell
# PowerShell
$env:MINIMAX_API_KEY = "sk-ant-ваш-ключ"

# bash
export MINIMAX_API_KEY="sk-ant-ваш-ключ"
```

### 3.3. Каталог моделей

Файл `harness/server/llm/router.py` содержит каталог `MODELS` — список всех моделей, которые Harness знает. Каждая модель имеет поля:

| Поле | Назначение |
|------|------------|
| `id` | LiteLLM-совместимый id (например, `minimax/MiniMax-M2.7`) |
| `display_name` | Имя для UI |
| `max_tokens` | Максимальная длина контекста |
| `max_tools` | Лимит tools per request (default 4) |
| `cost_in_per_1k` | Стоимость входа USD / 1K токенов |
| `cost_out_per_1k` | Стоимость выхода USD / 1K токенов |

**Где взять ключи:**

| Провайдер | Где | Endpoint |
|-----------|-----|----------|
| MiniMax | <https://console.minimax.io/> | `https://api.minimax.io` |
| ZhipuAI (GLM) | <https://bigmodel.cn/> | `https://open.bigmodel.cn/api/paas/v4` |
| Moonshot (Kimi) | <https://platform.moonshot.cn/> | `https://api.moonshot.cn/v1` |
| OpenAI | <https://platform.openai.com/> | `https://api.openai.com/v1` |
| Anthropic | <https://console.anthropic.com/> | `https://api.anthropic.com` |

### 3.4. Проверка подключения

```bash
curl -s http://127.0.0.1:8765/api/v1/models
# {"models":[{"id":"minimax/MiniMax-M2.7","display_name":"MiniMax M2.7",...}, ...]}
```

Если модель отсутствует — добавьте в `MODELS` каталог и перезапустите сервер.

---

## 4. Создание агента

В Harness есть **два уровня** агентов:

1. **Built-in агенты** (4 шт.) — готовые, можно использовать сразу.
2. **Custom агенты** — вы создаёте `.md` файл с описанием своего.

### 4.1. Built-in агенты

Посмотреть список:

```bash
harness agents list
```

```
Available sub-agents (project root: C:/MyAI):
  - code
  - explore
  - plan
  - review

  code      model=MiniMax-M2.7  perms=full         max_iter= 8  tools=[read_file, write_file, edit_file, bash, grep, glob]
  explore   model=MiniMax-M2.7  perms=read-only    max_iter= 8  tools=[read_file, grep, glob]
  plan      model=MiniMax-M2.7  perms=read-only    max_iter=10  tools=[read_file, grep, glob]
  review    model=MiniMax-M2.7  perms=read-only    max_iter= 8  tools=[read_file, grep, glob]
```

| Агент | Назначение | Permissions |
|-------|-----------|-------------|
| **explore** | Разведка кода: grep, glob, read_file. Только чтение. | read-only |
| **plan** | Планирование изменений перед кодом. | read-only |
| **code** | Пишет код в git worktree (изолированная ветка). | full |
| **review** | Code review — находит проблемы, не правит. | read-only |

### 4.2. Запуск встроенного агента через CLI

```bash
# Спросить explore агента о структуре проекта
harness agents run explore \
  --no-worktree \
  --repo "C:/MyAI/06_Harness" \
  "List the top 5 largest Python files in this repo with their line counts."
```

**Опции CLI:**

| Флаг | Что делает |
|------|------------|
| `--no-worktree` | Не создавать git worktree (быстрее, но менее безопасно) |
| `--repo PATH` | Путь к репозиторию (по умолчанию `settings.project_root`) |
| `--background` | Поставить в очередь, вернуть `job_id` сразу |
| `--cascade` | Использовать Tier Router (T1 → T2 → T3) |
| `--pr` | Открыть draft PR (требует `--background`) |

### 4.3. Создание custom агента

Создайте файл `C:/MyAI/06_Harness/.harness/agents/my-helper.md`:

```markdown
---
name: my-helper
model: MiniMax-M2.7
tools: [read_file, grep, glob]
permissions: read-only
max_iterations: 8
worktree_required: false
allowed_paths: []
---

You are the **my-helper** sub-agent of Solomon Harness.

Your job: answer questions about the `harness/` directory specifically.
You are a documentation assistant — never modify files.

## Operating rules

1. Always run `glob` first to map relevant files.
2. Use `read_file` on specific files only.
3. Report findings as bullets: `path:line — one-line justification`.
4. If ambiguous, list candidate interpretations first.

## Output format

- A short preamble (1–2 sentences framing the answer).
- Bulleted findings with file paths and line numbers.
```

**Поля frontmatter:**

| Поле | Обязательно | Описание |
|------|-------------|----------|
| `name` | да | Уникальное имя агента |
| `model` | да | ID модели из каталога |
| `tools` | да | Список tools: `read_file`, `grep`, `glob`, `write_file`, `edit_file`, `bash` |
| `permissions` | да | `read-only` или `full` |
| `max_iterations` | нет | Максимум шагов (default 8) |
| `worktree_required` | нет | Создавать ли git worktree (default `true`) |
| `allowed_paths` | нет | Glob-паттерны путей, к которым разрешён доступ |

После создания файла:

```bash
harness reload                       # форсировать hot-reload
harness agents list                  # увидеть "my-helper" в списке
harness agents run my-helper \
  --no-worktree \
  "List all Python files in harness/server/routes/"
```

### 4.4. Запуск через REST API

```bash
# 1. Создать auth token (один раз)
TOKEN=$(harness auth create --label "my-token" \
  --scopes "agents.read,agents.write,sessions.read,sessions.write" \
  | grep -oE "^token=[A-Za-z0-9_-]+" | cut -d= -f2)

# 2. Создать job (синхронно)
curl -X POST http://127.0.0.1:8765/api/v1/agents/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "agent_name": "explore",
    "prompt": "List the directory structure of harness/server/",
    "repo": "C:/MyAI/06_Harness"
  }'

# 3. Получить результат
curl -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8765/api/v1/agents/jobs/<job_id>"
```

---

## 5. Auth (scope-gated API)

Harness защищает `/api/v1/*` через **Bearer tokens с scopes**. Без токена — 401. С токеном без нужного scope — 403.

### 5.1. Список scopes (16 штук)

```
agents.read, agents.write, agents.pr,
memory.read, memory.write,
sessions.read, sessions.write,
observability.read,
elicitation.read, elicitation.write,
webhooks.admin,
privacy.read, privacy.write,
hooks.admin,
plugins.admin, plugins.read
```

### 5.2. Создание токена

```bash
# Минимальный: только чтение агентов
harness auth create --label "reader" --scopes "agents.read"

# Полный: все scopes
harness auth create --label "admin" \
  --scopes "agents.read,agents.write,agents.pr,memory.read,memory.write,sessions.read,sessions.write,observability.read,elicitation.read,elicitation.write,webhooks.admin,privacy.read,privacy.write,hooks.admin,plugins.admin,plugins.read"

# Wildcard больше НЕ работает (после v1.32.0)
harness auth create --scopes "*"
# error: unknown scope: '*'
```

**⚠️ Токен показывается только ОДИН раз.** Сохраните его в переменную окружения или в password manager.

### 5.3. Управление токенами

```bash
harness auth list                  # список активных токенов
harness auth whoami --token <TOKEN>  # информация о токене
harness auth revoke --label "reader"  # отозвать по label
harness auth revoke --hash "abc123..."  # отозвать по hash
```

---

## 6. Память (4 слоя)

Harness имеет **4 слоя памяти**, доступных через `/api/v1/memory/*` (требует scope `memory.read` / `memory.write`):

| Слой | Бэкенд | Что хранит | Когда использовать |
|------|--------|-----------|-------------------|
| **L1 hmem** | Hierarchical Markdown | Структурированные факты (P/L/T/E/D/M-префиксы) | Долгосрочная "память агента" |
| **L2 mem0** | Qdrant + embeddings | Семантические факты | Похожие запросы, fuzzy search |
| **L3 hybrid** | SQLite + OpenSearch | Эпизоды сессий | История разговоров |
| **L4 file** | Markdown в `data/sessions/` | Per-session логи | Аудит, отладка |

### 6.1. Запись и чтение

```bash
TOKEN=ваш-токен-с-memory.write

# Записать факт в L1 hmem (structured)
curl -X POST http://127.0.0.1:8765/api/v1/memory/l1 \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"id":"L001","title":"My first lesson","content":"Always read before writing.","tags":["#lesson"]}'

# Семантический поиск по L2 mem0
curl -X POST http://127.0.0.1:8765/api/v1/memory/search \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"query":"how to debug Python imports","k":5}'
```

---

## 7. Tier Router — выбор модели

Tier Router автоматически выбирает модель по сложности задачи. Это снижает cost на ~58% (см. calibration report v1.33).

| Tier | Модель | Когда |
|------|--------|-------|
| **T1** | Qwen3 8B (local, бесплатно) | Простые задачи, короткий prompt, малый контекст |
| **T2** | GLM-4.7 (cloud, mid-tier) | Средняя сложность |
| **T3** | MiniMax-M2.7 (cloud, premium) | Сложный reasoning, длинный контекст |
| **T4** | Escalate (fallback) | Если T1-T3 не сработали |

Калибровка v1.34 (текущая):
- T1: prompt ≤ 1000 chars И context ≤ 2000 tokens
- T3: prompt ≥ 10000 chars ИЛИ context ≥ 16000 tokens ИЛИ keywords `["reasoning", "analyze", "prove", "derive", "evaluate"]`
- Остальное → T2

Параметры можно переопределить в `harness/config.py` (`tier_routing_*`).

---

## 8. Troubleshooting

### 8.1. Сервер не стартует

```
[WinError 10013] WSAEACCES — An attempt was made to access a socket in a way forbidden by its access permissions
```

→ Порт занят `hns` (Host Network Service) или зарезервирован Windows. Используйте другой:

```bash
harness serve --port 8765   # стандарт
harness serve --port 9000   # альтернатива
```

### 8.2. UI белый экран

→ Chrome кэшировал старый bundle hash. **Ctrl+Shift+R** (hard reload).

### 8.3. `mcp__mem0__*` падает с WinError 10061

→ В вашем окружении есть **две** системы памяти:
- `mcp__mem0__*` — shared (порт 7333, зона Алекса)
- `mcp__solomon-mem0__*` — изолированная (порт 17333, ваша зона)

Используйте префикс `mcp__solomon-mem0__*` или поднимите Qdrant на 17333.

### 8.4. `unified_memory disabled (init failed)`

→ **Исправлено в e5c5a7e.** Если видите после обновления — проверьте `harness/server/app.py:180`, что вызов `UnifiedMemory(hmem_dir=..., mem0_dir=..., hybrid_dir=..., file_dir=...)` без `settings=` kwarg.

### 8.5. `Payload validation failed for event SubagentStart/Stop`

→ **Исправлено в e5c5a7e.** Hook payload теперь содержит обязательные поля `prompt` (Start) и `result` + `duration_ms` (Stop).

### 8.6. Marketplace возвращает 0 плагинов

→ Скорее всего ваш токен не имеет scope `plugins.read`. Проверьте:

```bash
harness auth whoami --token "$TOKEN"
```

Если `plugins.read` отсутствует — создайте новый токен:

```bash
harness auth create --label "marketplace-user" --scopes "plugins.read"
```

### 8.7. Агент завершился со статусом `failed`

→ Не паника. Статус `failed` часто = **adversarial verify** отклонил результат (Phase 2). Проверьте:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  "http://127.0.0.1:8765/api/v1/agents/jobs/<job_id>" | jq
```

Если `error: "..."` содержит `verify_rejected` — это нормально для smoke-тестов. Для production-задач используйте более конкретный prompt.

---

## 9. Что дальше

- **Читать:** `docs/architecture.md`, `docs/PHASE-0-SPEC.md`
- **Production deploy:** `docs/deployment.md` (Docker + systemd)
- **Создать свой plugin:** `docs/plugins.md` (Phase 6.2+)
- **GitHub:** <https://github.com/mbakholdin-byte/HarnessSolomon>

---

**Связанные артефакты:**
- `C:\MyAI\_output\2026-06\23.06 Harness-Bring-Up\bring-up-report.md` — bring-up session
- `C:\MyAI\_output\2026-06\24.06 Harness-Bugfix-UserGuide\fixes-applied.md` — список фиксов 24.06
