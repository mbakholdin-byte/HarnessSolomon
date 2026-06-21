---
sidebar_position: 1
title: General FAQ
description: Frequently asked questions about Harness
---

# General FAQ

## What is Harness?

Solomon Harness is an open-source agentic runtime for AI. It's a production-grade
framework for running AI agents with cost optimization, privacy, and observability.

## Is Harness free?

Yes — Harness core is MIT-licensed, free for commercial use. You only pay for
the LLM API calls (e.g., Claude, MiniMax, GLM, OpenAI). With T1 local model,
80% of queries are free.

## How is it different from Claude Code / OpenCode?

| | Harness | Claude Code | OpenCode |
|---|---------|-------------|----------|
| **Open-source** | ✅ MIT | ❌ Proprietary | ✅ |
| **Cost optimization** | ✅ Auto T1/T2/T3 | ❌ Single model | ⚠️ Manual |
| **Plugin marketplace** | ✅ With ed25519 | ❌ | ⚠️ Beta |
| **Privacy zones** | ✅ 9 sinks | ❌ | ❌ |
| **On-premise** | ✅ Self-hosted | ❌ Cloud-only | ⚠️ |
| **RU-first** | ✅ | ❌ | ❌ |

## What's the smallest viable setup?

Single Docker container + one local LLM (Ollama) = **$0/month**.

## Can I run Harness without internet?

Yes — fully air-gapped. Install Qdrant, SQLite, Ollama locally. Only outbound
traffic is to your LLM provider.

## How do I migrate from Claude Code?

See [Migration guide v1.32 → v1.40](../migration/v1.32-to-v1.40). Most prompts
work as-is.

## Can I use Harness with my own model?

Yes — supports any OpenAI-compatible API. Just add a provider in config:

```yaml
llm:
  providers:
    - name: my-model
      base_url: http://my-llm:8080/v1
      model: my-model-1.0
```

## Where is the data stored?

- **SQLite** (default) at `data/harness.db`
- **Vectors** in Qdrant (port 6333)
- **Logs** at `data/harness-YYYY-MM-DD.jsonl`
- All paths configurable

## How do I contribute?

See [Contributing](../project/contributing).
