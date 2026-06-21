# Docusaurus Skeleton for Harness v1.40+

**Source:** `C:\MyAI\_Solomon\.claude\templates\docs-site\`
**Target:** `06_Harness\docs-site\`
**Created:** 2026-06-21 by Solomon (Phase 8.0 acceleration)
**Docusaurus version:** 3.0+

## Что включено

- ✅ Docusaurus v3 config (`docusaurus.config.ts`) с i18n (en/ru), algolia placeholder
- ✅ Landing page (`src/pages/index.tsx`) — hero + features + comparison table
- ✅ Solomon theme (`src/css/custom.css`) — blue palette + dark mode
- ✅ Sidebar config (`sidebars.ts`) — 7 категорий, ~30 страниц
- ✅ Intro page (`docs/intro.md`)
- ✅ Tutorial 1 — Quickstart (`docs/tutorials/quickstart.md`)
- ✅ CI workflow (`.github/workflows/docs.yml`) — build + deploy на GitHub Pages
- ✅ docusaurus-plugin-openapi-docs в config (для API reference Wave 3)

## Что НЕ включено (Prog дополнит)

- ⏳ Tutorial 2 (first-agent) — Wave 1
- ⏳ Tutorial 3 (plugin-development) — Wave 1
- ⏳ Configuration reference (`docs/configuration/reference.md`) — Wave 2
- ⏳ Configuration profiles (development, production, cost-optimized) — Wave 2
- ⏳ API reference pages (auto-gen from OpenAPI) — Wave 3
- ⏳ Installation guides (Linux, macOS, Windows, Docker, k8s) — Wave 4
- ⏳ Migration guides (v1.0-to-v1.40, v1.32-to-v1.40, breaking-changes) — Wave 4
- ⏳ Troubleshooting pages — Wave 4
- ⏳ Press kit (`static/press/`) — Wave 5
- ⏳ Logo SVG/PNG (placeholder) — Wave 5 (Марк даст финальный)
- ⏳ `scripts/gen-config-ref.py` — Wave 2
- ⏳ `scripts/gen-changelog.py` — Wave 5
- ⏳ Logo + favicon (нужны от Марка) — Wave 5

## Использование

### Для Prog (копирование в 06_Harness)

```bash
# Из корня 06_Harness/
cp -r .claude-templates-reference/docs-site ./docs-site
# (этот шаблон живёт в _Solomon/.claude/templates/docs-site/)

cd docs-site
npm install
npm run start
# Откроется http://localhost:3000
```

### Проверка качества

```bash
# Type check
npm run typecheck

# Build (для проверки ошибок)
npm run build

# Lighthouse (после deploy)
# Performance ≥90, A11y ≥95, SEO ≥95 — DoD Phase 8.0
```

## Структура

```
docs-site/
├── docusaurus.config.ts          # Main config
├── package.json                  # npm scripts + deps
├── sidebars.ts                   # Sidebar config
├── tsconfig.json                 # TypeScript config
├── README.md                     # Этот файл
├── .github/
│   └── workflows/
│       └── docs.yml              # CI: build + deploy
├── src/
│   ├── pages/
│   │   ├── index.tsx             # Landing page
│   │   └── index.module.css
│   ├── components/               # (empty, Prog дополнит)
│   └── css/
│       └── custom.css            # Solomon theme
├── docs/
│   ├── intro.md                  # Welcome page
│   └── tutorials/
│       └── quickstart.md         # Tutorial 1
└── static/                       # (assets: logo, favicon, press kit)
```

## Скрипты

| Скрипт | Что делает |
|--------|-----------|
| `npm run start` | Dev server на http://localhost:3000 |
| `npm run build` | Production build в `build/` |
| `npm run serve` | Serve production build локально |
| `npm run typecheck` | TypeScript type check |
| `npm run clear` | Очистить кэш Docusaurus |
| `npm run deploy` | Deploy на GitHub Pages (если настроен) |
| `npm run gen:config-ref` | Генерирует configuration reference (Wave 2) |
| `npm run gen:changelog` | Генерирует CHANGELOG (Wave 5) |

## Связано

- **Phase 8.0 handoff:** `C:\MyAI\_infra\orchestrator-inbox\from-solomon\2026-06-21-phase-80-public-docs-handoff.md`
- **Phase 8.0 planning:** `C:\MyAI\_output\2026-06\21.06 Phase-8-Planning\phase-8-options.md`
- **Phase 8.0 handoff doдкумент** от Solomon: 4-6 недель, target v1.40.0, deadline 12.07

## ТЗ для Prog (что делать дальше)

1. Скопировать `docs-site/` в `06_Harness/`
2. `npm install` для установки зависимостей
3. Проверить что `npm run start` показывает landing page
4. Написать Tutorial 2 (first-agent) — 15 мин walkthrough
5. Написать Tutorial 3 (plugin-development) — 30 мин walkthrough
6. Создать placeholder pages для остальных категорий (sidebar ссылается)

## DoD Wave 1

- [x] Skeleton создан
- [ ] docs-site/ builds без warnings
- [ ] GitHub Pages deploys
- [ ] 3 tutorials читаемы и проходят по шагам
- [ ] Марк одобрил landing page (UI/UX review)

— Solomon (2026-06-21 11:45)
