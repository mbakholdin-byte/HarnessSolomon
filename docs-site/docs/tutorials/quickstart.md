---
sidebar_position: 1
title: Quickstart
description: Get Harness running in 5 minutes
---

# Quickstart — 5 minutes to your first agent

This tutorial walks you through installing Harness, configuring your first LLM
provider, and running a simple agent task.

## Prerequisites

- **Python 3.11+** (3.12 recommended)
- **Node.js 18+** (for plugin system)
- **Docker** (for PostgreSQL, Qdrant, OpenSearch — required for memory layers)
- **API key** from at least one provider:
  - [MiniMax](https://api.minimax.io) (default, recommended)
  - [ZhipuAI](https://open.bigmodel.cn) (GLM models)
  - [Moonshot](https://platform.moonshot.cn) (128K context)

## Step 1: Install Harness

```bash
git clone https://github.com/mbakholdin-byte/HarnessSolomon.git
cd HarnessSolomon
python -m pip install -e .
```

Verify installation:

```bash
harness --version
# Should print: harness, version 1.40.0
```

## Step 2: Configure API key

```bash
export MINIMAX_API_KEY="sk-..."
# Or use a different provider:
# export ZHIPUAI_API_KEY="..."
# export MOONSHOT_API_KEY="..."
```

> **Tip:** Add this to your `~/.bashrc` or `~/.zshrc` to persist across sessions.

## Step 3: Start the backend

Harness needs PostgreSQL, Qdrant, and OpenSearch for full functionality. The
easiest way is via Docker Compose:

```bash
docker compose -f docker/docker-compose.yml up -d
```

This starts all dependencies on default ports:
- PostgreSQL: `localhost:5432`
- Qdrant: `localhost:6333`
- OpenSearch: `localhost:9200`

Then start the Harness backend:

```bash
harness serve
# Server running on http://0.0.0.0:8765
```

## Step 4: Run your first agent

In a new terminal:

```bash
harness agents run --prompt "Write a Python function to compute factorial"
```

Harness will:
1. **Route** the task to T1 (cheap tier) since the prompt is simple
2. **Call** MiniMax-M2.7 (or your configured model)
3. **Stream** the response to your terminal
4. **Log** the call to `data/harness-2026-06-21.jsonl`

You should see something like:

```python
def factorial(n: int) -> int:
    """Compute n! recursively."""
    if n < 0:
        raise ValueError("factorial() not defined for negative values")
    if n == 0:
        return 1
    return n * factorial(n - 1)
```

## Step 5: Check observability

```bash
harness observability metrics
# Output:
# llm_calls_total{model="MiniMax-M2.7",tier="T1"} 1
# llm_cost_total_usd{model="MiniMax-M2.7"} 0.0023
# llm_tokens_total{model="MiniMax-M2.7",type="prompt"} 24
```

## What's next?

Now that you have Harness running, explore:

- **[First Agent tutorial](/tutorials/first-agent)** — build a multi-step agent
  with planning, tool calls, and reflection
- **[Plugin Development](/tutorials/plugin-development)** — write your first
  Manifest v2 plugin
- **[Configuration overview](/configuration/overview)** — tune 100+ settings
  for your workload

## Troubleshooting

If something doesn't work:

1. **Check prerequisites** — Python 3.11+, Node 18+, Docker running
2. **Verify API key** — `echo $MINIMAX_API_KEY` should print your key
3. **Check backend logs** — `harness serve --log-level debug`
4. **See [Common Errors](/troubleshooting/common-errors)** — top 20 issues + fixes

Happy hacking! 🚀
