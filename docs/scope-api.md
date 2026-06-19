# Scope-gated API — Solomon Harness Phase 1.6

The scope-gated API is the *declarative* security surface for the
`/api/v1/*` routes. It is a port of the pattern from Odysseus
(pewdiepie-archdaemon/odysseus) to a minimal FastAPI / SQLite
implementation. The key idea: the **server** publishes the list of
scopes it understands and which routes need which; the **client**
decides which token to use; the **server** enforces the contract
on every request.

## Quickstart

```bash
# 1. Start the server (with auth required — default)
harness serve &

# 2. First call triggers bootstrap. Save the printed token.
harness auth list
# [harness] bootstrap-admin token created (label=bootstrap-admin).
# [harness] SAVE THIS — it will not be shown again:
#   YsVQ3gfLHK_GYoe8kUvKVZh4B2GcUFtcxvwkN0OM9JM

# 3. Use it.
curl -sH "Authorization: Bearer YsVQ3gfLHK_GYoe8kUvKVZh4B2GcUFtcxvwkN0OM9JM" \
     http://localhost:8765/api/v1/agents/jobs?recent=5 | jq

# 4. Mint scoped tokens for downstream callers.
harness auth create --label "opencode-mcp" --scopes "agents.read,memory.read"
# token=AbC...123
# label=opencode-mcp
# scopes=agents.read,memory.read
# WARNING: this is the only time the plaintext will be shown. ...

# 5. Revoke when no longer needed.
harness auth revoke opencode-mcp
# revoked: opencode-mcp (a466e074c516…)

# 6. Inspect what tokens exist.
harness auth list
# label            scopes                                created_at             last_used_at          hash
# bootstrap-admin  *                                     2026-06-14T...         (never)               a466e074c516...
```

## Scopes reference

The closed set has **10 scopes** (v1.23.0). Defined in `harness/server/auth/scopes.py`; new scopes added by extending the `Scope` enum and providing a description in `SCOPE_DESCRIPTIONS`. The capabilities endpoint (`GET /api/v1/capabilities`) reflects the closed set live.

| Scope | Wire value | Routes | Phase |
|-------|-----------|--------|-------|
| `AGENTS_READ` | `agents.read` | `GET /api/v1/agents/jobs/{id}`, `GET /api/v1/agents/jobs`, `GET /api/v1/agents/health` | 1.6 |
| `AGENTS_WRITE` | `agents.write` | `POST /api/v1/agents/jobs` (when `pr_mode="off"`) | 1.6 |
| `AGENTS_PR` | `agents.pr` | `POST /api/v1/agents/jobs` (when `pr_mode != "off"`, compound with `agents.write`) | 2.3+ |
| `MEMORY_READ` | `memory.read` | `GET /api/v1/memory/search`, `GET /api/v1/memory/stats` | 1.6 |
| `MEMORY_WRITE` | `memory.write` | `POST /api/v1/memory/notes` | 1.6 |
| `SESSIONS_READ` | `sessions.read` | `GET /api/v1/sessions?recent=N` | 1.6 |
| `SESSIONS_WRITE` | `sessions.write` | `POST /api/v1/sessions/{id}/compact` (manual /compact, Phase 3 v1.4.0) | 3 v1.4.0 |
| `OBSERVABILITY_READ` | `observability.read` | `GET /api/v1/observability/{metrics,health/deep,audit/recent,webhooks/dlq}` (Phase 4.11 v1.21.0) | 4.11 |
| `ELICITATION_READ` | `elicitation.read` | `GET /api/v1/elicitation/sse` (SSE transport, Phase 4.11 v1.21.0) | 4.11 |
| `WEBHOOK_ADMIN` | `webhooks.admin` | `POST /api/v1/webhooks/enable`, `POST /api/v1/observability/webhooks/dlq/{id}/replay` (Phase 4.13B v1.23.0) | 4.13B |

**Matching semantics:** `has_scope(token_scopes, required)` uses **ANY** match (logical OR). A token with scopes `{memory.read, sessions.read}` can call any endpoint whose required set intersects. Empty `required` is treated as "no requirement" (returns True).

## Auth model

### `/api/v1/*` — scope-gated

Every route under `/api/v1/*` requires a `Bearer` token in the
`Authorization` header, except `/api/v1/capabilities` which is
public so clients can self-discover the auth surface.

- **Missing header** → `401 missing Authorization header`
- **Malformed header** → `401 invalid Authorization header (expected 'Bearer <token>')`
- **Unknown / revoked token** → `401 invalid or revoked token` (same message for both — no token-hash enumeration via status code)
- **Valid token, missing scope** → `403 missing required scope: X (have: A, B)`

### `/api/*` — open (Phase 1.6 contract)

Legacy routes stay open in Phase 1.6: `/api/sessions`, `/api/chat/ws`,
`/api/models`, `/api/health`. They will move to `/api/v1/*` in
Phase 4+ with deprecation headers. In Phase 1.6 they are
**always** reachable without a token, regardless of
`settings.auth_required`.

### `auth_required=False` — open dev mode

The master switch `settings.auth_required` (mirrored on
`app.state.auth_required`) disables scope checks for `/api/v1/*`
when set to `False`. This is the dev / test mode and the
recommended way to run a local harness without worrying about
tokens. In production, leave the default (`True`).

```bash
# Dev mode: skip auth, just run the server.
AUTH_REQUIRED=false harness serve

# Test suite: tests/conftest.py sets `auth_required=False` per
# isolated_settings fixture, so the existing Phase 0-2.2 tests
# don't need to be updated.
```

## Token lifecycle

### `harness auth create`

```
harness auth create --label LABEL [--scopes SCOPES] [--bootstrap]
```

Mints a new token. The plaintext is **printed to stdout once** and
never persisted (only the SHA-256 hash is stored). The `--bootstrap`
flag is a shortcut for `--scopes "*"` (the full ALL_SCOPES set).

If `auth_required=True` and no active token exists, the implicit
bootstrap path will create an `bootstrap-admin` token on the next
`auth list` / `whoami` / `test` invocation. Bootstrap never runs
on `create` / `revoke` — those are write commands, and bootstrap
could surprise the user.

### `harness auth list`

```
harness auth list
```

Prints a table of active (non-revoked) tokens with their label,
scopes, created/last-used timestamps, and the first 12 chars of
the token hash. Plaintext is **never** in this output.

### `harness auth revoke`

```
harness auth revoke <hash-or-label>
```

Marks a token as revoked. Accepts either a 64-char hash (the
programmatic path, no ambiguity) or a label (one-off operator
use; if multiple tokens share the label, the command refuses
and asks for the hash).

### `harness auth whoami`

```
harness auth whoami <plaintext>
```

Debug command: shows the scopes and metadata for a token. Returns
exit 1 if the token is unknown or revoked.

### `harness auth test`

```
harness auth test <plaintext> [--base-url URL]
```

Smoke-tests a token against the local server. Calls
`GET /api/v1/capabilities` with the supplied token and prints
`ok: <url> -> 200` on success, or a clear error if the server
is unreachable / returns non-200. Useful for CI smoke tests.

## Capabilities discovery

```bash
curl -s http://localhost:8765/api/v1/capabilities | jq
```

Returns the server's self-description. **Always public** (no auth
required) so a client with no token can still learn the auth
surface. Shape (v1.23.0 example):

```json
{
  "server_version": "1.21.0",
  "auth_required": true,
  "scopes_available": [
    {"name": "agents.read", "description": "Read sub-agent jobs and queue stats (GET /api/v1/agents/jobs*)"},
    {"name": "agents.write", "description": "Enqueue / cancel sub-agent jobs (POST /api/v1/agents/jobs)"},
    {"name": "agents.pr", "description": "Open and merge GitHub PRs via the merge queue (Phase 2.3+)"},
    {"name": "memory.read", "description": "Search the 4-layer memory system (GET /api/v1/memory/*)"},
    {"name": "memory.write", "description": "Dual-write notes to the 4-layer memory system (POST /api/v1/memory/notes)"},
    {"name": "sessions.read", "description": "Read session metadata (GET /api/v1/sessions)"},
    {"name": "sessions.write", "description": "Force-compact a session's context (POST /api/v1/sessions/{id}/compact, Phase 3 v1.4.0)"},
    {"name": "observability.read", "description": "Read admin observability endpoints (metrics/health/audit, Phase 4.11 v1.21.0)"},
    {"name": "elicitation.read", "description": "Subscribe to Elicitation questions via SSE transport (Phase 4.11 v1.21.0)"},
    {"name": "webhooks.admin", "description": "Administer outbound webhooks: re-enable disabled URLs, list/replay the DLQ (Phase 4.13B v1.23.0)"}
  ],
  "endpoints": [
    {"method": "GET", "path": "/api/v1/agents/jobs", "scopes": ["agents.read"]},
    {"method": "POST", "path": "/api/v1/agents/jobs", "scopes": ["agents.write", "agents.pr"]},
    {"method": "GET", "path": "/api/v1/observability/metrics", "scopes": ["observability.read"]},
    {"method": "POST", "path": "/api/v1/webhooks/enable", "scopes": ["webhooks.admin"]},
    "..."
  ]
}
```

The `endpoints` list is built live from the mounted FastAPI routes
via introspection in `harness/server/auth/route_registry.py`. The
introspection looks for the `_required_scopes` marker attribute on
the dep callable set by `require_scope()`. Adding a new
scope-gated route is automatic — no manual manifest update.

## Compound scope checks (Phase 1.6)

Some routes require **two** scopes depending on the request body.
The current example is `POST /api/v1/agents/jobs`:

- `pr_mode="off"` → only `agents.write` is required
- `pr_mode="draft" | "ready"` → `agents.write` **AND** `agents.pr`

The compound check is encoded in the route handler body (not
in a single dependency) because the pr_scope requirement is
conditional on the request payload. The error response always
includes the missing scope and the scopes the token has.

## HTTP status code conventions

| Code | When |
|------|------|
| `200` | Success |
| `201` | Resource created (POST `/api/v1/memory/notes`, POST `/api/v1/agents/jobs`) |
| `400` | (reserved — not used in Phase 1.6) |
| `401` | Auth missing / malformed / invalid / revoked. Always `WWW-Authenticate: Bearer` |
| `403` | Auth valid but missing the required scope(s) |
| `404` | Resource not found (job_id, session_id) |
| `422` | Pydantic validation failed (missing field, wrong type, out-of-range) |
| `500` | (reserved — caught by FastAPI's default handler) |
| `503` | Service unavailable (lifespan init failed: no `job_store`, no `token_store`, no `merge_queue`) |

## Trust boundary

The auth package (`harness/server/auth/`) is a **leaf** in the
dependency graph. It imports from stdlib + pydantic only. Routes
under `harness/server/routes/` import from `harness/server/auth/`
but **never** the other way around. The `harness/agents/`
package does not import from `harness/server/auth/` either — the
boundary is one-way.

```bash
# Verify trust boundary preservation:
grep -rn "from harness.server" harness/agents/ | grep -v ".agent."
# (should be empty — agent/* imports are pre-existing Phase 2.0
# pattern, not a Phase 1.6 violation)
```

## Troubleshooting

### "401 invalid or revoked token" but I'm sure the token is right

1. Check `echo $TOKEN | head -c 12` — make sure no shell expansion
   ate part of it.
2. `harness auth whoami "$TOKEN"` to confirm the token is
   recognised by the local store.
3. If the token was created on a different data dir, the hashes
   won't match. Check `$AUTH_DB_PATH` on both sides.

### "403 missing required scope: X (have: A, B)"

The token doesn't have the required scope. Mint a new one with
`harness auth create --scopes "A,B,X"` (or use the bootstrap
admin token, which has all scopes).

### "503 MergeQueue not initialised" on `POST /api/v1/agents/jobs`

The lifespan handler couldn't construct the `MergeQueue` (usually
because no LLM API keys are set). Set the relevant env vars
(`MINIMAX_API_KEY` / `ZHIPUAI_API_KEY` / `MOONSHOT_API_KEY`) and
restart, or accept the 503 and use the read-only routes
(`/api/v1/agents/jobs` GET) for now.

### "503 TokenStore not initialised"

The lifespan handler failed. Check the server startup log
(`[harness] token_store: <path> (auth_required=True)` should
appear). If it doesn't, the auth DB path is unwritable.

### I want to start over — nuke all tokens

```bash
rm -f data/harness-scope.db
harness auth list  # bootstraps a new admin token
```

The next `harness auth list` (or any read-only command) will
bootstrap a fresh `bootstrap-admin` token. **Phase 1.6 has no
"delete all tokens" command** — manual file removal is the
escape hatch.

### How do I run the server without auth (dev mode)?

```bash
AUTH_REQUIRED=false harness serve
```

This sets `app.state.auth_required = False`; all `/api/v1/*`
routes skip the scope check. Tokens are still created (the
token store is always initialised) but the route layer
ignores them.

### Tokens are created but the server says "401" anyway

Make sure `auth_required=True` (the default) and that
`Authorization: Bearer <token>` is sent verbatim — Bash
substitution can drop underscores. `curl -v` shows the
negotiated request.

## Out of scope (future phases)

- **Token rotation** through the API (manual via CLI today)
- **Token expiry** (no TTL today — only manual revoke)
- **Rate limiting** per token (note: hook rate limiter exists, but per-token API rate limit does not)
- **OAuth / OIDC** integration
- **Per-endpoint scopes** (e.g. `agents.jobs.read` vs `agents.jobs.write`)
- **Web UI** for token management
- **WebSocket auth** (`/api/v1/chat/ws` with Bearer in query)
