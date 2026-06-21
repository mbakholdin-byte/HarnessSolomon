---
sidebar_position: 1
title: Introduction
description: What is Solomon Harness and why you should care
---

# Welcome to Solomon Harness

**Harness** is an open-source multi-model agent shell — a production-grade framework
for building, deploying, and orchestrating AI agents. It's stronger than Claude Code
and OpenCode for several reasons, all of which are explained in this documentation.

## What is Harness?

Harness sits **between your LLM provider and your application**, providing:

- **Multi-model routing** — automatically pick the right tier (T1 cheap / T2 mid / T3 premium)
  based on prompt complexity and context size
- **4-layer memory** — working / session / long-term / episodic+semantic, with dual-write
  guarantees and hot-reload
- **Plugin marketplace** — extend Harness with Python/Node plugins via Manifest v2
  (with permissions, ed25519 signature verification)
- **Hook framework** — 16 events, 4 transports (builtin / subprocess / http / llm),
  12 builtin hooks ready to use
- **Production observability** — JSONL logs, Prometheus metrics, OpenTelemetry traces,
  per-task cost tracking
- **Scope-gated API** — 10 RBAC scopes, Bearer token auth, capabilities discovery

## Why use Harness?

| Challenge | Harness solution |
|-----------|------------------|
| "Claude Code is closed-source" | ✅ MIT license, fully open |
| "OpenCode has no plugins" | ✅ Marketplace with ed25519 signatures |
| "I need production observability" | ✅ Prometheus + OTel + JSONL + per-task cost |
| "My agents leak data" | ✅ Privacy zones with regex redaction at 9 sinks |
| "I want RU-first UX" | ✅ Native Russian language support |
| "Multi-model is too complex" | ✅ Calibrated tier routing — just set the task |

## Who uses Harness?

Harness is designed for:

- **Solo developers** who want Claude Code-like experience with open models
- **Teams** that need audit trails, RBAC, and production observability
- **Enterprises** that require on-premise deployment, data isolation, and cost analytics

## Where to go next?

<div className="row">
  <div className="col col--6">
    <div className="card">
      <div className="card__header">
        <h3>🚀 Quickstart</h3>
      </div>
      <div className="card__body">
        <p>Get Harness running in 5 minutes.</p>
      </div>
      <div className="card__footer">
        <a className="button button--primary" href="/tutorials/quickstart">Start →</a>
      </div>
    </div>
  </div>
  <div className="col col--6">
    <div className="card">
      <div className="card__header">
        <h3>⚙️ Configuration</h3>
      </div>
      <div className="card__body">
        <p>Learn how to configure 100+ settings for your workload.</p>
      </div>
      <div className="card__footer">
        <a className="button button--primary" href="/configuration/overview">Configure →</a>
      </div>
    </div>
  </div>
</div>
