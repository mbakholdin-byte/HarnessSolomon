# Plugins — Author Guide

**Audience:** Developers who want to extend Solomon Harness with custom plugins.

**What is a plugin?** A plugin is a Python file that hooks into Harness lifecycle events (tool use, session start/end, memory writes, etc.) and runs custom logic. Plugins run in the **same Python process** as Harness (in-process dispatch, see `harness/plugins/dispatcher.py`).

**Since:** v1.28.0 (Phase 6.3 Plugin Dispatch Integration).

---

## Quick Start

### 1. Create your first plugin

Create `.harness/plugins/tool_logger.py`:

```python
"""Log every tool call to stderr."""

PLUGIN_NAME = "tool_logger"
PLUGIN_VERSION = "0.1.0"


def register(registry):
    """Called by Harness on startup. Register hooks via the registry."""
    @registry.register_hook("OnToolUse")
    def log_tool_use(payload):
        tool_name = payload.get("tool_name", "unknown")
        status = payload.get("status", "unknown")
        print(f"[tool_logger] {tool_name} → {status}", file=__import__("sys").stderr)
```

That's it. Harness will call your hook on every tool invocation.

### 2. Enable plugins

Plugins are **opt-in**. Set in `~/.harness/config.yaml`:

```yaml
plugins_enabled: true
plugins_dir: .harness/plugins/
plugins_allowed:
  - tool_logger
```

Or via environment variables:

```bash
export HARNESS_PLUGINS_ENABLED=true
export HARNESS_PLUGINS_DIR=.harness/plugins/
export HARNESS_PLUGINS_ALLOWED="tool_logger,audit_logger"
```

### 3. Restart Harness

```bash
cd /c/MyAI/06_Harness
.venv/Scripts/python.exe -m uvicorn harness.server.app:create_app --factory --reload
```

Your plugin is loaded automatically on startup.

---

## Available Hook Events

Harness fires hooks on these events. Subscribe to any combination:

| Event | When | Payload |
|-------|------|---------|
| `OnSessionStart` | Session begins | `{session_id, user_id, timestamp}` |
| `OnSessionEnd` | Session ends | `{session_id, message_count, total_tokens, total_cost}` |
| `OnUserPromptSubmit` | User sends message | `{session_id, prompt_text, timestamp}` |
| `OnPreCompact` | Before context compaction | `{session_id, pre_tokens, target_tokens, model}` |
| `OnPostCompact` | After compaction | `{session_id, pre_tokens, post_tokens, ratio}` |
| `OnToolUse` | Tool is invoked | `{session_id, tool_name, arguments, status, latency_ms}` |
| `OnToolResult` | Tool returns | `{session_id, tool_name, result_preview, status}` |
| `OnMemoryWrite` | Memory is written | `{session_id, layer, key_hash, size_bytes}` |
| `OnMemorySearch` | Memory is searched | `{session_id, query, result_count, latency_ms}` |
| `OnRoutingDecision` | LLM tier is selected | `{session_id, prompt_tokens, selected_tier, model_id}` |
| `OnWebhookReceived` | Inbound webhook | `{source, event_type, payload_size}` |
| `OnWebhookSent` | Outbound webhook delivered | `{url, status_code, latency_ms}` |

Payload is always a Python dict. Use type hints in your hook for clarity:

```python
def register(registry):
    @registry.register_hook("OnToolUse")
    def on_tool(payload: dict) -> None:
        tool_name = payload.get("tool_name", "?")
        ...
```

---

## Plugin API

### `registry.register_hook(event_type: str)`

Register a function as a hook for `event_type`:

```python
def register(registry):
    @registry.register_hook("OnToolUse")
    def my_handler(payload):
        ...
```

The function is called **in-process** (same thread as Harness). Exception in your hook is **logged, not raised** — Harness continues running.

### `registry.register_tool(name: str, fn: Callable, description: str = "")`

Register a new tool that becomes available to agents:

```python
def register(registry):
    @registry.register_tool(
        name="send_slack_message",
        description="Send a Slack message to a channel",
    )
    def send_slack_message(channel: str, message: str) -> str:
        # ... call Slack API ...
        return f"Sent to {channel}"
```

### `registry.register_scope(name: str, description: str)`

Register a custom RBAC scope for your tool:

```python
def register(registry):
    registry.register_scope(
        name="slack.write",
        description="Send messages to Slack channels",
    )
```

Then in your tool, request the scope:

```python
@registry.register_tool("send_slack_message", requires_scopes=["slack.write"])
def send_slack_message(channel: str, message: str) -> str:
    ...
```

### `registry.list_plugins()`

List all loaded plugins:

```python
def register(registry):
    @registry.register_hook("OnSessionStart")
    def show_plugins(payload):
        plugins = registry.list_plugins()
        print(f"Loaded {len(plugins)} plugins: {plugins}")
```

---

## Trust Boundary (CRITICAL)

**Plugins run in the Harness Python process with FULL privileges.** A malicious plugin can:
- Read all memory contents (L1-L4)
- Modify session state
- Call any tool on behalf of the agent
- Exfiltrate data via outbound webhooks

**Harness enforces a 3-layer trust boundary:**

### Layer 1: AST pre-scan (load time)

Before `exec()`, Harness parses your plugin and **rejects imports** of:
- `harness.agents` — sub-agents internals (use hooks instead)
- `harness.server.routes` — HTTP route internals
- `harness.server.llm` — LLM router internals
- `harness.server.agents` — agent runner internals

You **can** import:
- `harness.privacy.zones` — read privacy filter config
- `harness.config` — read settings
- `harness.hooks` — use hook context utilities

### Layer 2: Whitelist (`plugins_allowed`)

Set `plugins_allowed` in config. **Empty list = NO plugins loaded**. Add plugin names to allowlist:

```yaml
plugins_allowed:
  - tool_logger       # only this plugin loads
```

### Layer 3: Subprocess sandbox (Phase 6.2+, optional)

For untrusted plugins, enable subprocess sandbox:

```yaml
plugins_sandbox: subprocess  # default: in_process
plugins_timeout: 30.0
plugins_memory_limit_mb: 256
```

Plugin runs in isolated subprocess with JSON-RPC over stdin/stdout. Has access to:
- Hook events (read-only payload)
- No access to Harness internals (3-layer isolation: `-I -S` + cwd tempdir + stripped env)

---

## Plugin Lifecycle

1. **Harness startup** → reads `plugins_dir`, scans for `.py` files
2. **AST pre-scan** → rejects plugins importing `harness.agents` / `harness.server.routes` etc.
3. **Whitelist check** → only plugins in `plugins_allowed` are loaded
4. **`register(registry)` called** → plugin registers hooks/tools/scopes
5. **Hooks fire** → on each event, registered callbacks are invoked in order
6. **Exceptions caught** → logged to `.memory/EVENTS/E-plugin-error-*.json`

---

## Example: Slack Notifier

Create `.harness/plugins/slack_notifier.py`:

```python
"""Send Slack notifications on session end."""
import os
import urllib.request
import json

PLUGIN_NAME = "slack_notifier"
PLUGIN_VERSION = "1.0.0"

WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")


def register(registry):
    if not WEBHOOK_URL:
        return  # Slack not configured, skip

    @registry.register_hook("OnSessionEnd")
    def notify_slack(payload):
        message_count = payload.get("message_count", 0)
        total_tokens = payload.get("total_tokens", 0)
        total_cost = payload.get("total_cost", 0.0)
        text = f"Session {payload['session_id']} ended: {message_count} messages, {total_tokens} tokens, ${total_cost:.4f}"
        payload_json = json.dumps({"text": text}).encode("utf-8")
        req = urllib.request.Request(
            WEBHOOK_URL,
            data=payload_json,
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
        except Exception as exc:
            print(f"[slack_notifier] Failed: {exc}", file=__import__("sys").stderr)
```

Enable:

```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/YOUR/WEBHOOK/URL"
# In config:
plugins_allowed:
  - slack_notifier
```

---

## Example: Custom Tool — Weather Lookup

Create `.harness/plugins/weather_tool.py`:

```python
"""Custom tool: get current weather for a city."""
import urllib.request
import json

PLUGIN_NAME = "weather_tool"
PLUGIN_VERSION = "1.0.0"


def register(registry):
    registry.register_scope("weather.read", "Read weather forecasts")

    @registry.register_tool(
        name="get_weather",
        description="Get current weather for a city (temperature + conditions)",
        requires_scopes=["weather.read"],
    )
    def get_weather(city: str) -> str:
        """Return current weather as JSON string."""
        # Use Open-Meteo (free, no API key needed)
        # First geocode city → lat/lon
        geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1"
        with urllib.request.urlopen(geo_url, timeout=5) as resp:
            geo = json.loads(resp.read())
        if not geo.get("results"):
            return json.dumps({"error": f"City not found: {city}"})
        lat = geo["results"][0]["latitude"]
        lon = geo["results"][0]["longitude"]
        # Then fetch weather
        wx_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current_weather=true"
        with urllib.request.urlopen(wx_url, timeout=5) as resp:
            wx = json.loads(resp.read())
        return json.dumps(wx["current_weather"])
```

Now agents can call `get_weather(city="Moscow")` and get current weather.

---

## Example: Cost Guardrail

Create `.harness/plugins/cost_guard.py`:

```python
"""Warn when session cost exceeds $1.00."""

PLUGIN_NAME = "cost_guard"
PLUGIN_VERSION = "1.0.0"
THRESHOLD = 1.00  # dollars


def register(registry):
    @registry.register_hook("OnSessionEnd")
    def check_cost(payload):
        total_cost = payload.get("total_cost", 0.0)
        if total_cost > THRESHOLD:
            session_id = payload.get("session_id")
            print(
                f"[cost_guard] WARNING: Session {session_id} cost ${total_cost:.4f} > ${THRESHOLD:.2f}",
                file=__import__("sys").stderr,
            )
            # Could also send webhook, log to file, etc.
```

---

## Troubleshooting

### Plugin not loading

Check Harness startup logs:
```
[harness] plugin_loader: scanning .harness/plugins/
[harness] plugin_loader: found 3 plugin files
[harness] plugin_loader: registered tool_logger (v0.1.0)
```

If your plugin isn't listed, check:
1. File extension `.py` (not `.pyc` or `.txt`)
2. No syntax errors — try `python .harness/plugins/your_plugin.py`
3. Not in `plugins_allowed` whitelist
4. Not importing `harness.agents` etc. (AST rejection)

### Hook not firing

Verify with `OnSessionStart` (always fires) — does your hook get called? If yes, the event registration works, the issue is specific to the event you're targeting.

### Plugin crashes Harness

Plugin exceptions are **logged, not raised** — Harness continues. Check:
- `.memory/EVENTS/E-plugin-error-*.json` for crash logs
- Harness stdout/stderr

If your plugin breaks Harness anyway, file a bug — trust boundary should prevent this.

---

## API Reference

### `PluginRegistry.register_hook(event_type: str) -> Callable`

Decorator. Returns a function that wraps the hook. Stores the hook in the registry.

```python
@registry.register_hook("OnToolUse")
def my_hook(payload):
    ...
```

### `PluginRegistry.register_tool(name: str, description: str = "", requires_scopes: list[str] = None) -> Callable`

Decorator. Registers a new tool.

```python
@registry.register_tool(
    name="my_tool",
    description="Does something useful",
    requires_scopes=["my.scope"],
)
def my_tool(arg1: str, arg2: int) -> str:
    ...
```

### `PluginRegistry.register_scope(name: str, description: str)`

Register a custom RBAC scope.

### `PluginRegistry.list_plugins() -> list[dict]`

Return list of loaded plugins:

```python
[{
    "name": "tool_logger",
    "version": "0.1.0",
    "hooks": ["OnToolUse"],
    "tools": [],
    "scopes": [],
}, ...]
```

---

## Versioning & Compatibility

- Plugin API stable since v1.28.0
- Breaking changes will be announced in CHANGELOG.md
- Plugins should declare `PLUGIN_NAME` and `PLUGIN_VERSION` constants
- Plugin registry may add fields in future versions; unknown fields are ignored

---

## Related Docs

- [Architecture: Plugin system](../.claude/agents/Solomon-Coder.md) — how Harness loads plugins
- [Trust boundary](../.architecture/plugin-trust-boundary.md) — security details
- [Hook events](hooks.md) — all available hooks with payload schemas
- [RBAC scopes](scope-api.md) — how scopes work
- [Configuration](../pyproject.toml) — all plugin-related settings

---

**Last updated:** 2026-06-20 (Solomon v1.28.0)

Создал Solomon через JV с Марком + автономно после v1.28.0 ship.
