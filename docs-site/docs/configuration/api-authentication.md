---
sidebar_position: 2
title: API Authentication
description: Token-based API authentication for Harness
---

import Tabs from '@theme/Tabs';
import TabItem from '@theme/TabItem';

# API Authentication

Harness uses **scope-gated token authentication** for its REST API (`/api/v1/*`).
Tokens are opaque bearer strings; the server stores only SHA-256 hashes — plaintext
is shown exactly once at creation time.

## Overview

- **Token-based** — every authenticated request carries `Authorization: Bearer «token»`
- **Enabled by default** — `auth_required` is `True` in production; set to `False`
  for local development to run in open dev mode
- **Scope-gated** — each token carries a set of dot-separated scopes
  (e.g. `agents.read`, `memory.write`); endpoints declare which scopes they need
- **Public capabilities endpoint** — `GET /api/v1/capabilities` is always open and
  tells clients what scopes are available and which endpoints require which scopes

## Enabling and disabling authentication

The master switch is `auth_required`. All configuration layers are supported:

<Tabs>
  <TabItem value="env" label="Environment variable" default>

```bash
# Enable (default — already True in production)
export HARNESS_AUTH_REQUIRED=true

# Disable — open dev mode, no auth checks on /api/v1/*
export HARNESS_AUTH_REQUIRED=false
```

  </TabItem>
  <TabItem value="yaml" label="settings.yaml">

```yaml
auth:
  auth_required: true
```

  </TabItem>
</Tabs>

In **open dev mode** (`auth_required: false`), all `/api/v1/*` routes behave as if
the request carries a fully-scoped admin token. Legacy `/api/*` routes (sessions,
chat, models, health) are always open regardless of this setting.

## Token management

All token operations go through the `harness auth` CLI subcommand. Tokens are
stored in a SQLite database (`data/harness-scope.db` by default); only SHA-256
hashes are persisted — the plaintext is never written to disk.

### Creating a token

```bash
harness auth create --label "my-opencode-client" --scopes "agents.read,memory.read"
```

Output:
```
token=dGhpcyBpcyBhIHRva2VuIHBsYWludGV4dCBleGFtcGxl...
label=my-opencode-client
scopes=agents.read, memory.read
WARNING: this is the only time the plaintext will be shown. Store it in a password manager or env var.
```

Save the `token=` line. It will never be displayed again.

**Bootstrap admin token** — on first run with `auth_required: true`, Harness
auto-creates a token with `*` (all scopes) if no active tokens exist:

```bash
harness auth create --label "bootstrap-admin" --bootstrap
```

### Listing tokens

```bash
harness auth list
```

Shows a table of active (non-revoked) tokens with their labels, scopes,
creation time, last-used time, and a short hash prefix.

### Revoking a token

Revoke by hash (precise) or by label (convenient):

```bash
# By hash — 64-char hex string
harness auth revoke a1b2c3d4e5f6...

# By label — if exactly one active token matches
harness auth revoke my-opencode-client
```

### Inspecting a token

```bash
harness auth whoami «your-plaintext-token»
```

Shows the label, scopes, hash, and timestamps without calling the server.

### Testing a token against the server

```bash
harness auth test «your-plaintext-token»
```

Calls `GET /api/v1/capabilities` on the local server with the token. Exits `0`
on HTTP 200, `1` on any error.

## Using tokens in API requests

Pass the token in the `Authorization` header using the Bearer scheme:

<Tabs>
  <TabItem value="curl" label="curl" default>

```bash
curl -H "Authorization: Bearer $HARNESS_TOKEN" \
     http://127.0.0.1:8765/api/v1/agents/jobs
```

  </TabItem>
  <TabItem value="httpie" label="HTTPie">

```bash
http GET http://127.0.0.1:8765/api/v1/agents/jobs \
     Authorization:"Bearer $HARNESS_TOKEN"
```

  </TabItem>
  <TabItem value="python" label="Python">

```python
import os
import urllib.request

req = urllib.request.Request(
    "http://127.0.0.1:8765/api/v1/agents/jobs",
    headers={"Authorization": f"Bearer {os.environ['HARNESS_TOKEN']}"},
)
with urllib.request.urlopen(req) as resp:
    print(resp.read().decode())
```

  </TabItem>
</Tabs>

### Error responses

| Status | Condition | Detail |
|--------|-----------|--------|
| `401` | Missing or malformed `Authorization` header | `"missing Authorization header"` / `"invalid Authorization header"` |
| `401` | Token not found or revoked | `"invalid or revoked token"` (deliberately vague — no enumeration) |
| `403` | Valid token, insufficient scopes | `"missing required scope: agents.read (have: memory.read)"` |

## Scopes

Scopes are dot-separated, lowercase strings. Each token carries a set of scopes;
endpoints declare their required scopes. The match is **logical OR** — if the
token has **any** of the required scopes, access is granted.

### Available scopes

| Scope | Description |
|-------|-------------|
| `agents.read` | Read sub-agent jobs and queue stats |
| `agents.write` | Enqueue / cancel sub-agent jobs |
| `agents.pr` | Open and merge GitHub PRs via the merge queue |
| `memory.read` | Search the 4-layer memory system |
| `memory.write` | Write notes to the 4-layer memory system |
| `sessions.read` | Read session metadata |
| `sessions.write` | Force-compact a session's context |
| `observability.read` | Read metrics, health checks, audit logs |
| `elicitation.read` | Subscribe to Elicitation questions via SSE |
| `elicitation.write` | Answer Elicitation / confirmation questions |
| `webhooks.admin` | Administer outbound webhooks and the DLQ |
| `privacy.read` | Read privacy zone rules |
| `privacy.write` | Create / update / delete privacy zone rules |
| `hooks.admin` | Administer hooks |
| `plugins.admin` | Administer plugins |
| `plugins.read` | Browse the plugin marketplace |

### Discovery at runtime

Any client (no token needed) can query the full scope set:

```bash
curl http://127.0.0.1:8765/api/v1/capabilities
```

Response includes `scopes_available` (every scope with its description),
`endpoints` (every route with its required scopes), and the `auth_required` flag.

### Example: creating a read-only monitoring token

```bash
harness auth create \
  --label "monitoring-readonly" \
  --scopes "observability.read,sessions.read,agents.read"
```

### Example: creating a CI/CD integration token

```bash
harness auth create \
  --label "github-actions-bot" \
  --scopes "agents.read,agents.write,agents.pr,sessions.read"
```

## Configuration reference

All auth-related settings:

| Setting | Type | Default | Description |
|---------|------|---------|-------------|
| `auth_required` | `bool` | `true` | Master switch — `false` disables all auth checks |
| `auth_db_path` | `Path` | `data/harness-scope.db` | SQLite path for the token store |
| `auth_token_bytes` | `int` | `32` | Random bytes per token (32 B = 256-bit = 43 URL-safe chars) |
| `auth_default_scopes` | `str` | `""` | Default scopes for `harness auth create` without `--scopes` |

Override via environment variables:

```bash
export HARNESS_AUTH_REQUIRED=true
export HARNESS_AUTH_DB_PATH=/var/lib/harness/tokens.db
export HARNESS_AUTH_TOKEN_BYTES=48
export HARNESS_AUTH_DEFAULT_SCOPES="agents.read,sessions.read"
```

For the full schema, see the [Configuration Reference](/configuration/reference#auth).
