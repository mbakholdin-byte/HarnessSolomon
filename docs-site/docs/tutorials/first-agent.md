---
id: first-agent
title: Your First Agent
sidebar_position: 2
description: Build a multi-step agent with tools, memory, and planning in 15 minutes
custom_edit_url: https://github.com/mbakholdin-byte/HarnessSolomon/tree/main/docs-site/docs/tutorials/first-agent.md
---

import CodeBlock from '@theme/CodeBlock';

# Your First Agent

**Time:** 15 minutes. **Difficulty:** Beginner.

This tutorial walks you through creating your first Harness agent from scratch.
By the end, you will have an agent that reads PDF files, searches the web, and
remembers context across conversations.

---

## 1. What you'll build

An agent that:

- Reads a PDF document and answers questions about its contents
- Uses web search to enrich responses with current information
- Maintains short-term memory (scratchpad) for multi-turn reasoning
- Persists important facts to long-term memory across sessions

```text
User: "Summarize the key points from report.pdf"
Agent: [reads file → summarises] → "The report covers..."
User: "How does this compare to industry trends?"
Agent: [web search → compares] → "Current trends show..."
```

---

## 2. Prerequisites

- **Harness installed** — see the [Quickstart](/tutorials/quickstart) if you
  haven't already
- **Python 3.12+** with `pip`
- **At least one API key** configured (MiniMax, ZhipuAI, or Moonshot)
- **Docker** running (PostgreSQL + Qdrant + OpenSearch)

Check your setup:

```bash
harness --version
# Should print: harness, version 1.40.0

harness serve --check
# Should print: All dependencies OK
```

---

## 3. Step 1: Initialize the project

Create a new Harness project:

```bash
harness init my-agent
cd my-agent
```

This creates the following structure:

```text
my-agent/
├── .harness/
│   ├── agents/
│   │   └── main.agent.md    # Your agent prompt
│   ├── plugins/              # Future plugins go here
│   └── hooks/                # Future hooks go here
├── settings.yaml             # Your configuration
└── harness.lock              # Dependency lockfile (gitignored)
```

:::tip
The `.harness/` directory is the heart of your project. All agent prompts,
plugins, and hooks live here. Harness hot-reloads changes automatically — no
restart needed.
:::

---

## 4. Step 2: Configure the model

Open `settings.yaml` and configure your provider:

```yaml
# settings.yaml
project_root: .

# Provider: choose one
minimax_api_key: "${MINIMAX_API_KEY}"   # Recommended
# zhipuai_api_key: "${ZHIPUAI_API_KEY}" # GLM models
# moonshot_api_key: "${MOONSHOT_API_KEY}" # 128K context

# Tier routing (optional — defaults are sensible)
tier:
  t1_model: "MiniMax-M2.7"    # Cheap, for simple tasks
  t2_model: "MiniMax-M2.7"    # Mid-tier
  t3_model: "MiniMax-M2.7"    # Premium — large context

# Memory layers
memory:
  l0_scratchpad_max_tokens: 2000   # Working memory
  l2_persistent_enabled: true      # Long-term (Qdrant)
  l3_episodic_enabled: true        # Episodic (Neo4j)
```

Harness auto-selects T1/T2/T3 based on prompt complexity and context size.
For this tutorial, all three tiers point to the same model.

:::info Tier routing
T1 handles simple prompts (&lt;500 tokens), T2 for moderate tasks, T3 for complex
reasoning or >32K context. You can override per-task with `--tier t3`.
:::

---

## 5. Step 3: Write the agent prompt

Edit `.harness/agents/main.agent.md`:

```markdown
You are a research assistant. You help users understand documents, find
information, and draw connections.

## Your tools

You have access to:
- `read_file(path)` — read any file from the project
- `web_search(query)` — search the web for current information
- `memory_search(query)` — find facts from past conversations

## Your behavior

1. **Be concise** — answer in 3–5 sentences unless the user asks for detail
2. **Cite sources** — when using web_search, mention the source URL
3. **Admit uncertainty** — if you don't know, say so
4. **Use memory** — before answering, check if you have relevant past context

## Example

User: "What's in the Q3 report?"
You: [read_file("Q3-report.pdf")] → "The Q3 report shows revenue grew 12% YoY..."
```

The agent prompt is Markdown. Harness injects it as the system message before
every conversation turn. You can use headings, lists, and code blocks — they
become part of the LLM context.

:::warning
Keep the prompt under 2000 tokens. Longer prompts reduce the available context
window for the actual conversation. Use `harness agents inspect` to check token
count.
:::

---

## 6. Step 4: Add tools

Harness ships with built-in tools. Enable them in `settings.yaml`:

```yaml
# settings.yaml (append to existing file)
tool:
  read_file_enabled: true
  web_search_enabled: true
  web_search_provider: "tavily"     # or "duckduckgo" for free tier
  web_search_max_results: 5
  sandbox_enabled: false            # Enable for code execution (Docker required)
```

Set the Tavily API key if using web search:

```bash
export TAVILY_API_KEY="tvly-..."
```

:::tip
`web_search_provider: "duckduckgo"` works without an API key. Use it for
development. Switch to Tavily for production — it returns higher-quality
results with fewer hallucinations.
:::

---

## 7. Step 5: Add memory

Harness has 4 memory layers. For this tutorial, we'll use two:

**L0 — Scratchpad (working memory):**
Stored in-memory during a session. Holds the last N turns for context.

```yaml
# settings.yaml
compaction:
  compaction_enabled: true
  compaction_keep_recent_turns: 6   # Keep last 6 turns verbatim
  compaction_trigger: "token"       # Compact when 75% of context window is full
```

**L2 — Persistent memory (long-term):**
Stored in Qdrant. Survives restarts. Agents can recall facts from weeks ago.

```yaml
# settings.yaml
memory:
  l2_persistent_enabled: true
  l2_persistent_collection: "harness-memory"
```

Now your agent remembers facts across sessions. Try it:

```bash
harness run --prompt "Remember: the Q3 report was published on October 15, 2025"
# Agent: "Got it. I've stored that fact."

harness run --prompt "When was the Q3 report published?"
# Agent: "October 15, 2025."  ← Retrieved from L2 memory!
```

---

## 8. Step 6: Run and test

Start the backend (if not already running):

```bash
harness serve
# Server running on http://0.0.0.0:8765
```

In another terminal, run your agent:

```bash
harness run --prompt "Read report.pdf and give me a one-paragraph summary"
```

Expected output:

```text
🤖 Harness v1.40.0 | Model: MiniMax-M2.7 (T1)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[Tool: read_file("report.pdf")]
✅ Read 3,421 chars from report.pdf

[Agent]
The report analyzes Q3 financial performance across three business units.
Revenue grew 12% YoY to $4.2M, driven primarily by the Cloud division (+18%).
Operating margins improved from 22% to 27%. The key risk identified is
customer concentration — the top 3 clients account for 45% of revenue.

⏱️ 1.2s | 💰 $0.0023 | 🔤 342 tokens
```

If you see errors:

- **`Provider not found`** — check your API key is exported
- **`Tool unavailable`** — verify `tool.read_file_enabled: true`
- **`Memory error`** — ensure Qdrant is running (`docker compose up -d`)

---

## 9. Step 7: Multi-turn conversation

Start an interactive session:

```bash
harness chat
```

```text
You: Summarize report.pdf
Agent: [reads file → summary]

You: What's the biggest risk?
Agent: [memory_search → finds "customer concentration"] →
      The biggest risk is customer concentration — the top 3 clients
      represent 45% of revenue, making the business vulnerable to
      losing any single client.

You: Search for industry benchmarks on customer concentration
Agent: [web_search → "SaaS customer concentration benchmark 2025"] →
      According to a 2025 SaaS Metrics report by ChartMogul, the median
      customer concentration for Series A companies is 25-30%. At 45%,
      this company is above the 75th percentile, which is a yellow flag
      for investors.
```

Each turn adds context to the scratchpad. When the context window fills up
(75% threshold), Harness automatically compacts older turns into a summary.

---

## 10. What's next?

Congratulations! You've built an agent with tools, memory, and multi-turn
capabilities. Here's where to go next:

- **[Plugin Development](/tutorials/plugin-development)** — extend Harness
  with custom plugins (weather, databases, APIs)
- **[Configuration Reference](/configuration/reference)** — explore all
  233 settings for production tuning
- **[Hooks Framework](/api/hooks)** — intercept and modify agent behavior
  at 16 lifecycle events
- **[Sub-agents](/api/agents)** — delegate tasks to specialized sub-agents

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| `ModuleNotFoundError: harness` | Not installed | `pip install -e .` from repo root |
| `Connection refused` on :8765 | Backend not running | `harness serve` in another terminal |
| Agent returns generic answers | Prompt too vague | Add more detail to `.harness/agents/main.agent.md` |
| Memory not persisting | Qdrant not running | `docker compose -f docker/docker-compose.yml up -d` |
| `TAVILY_API_KEY not set` | Missing API key | `export TAVILY_API_KEY="..."` or switch to DuckDuckGo |
