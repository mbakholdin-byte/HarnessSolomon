# AGENTS.md — Правила для агентов в репо 06_Harness

## Контекст

Это репозиторий **Solomon Harness** — open-source агентской оболочки. Исходное исследование: `C:\MyAI\_output\2026-06\12.06 Harness-Claude-Code-Architecture\`.

## Принципы разработки

1. **Open-source first** — MIT, не зависеть от Anthropic/OpenAI API
2. **Local-first** — работает на Qwen3-8B без облака
3. **Composability** — модули независимы
4. **Dual-write** — изменения в памяти дублируются в файлы
5. **Observability by default** — каждое действие логируется
6. **Cost transparency** — пользователь видит стоимость каждой задачи

## Code style

- Python 3.12+, type hints везде
- `async/await` для I/O
- Pydantic v2 для всех схем
- `from __future__ import annotations`
- Black + Ruff

## Структура

```
harness/
├── cli.py                # Entry point
├── agent.py              # Main agent loop
├── tools/                # Базовые tools
├── providers/            # Multi-provider
├── memory/               # 4-слойная память (Фаза 1)
├── agents/               # Sub-agents (Фаза 2)
├── context/              # Compaction (Фаза 3)
├── hooks/                # 12 hook events (Фаза 4)
├── observability/        # Tracing, metrics (Фаза 4)
├── eval/                 # Eval harness (Фаза 5)
└── sandbox/              # Docker (Фаза 5)
```

## Запрещено

- Использовать ORM (чистый SQL через asyncpg/sqlite3)
- Хардкодить API-ключи (только env)
- Блокирующие вызовы в main loop
- Писать в `private/*` без явного разрешения

## Память

- Существующая инфраструктура Соломона: hmem, mem0, mempalace, fas-hybrid-memory
- Не дублировать, а **использовать через MCP-адаптеры**

## Roadmap

См. `docs/roadmap.md`. Текущая фаза: **0 — MVP**.
