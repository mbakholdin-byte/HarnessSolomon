# CLI Reference — Solomon Harness v1.0.0+

> Last updated: 2026-06-19, v1.0.0 final. CLI entry point: `harness` или `python -m harness`.

> `harness` (или `python -m harness`) — command-line entry point. Subcommands покрывают server lifecycle, sub-agent management, hooks inspection, observability, auth, elicitation, context, sessions, reload, webhooks DLQ.

## Synopsis

```
harness <command> [subcommand] [options]
```

## Commands overview

| Command | Описание | Phase |
|---------|----------|-------|
| `serve` | Start FastAPI server (default) | 0 |
| `agents` | Sub-agent management (list/run/jobs/split-plan) | 2 |
| `context` | Scratchpad notes + plan inspector | 3 v1.2 |
| `sessions` | Session control (manual /compact) | 3 v1.4 |
| `auth` | Token management (create/list/revoke/whoami/test) | 1.6 |
| `reload` | Force-reload hot-reloadable resources | 4.2+ v1.9 |
| `hooks` | Hook registry inspection + audit + dispatch | 4.4-4.7 |
| `observability` | Logs + metrics + health + stats + webhooks DLQ | 4.4-4.13B |
| `elicitation` | Decision history | 4.8 v1.18 |

---

## `harness serve`

Start the FastAPI server.

```bash
harness serve [--host H] [--port P]
# или просто
python -m harness
```

**Defaults:** host из `settings.host`, port из `settings.port` (8765).

---

## `harness agents`

### `agents list`

```bash
harness agents list
```

Lists builtin + project-override sub-agents с model, permissions, max_iter, tools.

### `agents run`

```bash
harness agents run <name> "<prompt>" [options]
```

**Options:**
- `--no-worktree` — run in current directory (skip worktree isolation)
- `--repo PATH` — override project root
- `--worktree-id ID` — override auto-generated worktree id
- `--background` — enqueue as MergeQueue job (prints `job_id`, exits immediately)
- `--cascade` — route through TierSelector (T1→T2→T3)
- `--pr` / `--pr-draft` / `--pr-ready` — open PR (requires `--background`)
- `--pr-target BRANCH` — target branch (default: `main`)
- `--auto-merge` — `gh pr merge --auto` (branch-protection-aware)
- `--pr-auto-merge` — shorthand для `--pr --auto-merge`
- `--auto-merge-method squash|merge|rebase`
- `--auto-merge-label LABEL`
- `--split-into N` — split diff into N stacked PRs (requires `--pr*`)
- `--split-strategy auto|files|directory|size`
- `--stack-files FILE` — newline-separated paths для split override
- `--stack-repos PATH,PATH,...` — cross-repo stacks (one repo per slice)

**Examples:**

```bash
# Quick run (sync, read-only)
harness agents run explore "Find all Python files"

# Background job с draft PR
harness agents run code "Refactor auth module" --background --pr-draft

# Stacked PRs (3 slices)
harness agents run code "Add feature X" --background --pr --split-into 3
```

### `agents jobs`

```bash
harness agents jobs [job_id] [--recent N]
```

- С `job_id`: inspect single job (status, cost, PR info, error).
- Без `job_id`: list recent N (default 20).

### `agents split-plan`

```bash
harness agents split-plan [worktree_path] [--base BRANCH] [--split-into N] [--strategy S] [--files FILE]
```

Dry-run: preview how a worktree's diff would be split into N stacked PRs. No git/gh mutations.

---

## `harness context`

Scratchpad inspector (Phase 3 v1.2.0). Reads from `data/agent-jobs.db` (no HTTP).

### `context read`

```bash
harness context read --session S [--agent A] [--level L0|L1|L2]
```

### `context write`

```bash
harness context write --session S --level L0|L1|L2 --content "..." [--tags "t1,t2"]
```

### `context plan`

```bash
harness context plan --session S [--status STATUS] [--mark-done --step-id N]
```

Plan step statuses: `pending`, `in_progress`, `done`, `blocked`.

---

## `harness sessions`

### `sessions compact`

```bash
harness sessions compact --session S [--bypass-cache] [--base-url URL]
```

Manual `/compact` via HTTP POST to running server. Server должен быть запущен (CLI не делает compact сам).

---

## `harness auth`

Token management (Phase 1.6).

### `auth create`

```bash
harness auth create --label L [--scopes "s1,s2"] [--bootstrap]
```

Mints new token. Plaintext printed to stdout ONCE. `--bootstrap` = ALL_SCOPES.

### `auth list`

```bash
harness auth list
```

Table of active (non-revoked) tokens: label, scopes, created_at, last_used_at, hash prefix. First call с `auth_required=True` and no active tokens → bootstrap-admin token auto-created.

### `auth revoke`

```bash
harness auth revoke <hash-or-label>
```

Hash (64 hex chars) или label. Refuses если label collides (use hash).

### `auth whoami`

```bash
harness auth whoami <plaintext>
```

Debug: show scopes + metadata для token.

### `auth test`

```bash
harness auth test <plaintext> [--base-url URL]
```

Smoke-test token against `/api/v1/capabilities`. Exits 0 на 200, 1 на error.

---

## `harness reload`

Force-reload hot-reloadable resources (Phase 4.2+ v1.9). No server required — local file re-parse.

```bash
harness reload [all|agents|hooks|privacy] [--project-root P] [--json]
```

- `all` (default): reload all 3 resources.
- `agents`: `.harness/agents/*.md` (project overrides).
- `hooks`: `.harness/hooks/*.json`.
- `privacy`: `.harness/privacy/*.json`.

**Exit codes:** 0 = ok, 1 = parse errors, 2 = invalid args.

---

## `harness hooks`

Hook registry inspection + audit + dispatch (Phase 4.4-4.7).

### `hooks list`

```bash
harness hooks list [--event E] [--transport T] [--enabled|--disabled] [--json] [--project-root P]
```

Lists 12 builtin + project hooks. Comma-separated filters (`--event PreToolUse,Elicitation`).

### `hooks show`

```bash
harness hooks show <hook_id> [--json] [--project-root P]
```

Full spec для one hook. `Authorization` header redacted (`Bearer ***`).

### `hooks status`

```bash
harness hooks status [--json] [--project-root P]
```

Hot-reload summary: total_specs, builtin_specs, project_specs, files_errored.

### `hooks dispatch`

```bash
harness hooks dispatch <event> [--session S] [--agent A] [--payload JSON] [--project-root P]
```

Fire hook event против global registry, print aggregate decision. Useful для shell-based testing. Event name = PascalCase (matches `EventType.value`).

**Example:**
```bash
harness hooks dispatch PreToolUse --payload '{"tool_name":"bash","arguments":{"command":"ls"}}'
# decision: allow
```

### `hooks audit`

```bash
harness hooks audit [--tail N] [--event E] [--decision allow|block|modify] \
                    [--session S] [--since ISO] [--filter REGEX] \
                    [--follow] [--batch-size N] [--resume] [--reset] \
                    [--max-bytes B] [--json] [--project-root P]
```

Read NDJSON audit log (`data/audit/hooks-YYYY-MM-DD.ndjson`, today UTC by default).

**Modes:**
- **Snapshot** (default): last N entries (default 50) с filters.
- **Live tail** (`--follow`): open at EOF, print new entries. `--batch-size N` buffers into batches. `--resume` продолжает с last byte offset (state in `settings.cli_follow_state_dir`). `--reset` игнорирует state, start from byte 0.

**Filter:** `--filter REGEX` applied via `re.search` на JSON-serialised entry (snapshot mode) или raw line (follow mode).

---

## `harness observability`

Inspect observability layer (Phase 4.4-4.13B).

### `observability log`

```bash
harness observability log [--tail N] [--event E] [--date YYYY-MM-DD] [--max-bytes B] [--json]
```

Read JSONL log (`data/logs/harness-YYYY-MM-DD.jsonl`, today UTC by default). Local — no server required.

### `observability metrics`

```bash
harness observability metrics [--base-url URL] [--filter REGEX] [--timeout-s S]
# Live tail (in-process, no HTTP):
harness observability metrics --follow [--interval-ms N] [--json] [--batch-size N]
```

- Без `--follow`: HTTP scrape `GET /metrics` (Prometheus text format). `--filter` regex на metric names.
- С `--follow`: poll in-process `PrometheusMetrics.snapshot()` every `--interval-ms`, print only changed counters/gauges. No HTTP.

### `observability health`

```bash
harness observability health [--level live|ready|deep] [--base-url URL] [--timeout-s S] [--json]
```

`GET /health/{level}`. **Exit codes:** 0=ok, 1=degraded, 2=unhealthy/HTTP-error/invalid-args.

### `observability stats`

```bash
harness observability stats [--json]
harness observability stats --diff BEFORE.json AFTER.json
```

In-process snapshot (CLI starts fresh → counters are 0 unless incremented in this process).

`--diff`: compare 2 JSON snapshots, print per-metric delta. **Exit codes:** 0=no changes, 2=any delta, 1=read/parse error. Useful для CI regression testing.

### `observability webhooks dlq` (Phase 4.13B v1.23)

```bash
# List DLQ
harness observability webhooks dlq list [--limit N] [--include-replayed] \
                                        [--base-url URL] [--token T] [--json]

# Replay entry
harness observability webhooks dlq replay <dlq_id> [--base-url URL] [--token T] [--json]
```

См. [`docs/webhooks.md`](webhooks.md).

---

## `harness elicitation`

### `elicitation history`

```bash
harness elicitation history [--session S] [--limit N] [--json] [--project-root P]
```

Read decision log from `data/agent-jobs.db` (no HTTP). Default limit 100.

---

## Global options

- `--project-root PATH` — override project root (default: CWD).
- `--json` — machine-readable output (где применимо).
- UTF-8 stdout forced на Windows (cp1251 override).

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Error (parse failure, store error, HTTP error) |
| 2 | Invalid arguments / missing required flag |
| 3 | Planner error (rare; `agents split-plan`) |

Для `observability health`: 0=ok, 1=degraded, 2=unhealthy.
Для `observability stats --diff`: 0=no changes, 2=delta found.

## Environment variables

Все settings могут быть переопределены через env vars (uppercased, prefix по секции). Ключевые:

| Variable | Default | Описание |
|----------|---------|----------|
| `MINIMAX_API_KEY` | — | MiniMax provider key |
| `ZHIPUAI_API_KEY` | — | ZhipuAI (GLM) provider key |
| `MOONSHOT_API_KEY` | — | Moonshot (Kimi) provider key |
| `AUTH_REQUIRED` | `true` | Master auth switch (`false` = open dev mode) |
| `HOT_RELOAD_ENABLED` | `true` | Hot-reload file watchers |
| `OBSERVABILITY_PROMETHEUS_ENABLED` | `false` | `/metrics` endpoint |
| `OBSERVABILITY_OTLP_ENABLED` | `false` | OTLP trace export |
| `HOOKS_AUDIT_LOG` | `false` | NDJSON audit sink |
| `HOOKS_ELICITATION_LONGPOLL_ENABLED` | `false` | Long-poll elicitation transport |
| `HOOKS_ELICITATION_SSE_ENABLED` | `false` | SSE elicitation transport |
| `HOOKS_OBSERVABILITY_ADMIN_ENABLED` | `true` | Admin JSON endpoints |
| `LEGACY_APIS_GONE_ENABLED` | `false` | 410 Gone для legacy `/api/*` |
| `OUTBOUND_WEBHOOK_TOKEN` | — | V1 signing secret |
| `OUTBOUND_WEBHOOK_TOKEN_V2` | — | V2 signing secret (after rotation) |

## См. также

- [`docs/quickstart.md`](quickstart.md) — 10-min setup
- [`docs/hooks.md`](hooks.md) — hooks framework (с triggers для `hooks dispatch`)
- [`docs/observability.md`](observability.md) — observability subsystems
- [`docs/scope-api.md`](scope-api.md) — auth scopes
- [`docs/api.md`](api.md) — REST endpoints reference
- `harness/cli.py` — argparse definitions (2341 LoC)
- `harness/cli_hooks.py`, `cli_observability.py`, `cli_follow.py`, `cli_elicitation.py` — subcommand implementations

---

**Версия документа:** v1.22.0 (2026-06-19)
