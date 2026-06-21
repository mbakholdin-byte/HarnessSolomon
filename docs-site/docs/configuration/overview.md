---
id: config-overview
title: Configuration Overview
sidebar_position: 1
slug: /configuration
---

import Tabs from '@theme/Tabs';
import TabItem from '@theme/TabItem';

# Configuration Overview

Harness uses a **layered configuration system** that lets you define settings
at multiple levels. Each layer overrides the one below it, giving you precise
control from development through production.

## How configuration works

Settings are resolved from these layers, in order of priority (highest wins):

1. **CLI flags** — `--setting value` on the command line
2. **Environment variables** — `HARNESS_<SECTION>_<KEY>=value`
3. **`settings.yaml`** — project-level configuration file (`.harness/settings.yaml` or `~/.config/harness/settings.yaml`)
4. **Built-in defaults** — safe, documented defaults compiled into the binary

:::tip Priority rule
A setting defined in `settings.yaml` is **overridden** by an environment variable
with the same name. CLI flags take precedence over both.
:::

## Configuration files

### `settings.yaml`

The primary configuration file. Harness searches for it in this order:

1. `./.harness/settings.yaml` (project-local — recommended for teams)
2. `~/.config/harness/settings.yaml` (user-global)

Settings are organized into sections matching the code structure:

```yaml
# settings.yaml — minimal working example
llm:
  default_provider: minimax
  providers:
    minimax:
      api_key: "${HARNESS_LLM_PROVIDERS_MINIMAX_API_KEY}"
      model: abab6.5s-chat

tier:
  routing_strategy: calibrated
  t1_model: qwen3-8b
  t2_model: glm-4-flash
  t3_model: deepseek-v4

privacy:
  enabled: true
  redaction_sinks: [log, memory, api]
```

### Environment variables

All settings can be set via environment variables using the naming convention
`HARNESS_<SECTION>_<KEY>` (all uppercase). Nested keys use double underscores:

```bash
export HARNESS_LLM_DEFAULT_PROVIDER=minimax
export HARNESS_TIER_ROUTING_STRATEGY=calibrated
export HARNESS_PRIVACY_ENABLED=true
```

### CLI flags

For quick overrides, use `--setting value`:

```bash
harness run --task "summarize README" --llm-default-provider zhipuai
```

## Quick start

The absolute minimum to run Harness is a provider API key:

```bash
export HARNESS_LLM_PROVIDERS_MINIMAX_API_KEY="<your-key>"
harness run --task "Say hello in Russian"
```

No `settings.yaml` is required — defaults will be used for everything else.

## Sections overview

Harness ships with **233 settings** across **42 sections**. Key areas:

| Section | Purpose | Docs |
|---------|---------|------|
| `llm` | Provider selection, API keys, model IDs | [Reference](/configuration/reference#llm) |
| `tier` | T1/T2/T3 routing, cost caps | [Reference](/configuration/reference#tier) |
| `memory` | 4-layer memory backends | [Reference](/configuration/reference) |
| `hooks` | 16 hook events, transports, 12 builtins | [Reference](/configuration/reference#hooks) |
| `privacy` | Regex redaction, 9 sinks | [Reference](/configuration/reference#privacy) |
| `observability` | Prometheus, OTel, JSONL logs | [Reference](/configuration/reference#observability) |
| `auth` | RBAC scopes, token management | [Reference](/configuration/reference#auth) |
| `plugins` | Marketplace trust, signatures | [Reference](/configuration/reference#plugins) |

For the complete list with defaults, types, and descriptions, see the
[Configuration Reference](/configuration/reference) (auto-generated from source).
For a field-by-field mapping of the settings schema to internal code paths, see
[API Config Map](/configuration/api-config-map).

## Profiles

Harness supports **configuration profiles** — named presets that bundle settings
for common scenarios:

<Tabs>
  <TabItem value="dev" label="Development" default>

```yaml
profiles:
  dev:
    observability:
      log_level: DEBUG
      jsonl_enabled: false
    auth:
      auth_required: false
```

  </TabItem>
  <TabItem value="prod" label="Production">

```yaml
profiles:
  prod:
    observability:
      log_level: WARNING
      jsonl_enabled: true
      otel_enabled: true
    auth:
      auth_required: true
```

  </TabItem>
</Tabs>

Activate a profile with `--profile`:

```bash
harness run --profile prod --task "deploy staging"
```

## Common patterns

### "How do I switch the default provider?"

```bash
export HARNESS_LLM_DEFAULT_PROVIDER=zhipuai
```

Or in `settings.yaml`:

```yaml
llm:
  default_provider: zhipuai
```

### "How do I disable privacy redaction for debugging?"

```bash
export HARNESS_PRIVACY_ENABLED=false
```

### "How do I use different models for different task tiers?"

Configure the `tier` section:

```yaml
tier:
  routing_strategy: calibrated
  t1_model: qwen3-8b        # cheap, fast
  t2_model: glm-4-flash     # mid-tier
  t3_model: deepseek-v4     # premium reasoning
```

### "How do I add a custom hook?"

Place a Python file in `.harness/hooks/` and register it:

```yaml
hooks:
  custom:
    on_task_start:
      - path: .harness/hooks/audit_log.py
        args: {log_file: audit.jsonl}
```

:::info More
For hook authoring, see the [Hooks guide](/configuration/reference#hooks)
and the [Plugin marketplace documentation](/configuration/api-config-map).
:::
