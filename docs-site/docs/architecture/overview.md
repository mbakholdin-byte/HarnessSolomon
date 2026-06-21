---
sidebar_position: 1
title: Architecture Overview
description: High-level architecture of Solomon Harness
---

# Architecture Overview

Harness is built on a **layered, modular architecture** designed for production
deployment. This page gives you the 10,000-foot view.

## Core Components

```
┌────────────────────────────────────────────────────────────┐
│  Client (Web UI, CLI, Desktop, API consumer)             │
└──────────────────┬─────────────────────────────────────────┘
                   │ HTTPS / WebSocket
┌──────────────────▼─────────────────────────────────────────┐
│  FastAPI server (harness/server)                          │
│  ├─ Scope-gated auth (Bearer + RBAC)                     │
│  ├─ REST routes (/api/v1/*)                              │
│  ├─ WebSocket (/api/v1/observability/ws, /elicitation)  │
│  └─ 16 hook events (Pre/Post tool use)                    │
└──────────────────┬─────────────────────────────────────────┘
                   │
┌──────────────────▼─────────────────────────────────────────┐
│  Agent runtime                                            │
│  ├─ Tier Router (T1 local / T2 mid / T3 cloud)         │
│  ├─ 4-layer memory (working / session / long / episodic)│
│  ├─ Plugin system (AST-scanned, ed25519 signed)          │
│  ├─ Hook framework (builtin + subprocess + http + llm)  │
│  └─ Background jobs (SQLite JobStore)                   │
└──────────────────┬─────────────────────────────────────────┘
                   │
┌──────────────────▼─────────────────────────────────────────┐
│  Storage tier                                             │
│  ├─ SQLite (jobs, hooks, plugins, sessions)              │
│  ├─ PostgreSQL (optional, scale)                         │
│  ├─ Qdrant (vectors)                                     │
│  └─ OpenSearch (full-text + audit)                      │
└────────────────────────────────────────────────────────────┘
```

## Layered Design

### Layer 1: Interface

- **Web UI** (React SPA, Vite) at `/ui`
- **REST API** at `/api/v1/*` with 51 endpoints
- **WebSocket** for real-time observability + elicitation
- **CLI** at `harness <subcommand>`

### Layer 2: Auth & Hooks

- **Scope-gated API** — 7 RBAC scopes, Bearer tokens
- **Hook framework** — 16 events, 4 transports
- **Audit log** — every privileged operation logged

### Layer 3: Agent runtime

- **Tier Router** — auto-selects T1/T2/T3 per task complexity
- **Sub-agents** — 4 built-in (explore/plan/code/review)
- **Memory** — 4 layers with cross-session persistence
- **Plugin system** — extensible via Manifest v2

### Layer 4: Storage

- **SQLite** (default) — main data
- **PostgreSQL** (optional) — for scale
- **Qdrant** (vector DB) — embeddings
- **OpenSearch** (optional) — full-text + audit

## Key Design Principles

| Principle | Implementation |
|-----------|----------------|
| **Privacy-first** | 9 sinks auto-redact PII (passport, email, card) before any LLM call |
| **Cost-aware** | Tier Router puts 80% of traffic in local T1 (free) |
| **Plugin-safe** | AST pre-scan + restricted globals + ed25519 signatures |
| **Observable** | JSONL + Prometheus + OpenTelemetry — every metric exported |
| **Hot-reload** | Privacy zones, agents, hooks, plugins reload without restart |
| **Multi-tenant-ready** | Org/team isolation built in (Phase 9 = SaaS) |

## See Also

- [Memory Layers](./memory-layers) — detailed memory architecture
- [Plugin System](./plugin-system) — how plugins work
- [Routing](./routing) — tier router details
- [Configuration Overview](../configuration/overview) — 233 settings
- [API Reference](../api/overview) — 51 endpoints
