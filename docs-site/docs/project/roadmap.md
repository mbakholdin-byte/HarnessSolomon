---
sidebar_position: 1
title: Roadmap
description: What we shipped, what we're building, and what's planned
---

# Harness Roadmap

Updated weekly. Last update: 2026-06-21.

## ✅ Shipped (33 releases, 12 phases, 8 days)

| Phase | Tag | What | Date |
|-------|-----|------|------|
| 0 — Foundation | v0.1.0 | FastAPI + Vite + 9 tools + WebSocket | 13.06 |
| 1.6 — Auth | v0.6.0 | Scope-gated API + Bearer tokens | 14.06 |
| 2 — Sub-agents | v0.9.0 | Registry + worktree + PR + stacked | 15.06 |
| 3 — Production | v1.0.0 | Compactor + privacy + embeddings | 16.06 |
| 3.5 | v1.1.0 | Persistent compact store | 16.06 |
| 3 — v1.2-1.5 | v1.5.0 | Write + Select + Offload + Reflect | 16-17.06 |
| 4 — Hooks | v1.16.0 | 16 events, 4 transports, observability | 16-17.06 |
| 5 — Evals | v1.25.0 | RAG tests + Privacy Zones admin | 18-19.06 |
| 6 — Performance | v1.29.0 | Tier Router + Rust hot paths + Plugin | 19-20.06 |
| 7.2 — Web UI | v1.30.0 | React SPA + FastAPI mount | 20.06 |
| 7.3 | v1.31.0 | WebSocket + Audit UI + Rust ed25519 | 20.06 |
| 7.4 — Plugin Marketplace | v1.32.0 | REST API + CLI + UI + Trust Registry | 21.06 |
| 7.5 — Tier Router Calibration | v1.33.0 | 6 thresholds calibrated | 21.06 |
| 7.6 — context_tokens | v1.34.0 | LLM usage tracking + recalibration | 21.06 |

## 🚧 In Progress

- **Phase 8.0a — Public Docs MVP** (deadline 02.07.2026) — Docusaurus site, configuration reference, API reference

## 📅 Planned (next 6 months)

| Phase | What | Target | Effort |
|-------|------|--------|--------|
| 8.0b | Full docs (tutorials, troubleshooting) | 30.07 | 6-8 weeks |
| 8.1 | Benchmark suite (vs Claude Code / OpenCode) | 30.08 | 6-8 weeks |
| 8.2 | Observability v2 (OpenTelemetry, Grafana) | 30.08 | 6-8 weeks |
| 8.3 | Production hardening (Helm, security audit) | 15.10 | 10-12 weeks |
| 8.4 | Plugin Marketplace GA (payments, analytics) | 15.11 | 12-14 weeks |
| 9 | Multi-tenant / SaaS | 22.12 | 16-24 weeks |

## 💡 Have an idea?

Open an issue on [GitHub](https://github.com/mbakholdin-byte/HarnessSolomon/issues)
with the `roadmap` label. We review every proposal.

## See also

- [Changelog](./changelog) — every release notes
- [Architecture overview](../architecture/overview) — what's shipping
- [Contributing](./contributing) — how to help
