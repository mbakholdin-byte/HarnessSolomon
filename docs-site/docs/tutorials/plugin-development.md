---
id: plugin-development
title: Plugin Development
sidebar_position: 3
description: Write a Harness plugin with Manifest v2 — tools, hooks, and marketplace publishing
custom_edit_url: https://github.com/mbakholdin-byte/HarnessSolomon/tree/main/docs-site/docs/tutorials/plugin-development.md
---

import CodeBlock from '@theme/CodeBlock';

# Plugin Development

**Time:** 30 minutes. **Difficulty:** Intermediate.

Plugins are the primary way to extend Harness. This tutorial walks you through
writing a weather-data plugin with Manifest v2 — from local development to
marketplace installation.

---

## 1. What you'll build

A plugin that:

- Registers a `get_weather` tool — fetches current conditions for any city
- Hooks into `PostToolUse` to suggest relevant weather when the agent reads a
  location-related file
- Uses Manifest v2 with permissions, ed25519 signature, and trust registry
- Installs from the Harness Marketplace via CLI

```text
User: "What's the weather in Tokyo?"
Agent: [get_weather("Tokyo")] → "Tokyo: 22°C, partly cloudy, humidity 65%"
```

---

## 2. Prerequisites

- **Harness v1.40+** installed and running
- **Python 3.12+** — plugins are Python modules
- **Basic Python knowledge** — async/await, type hints
- **OpenWeatherMap API key** (free tier at [openweathermap.org](https://openweathermap.org/api))

```bash
harness --version
# Should print: harness, version 1.40.0
```

---

## 3. Step 1: Plugin structure

Create a plugin directory:

```bash
mkdir -p .harness/plugins/weather
```

Every Harness plugin consists of two files:

```text
.harness/plugins/weather/
├── __init__.py          # Plugin entry point
└── MANIFEST_V2          # Manifest v2 descriptor
```

Create `.harness/plugins/weather/__init__.py`:

```python
"""Weather plugin — adds real-time weather data to agent responses."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lifecycle hooks — called by Harness at specific moments
# ---------------------------------------------------------------------------


def on_load(config: dict[str, Any]) -> None:
    """Called once when the plugin is loaded."""
    logger.info("Weather plugin loaded. API key configured: %s",
                "yes" if config.get("weather_api_key") else "no")


def on_unload() -> None:
    """Called when the plugin is unloaded or the server shuts down."""
    logger.info("Weather plugin unloaded.")
```

:::info
`on_load` and `on_unload` are the only required lifecycle hooks. Harness calls
them during server startup/shutdown. Use `on_load` to initialize connections,
load models, or validate configuration.
:::

---

## 4. Step 2: Manifest v2 fields

Create `.harness/plugins/weather/MANIFEST_V2`:

```json
{
  "name": "weather",
  "version": "1.0.0",
  "manifest_version": 2,
  "description": "Adds real-time weather data via OpenWeatherMap API",
  "author": "Your Name <you@example.com>",
  "license": "MIT",
  "homepage": "https://github.com/your-org/harness-weather-plugin",
  "harness_version": ">=1.40.0",
  "permissions": [
    "tool:register",
    "hook:post_tool_use",
    "http:api.openweathermap.org"
  ],
  "config_schema": {
    "type": "object",
    "properties": {
      "weather_api_key": {
        "type": "string",
        "description": "OpenWeatherMap API key (free tier: https://openweathermap.org/api)"
      },
      "units": {
        "type": "string",
        "enum": ["metric", "imperial"],
        "default": "metric",
        "description": "Temperature units"
      }
    },
    "required": ["weather_api_key"]
  },
  "signature": {
    "algorithm": "ed25519",
    "public_key": "",  
    "signature": ""     
  }
}
```

**Key fields explained:**

| Field | Purpose |
|-------|---------|
| `permissions` | Declares what the plugin can do. Harness enforces this — a plugin cannot call `http://evil.com` if only `api.openweathermap.org` is listed. |
| `config_schema` | JSON Schema for plugin configuration. Harness validates `settings.yaml` against this before loading. |
| `signature` | ed25519 signature for marketplace verification. Empty during development — you'll sign it in Step 5. |

:::warning Permissions are mandatory
A plugin without `permissions` in its manifest will NOT load. Harness blocks
all network, filesystem, and tool access by default. Be explicit.
:::

---

## 5. Step 3: Plugin code

Expand `__init__.py` with the tool and hook:

```python
"""Weather plugin — adds real-time weather data to agent responses."""

from __future__ import annotations

import logging
from typing import Any
import aiohttp

logger = logging.getLogger(__name__)

# Module-level config — set by on_load
_config: dict[str, Any] = {}


def on_load(config: dict[str, Any]) -> None:
    global _config
    _config = config
    logger.info("Weather plugin loaded. Units: %s", config.get("units", "metric"))


def on_unload() -> None:
    logger.info("Weather plugin unloaded.")


# ---------------------------------------------------------------------------
# Tool: register_tool
# ---------------------------------------------------------------------------


def register_tool() -> dict[str, Any]:
    """Register the get_weather tool with Harness."""
    return {
        "name": "get_weather",
        "description": "Get current weather for a city. Returns temperature, "
                       "conditions, humidity, and wind speed.",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {
                    "type": "string",
                    "description": "City name (e.g. 'Tokyo', 'London', 'Moscow')"
                }
            },
            "required": ["city"]
        }
    }


async def get_weather(city: str) -> str:
    """Fetch current weather from OpenWeatherMap."""
    api_key = _config.get("weather_api_key")
    units = _config.get("units", "metric")

    if not api_key:
        return "Error: weather_api_key not configured. Set it in settings.yaml."

    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {"q": city, "appid": api_key, "units": units}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 404:
                    return f"City '{city}' not found."
                elif resp.status == 401:
                    return "Error: invalid API key. Check your weather_api_key."
                resp.raise_for_status()
                data = await resp.json()

        temp = data["main"]["temp"]
        conditions = data["weather"][0]["description"]
        humidity = data["main"]["humidity"]
        wind = data["wind"]["speed"]
        unit_symbol = "°C" if units == "metric" else "°F"

        return (
            f"{data['name']}, {data['sys']['country']}: "
            f"{temp}{unit_symbol}, {conditions}, "
            f"humidity {humidity}%, wind {wind} m/s"
        )
    except aiohttp.ClientError as e:
        logger.error("Weather API request failed: %s", e)
        return f"Error: could not reach weather service ({e})"


# ---------------------------------------------------------------------------
# Hook: PostToolUse
# ---------------------------------------------------------------------------


async def hook_on_post_tool_use(payload: dict[str, Any]) -> dict[str, Any] | None:
    """
    After the agent reads a file, check if it mentions a city.
    If so, suggest fetching the weather.
    """
    tool_name = payload.get("tool_name", "")

    # Only trigger after file reads
    if tool_name != "read_file":
        return None

    result = payload.get("result", "")
    if not isinstance(result, str):
        return None

    # Simple heuristic: does the file mention a known city?
    # In production, use NER or an LLM call.
    cities = ["Tokyo", "London", "Moscow", "Berlin", "Paris", "New York", "Beijing"]
    mentioned = [c for c in cities if c.lower() in result.lower()]

    if not mentioned:
        return None

    return {
        "decision": "allow",
        "context_inject": (
            f"The file mentions {', '.join(mentioned)}. "
            f"Consider offering to check the current weather there "
            f"using the get_weather tool."
        )
    }
```

:::tip Tool naming
Tool names in Harness are `snake_case` by convention. The LLM sees the
`description` field, so make it descriptive — it's part of the function-calling
prompt.
:::

---

## 6. Step 4: Test locally

Enable plugins in `settings.yaml`:

```yaml
# settings.yaml
plugins:
  plugins_enabled: true
  plugins_dir: ".harness/plugins"
  plugins_allowed: ["weather"]     # Only load the weather plugin

  # Plugin-specific config
  weather:
    weather_api_key: "${OPENWEATHER_API_KEY}"
    units: "metric"
```

Set your API key:

```bash
export OPENWEATHER_API_KEY="your-key-here"
```

Verify the plugin is loaded:

```bash
harness plugins list
```

Expected output:

```text
Loaded plugins (1):
  weather  v1.0.0  —  Adds real-time weather data via OpenWeatherMap API
     Tools: get_weather
     Hooks: PostToolUse
```

Test the tool directly:

```bash
harness plugins test weather get_weather --params '{"city": "Tokyo"}'
```

Expected output:

```text
Tool: weather.get_weather("Tokyo")
Result: Tokyo, JP: 22°C, partly cloudy, humidity 65%, wind 3.6 m/s
```

Run an end-to-end test with the agent:

```bash
harness run --prompt "What's the weather in Berlin?"
# Agent calls get_weather("Berlin") → returns temperature + conditions
```

---

## 7. Step 5: Sign your plugin

For marketplace publishing, plugins must be signed with ed25519.

Generate a key pair:

```bash
harness plugins keygen --name my-key
# Created: ~/.harness/keys/my-key      (private — keep secret!)
# Created: ~/.harness/keys/my-key.pub  (public — goes in MANIFEST_V2)
```

Sign the plugin:

```bash
harness plugins sign --key my-key --plugin .harness/plugins/weather
```

This updates `MANIFEST_V2` with the `signature` block:

```json
{
  "signature": {
    "algorithm": "ed25519",
    "public_key": "mcGzP1qX...",
    "signature": "9aF3kLm2..."
  }
}
```

:::warning
**Never commit the private key.** Add `~/.harness/keys/` to `.gitignore`.
Only the public key goes into the manifest. Harness verifies the signature
against the public key at install time.
:::

---

## 8. Step 6: Install via Marketplace CLI

Publish your plugin to the Harness Marketplace (requires a GitHub repo):

```bash
harness plugins publish --plugin .harness/plugins/weather
```

Users install it with a single command:

```bash
harness plugins install weather
```

Harness will:
1. **Fetch** the manifest from the marketplace registry
2. **Verify** the ed25519 signature against the declared public key
3. **Check** permissions against the trust registry
4. **Install** the plugin to `.harness/plugins/weather`
5. **Load** it on the next server restart (or immediately with hot-reload)

---

## 9. Step 7: Trust registry configuration

The trust registry controls which plugins and permissions are allowed:

```yaml
# settings.yaml
trust:
  trust_mode: "verify"              # "off" | "warn" | "verify" | "enforce"
  trust_allow_unsigned: false       # Reject plugins without valid signatures
  trust_allowed_keys:               # Whitelist of public keys
    - "mcGzP1qX..."                 # Your key
  trust_allowed_permissions:        # Whitelist of permissions
    - "tool:register"
    - "hook:post_tool_use"
    - "http:api.openweathermap.org"
  trust_deny_permissions:           # Blacklist (overrides whitelist)
    - "http:*"                      # Block all HTTP except explicitly allowed
```

| Mode | Behavior |
|------|----------|
| `off` | No verification. Unsafe — use only in isolated dev environments. |
| `warn` | Verify, log warnings for violations, but load anyway. |
| `verify` | Verify, refuse to load plugins that fail. **Recommended for production.** |
| `enforce` | Verify + check trust_allowed_permissions. Strictest mode. |

:::tip
Start with `warn` during development, switch to `verify` in production, and use
`enforce` in regulated environments (finance, healthcare) where every permission
must be pre-approved.
:::

---

## 10. Best practices

### Permissions

- **Least privilege** — only request the permissions your plugin actually uses.
  `http:*` is a red flag in code review.
- **Specific domains** — prefer `http:api.service.com` over `http:*`.
- **Document why** — add a comment in `MANIFEST_V2` explaining each permission.

### Backward compatibility

- **Semantic versioning** — `MAJOR.MINOR.PATCH`. Bump MAJOR when removing
  tools or changing hook signatures.
- **Deprecation notices** — if you must remove a tool, keep it for one minor
  version with a deprecation warning in the return value.
- **Config schema evolution** — add new fields as optional. Make required
  fields required only when bumping MAJOR.

### Testing

```bash
# Unit-test your tool functions
python -m pytest tests/

# Integration-test the plugin in Harness
harness plugins test weather get_weather --params '{"city": "London"}'

# Test with a real agent
harness run --prompt "Weather in Paris?"
```

### Observability

Harness automatically instruments plugins:

```bash
harness observability metrics | grep weather
# Output:
# plugin_tool_calls_total{plugin="weather",tool="get_weather"} 42
# plugin_tool_duration_ms{plugin="weather",tool="get_weather",quantile="0.5"} 320
# plugin_load_errors_total{plugin="weather"} 0
```

No extra code needed — Harness wraps every tool call and hook in a span.

---

## 11. What's next?

- **[Configuration Reference](/configuration/reference)** — tune plugin
  settings (trust modes, dispatch, admin API)
- **[API: Plugins](/api/plugins)** — REST API for plugin management
  (list, install, uninstall, enable/disable)
- **[Hooks Framework](/api/hooks)** — explore all 16 hook events your
  plugin can intercept
- **[Marketplace Publishing Guide](/api/marketplace)** — submit your
  plugin to the public registry

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Plugin not in `plugins list` | `plugins_enabled: false` | Set `plugins_enabled: true` |
| Plugin loads but no tools | `permissions` missing `tool:register` | Add to manifest |
| `Permission denied: http:*` | Trust registry blocks the domain | Add domain to `trust_allowed_permissions` |
| `Invalid signature` | Manifest modified after signing | Re-run `harness plugins sign` |
| `ModuleNotFoundError: aiohttp` | Missing dependency | `pip install aiohttp` |
