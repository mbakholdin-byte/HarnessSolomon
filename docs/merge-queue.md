# Merge queue — Solomon Harness Phase 2.2

The merge queue orchestrates the full sub-agent workflow:

```
                ┌────────────────────────────────────────────────────────────┐
                │                       enqueue                              │
                │  code → review → verify → (PR open → CI → merge) | ff-merge│
                └────────────────────────────────────────────────────────────┘
```

Phase 2.0 introduced the in-process `git merge --ff-only` workflow.
Phase 2.1 added persistent background mode and memory namespacing.
**Phase 2.2** (this document) adds real GitHub PR integration and
parallel cross-repo queueing.

If `pr_mode="off"` (the default), behaviour is identical to Phase 2.0/2.1.

## CLI quickstart

### Backward-compat (no PR)

```bash
# Async / background: open the harness/MiniMax-M2.7 model, code → review → ff-merge.
harness agents run code "add a docstring" --background
# job_id=8a3f9b2c1d4e5f6a
#   status: use `harness agents jobs 8a3f9b2c1d4e5f6a` to poll

# Poll the result.
harness agents jobs 8a3f9b2c1d4e5f6a
# job_id=8a3f9b2c1d4e5f6a
#   worktree_id : cli-4155
#   status      : merged
#   model       : MiniMax-M2.7
#   cost        : $0.0023
#   ...
#   pr_mode     : off          # NEW in 2.2

# List recent.
harness agents jobs --recent 5
# job_id              status       model        cost     worktree_id  pr_mode  started_at
# ------------------------------------------------------------------------------------------
# 8a3f9b2c1d4e5f6a    merged       MiniMax-M2.7 $0.0023  cli-4155     off      2026-06-14T12:31:29
```

### With GitHub PR

```bash
# Open a draft PR (shorthand: --pr == --pr-draft).
harness agents run code "fix the typo" --pr --background
# job_id=...
#   pr_mode: draft (target=main)

# Open a ready-for-review PR.
harness agents run code "fix the typo" --pr-ready --pr-target main --background

# Targeting a different branch.
harness agents run code "fix the typo" --pr --pr-target develop --background
```

### What happens when `gh` is unavailable

If `pr_strategy="auto"` (default) and `gh` is missing or not
authenticated, the queue logs a warning and falls back to a local
`git merge --ff-only`. The job is still recorded as `merged` with
the `pr_mode` set and an audit-trail `pr_skipped` event:

```bash
unset GITHUB_TOKEN
harness agents run code "echo hi" --pr --background
harness agents jobs <job_id>
# ...
#   pr_mode     : draft
#   (no pr_url / pr_number — local fallback ran)
#   error       : (none)
```

If `pr_strategy="strict"`, the job is marked `failed` with an
explicit "gh unavailable" error and the worktree is preserved for
human inspection:

```bash
export SUBAGENT_PR_STRATEGY=strict
unset GITHUB_TOKEN
harness agents run code "echo hi" --pr --background
harness agents jobs <job_id>
# ...
#   status      : failed
#   error       : gh unavailable: gh CLI not found in PATH (hint: Install from ...)
```

## Settings

| Setting | Default | Notes |
|---------|---------|-------|
| `SUBAGENT_T1_MODEL` | `qwen3:8b` | Phase 2.1: T1 cascade model |
| `SUBAGENT_T2_MODEL` | `glm-4.7` | Phase 2.1: T2 cascade model |
| `SUBAGENT_CONFIDENCE_HIGH` | `0.85` | Phase 2.1: T1 threshold |
| `SUBAGENT_CONFIDENCE_LOW` | `0.55` | Phase 2.1: T2 threshold |
| `GITHUB_TOKEN_ENV` | `GITHUB_TOKEN` | Phase 2.2: env var holding the token |
| `PR_DEFAULT_TARGET_BRANCH` | `main` | Phase 2.2: target branch for new PRs |
| `PR_POLL_INTERVAL_S` | `15.0` | Phase 2.2: seconds between `gh pr view` polls |
| `PR_WAIT_TIMEOUT_S` | `300.0` | Phase 2.2: max wait for CI + review |
| `PR_STRATEGY` | `auto` | Phase 2.2: `auto`/`strict`/`off` |

## Job status reference (13 statuses)

Phase 2.0+2.1 introduced 8 statuses; Phase 2.2 adds 5 PR-phase
statuses. `recover_running()` treats all 12 non-`merged`/`failed`/
`timeout`/`cancelled` statuses as in-flight.

| Status | Phase | Meaning |
|--------|-------|---------|
| `queued` | 2.0 | Created, not yet running |
| `running_code` | 2.0 | Code agent in progress |
| `running_review` | 2.0 | Review agent in progress |
| `verifying` | 2.0 | Adversarial verify in progress |
| `pr_creating` | 2.2 | `gh pr create` in progress |
| `pr_open` | 2.2 | PR opened, awaiting CI / review |
| `pr_waiting_checks` | 2.2 | Polling `gh pr view` for CI checks |
| `pr_waiting_review` | 2.2 | CI green, waiting for human approval |
| `merging_pr` | 2.2 | `gh pr merge` in progress |
| `merged` | 2.0 | Local ff-merge OR `gh pr merge` succeeded |
| `failed` | 2.0 | Any step failed (see `error` column) |
| `timeout` | 2.0 | `subagent_timeout_s` exceeded |
| `cancelled` | 2.1 | `recover_running()` after process restart, or explicit cancel |

## Per-repo locks

Two jobs targeting **different** repos run in parallel. Two jobs
targeting the **same** repo serialise (because git worktree + git
merge are not safe to run concurrently in one repo).

```python
from harness.agents.merge_queue import MergeJob, MergeQueue
from harness.agents.spec import AgentSpec

queue = MergeQueue(runner, verifier)  # singleton, per-process

# Same repo: serialised.
job1 = MergeJob(code_spec=spec, review_spec=spec, task="...", worktree_id="wt-1")
job2 = MergeJob(code_spec=spec, review_spec=spec, task="...", worktree_id="wt-2")
await queue.enqueue_async(job1)  # runs first
await queue.enqueue_async(job2)  # runs after job1

# Different repos: parallel.
job_a = MergeJob(..., worktree_id="wt-a", repo_override=Path("/abs/repo-a"))
job_b = MergeJob(..., worktree_id="wt-b", repo_override=Path("/abs/repo-b"))
await asyncio.gather(queue.enqueue_async(job_a), queue.enqueue_async(job_b))
#   ^ these two run concurrently
```

The registry's internal state is visible at
`queue._locks.stats()` → `{repo_path: queue_depth}`.

## HTTP API

The lifespan handler instantiates a `JobStore` and a `MergeQueue`
singleton on app startup. They're exposed at:

| Route | Description |
|-------|-------------|
| `GET /api/v1/agents/jobs/{job_id}` | Fetch one job by id (404 if unknown) |
| `GET /api/v1/agents/jobs?recent=N` | List the N most recent jobs (default 20) |
| `GET /api/v1/agents/health` | Per-repo lock stats + recent job count |

Example:

```bash
# Start the server.
harness serve &

# Enqueue a job (returns a job_id).
JID=$(curl -sX POST http://localhost:8765/api/v1/agents/jobs/...)

# Poll.
curl -s http://localhost:8765/api/v1/agents/jobs/$JID | jq
# {
#   "id": "8a3f9b2c1d4e5f6a",
#   "status": "pr_open",
#   "pr_mode": "draft",
#   "pr_url": "https://github.com/owner/repo/pull/42",
#   "pr_number": 42,
#   "target_branch": "main",
#   ...
# }

# List recent.
curl -s 'http://localhost:8765/api/v1/agents/jobs?recent=5' | jq

# Health.
curl -s http://localhost:8765/api/v1/agents/health | jq
# { "queue_locks": { "/abs/repo-a": 0, "/abs/repo-b": 0 }, "job_store_path": "..." }
```

If the merge queue can't be constructed at startup (e.g. no LLM
API keys in dev), the routes return `503` with a descriptive
error. The rest of the server (sessions, chat, health) is
unaffected.

## `gh` auth troubleshooting

The queue calls `gh auth status` at the start of every PR operation.
Common failure modes:

1. **`gh: command not found`** — install via
   `winget install GitHub.cli` (Windows) or
   `brew install gh` (macOS) or
   `apt install gh` (Debian/Ubuntu).
2. **`gh is installed but not authenticated`** — run
   `gh auth login` (interactive) or set
   `$GITHUB_TOKEN` to a token with `repo` scope.
3. **`gh auth status failed: HTTP 401`** — token expired or wrong
   scope. Refresh via `gh auth login --scopes repo`.
4. **`Pull Request is not mergeable`** — branch protection requires
   additional reviewers or a status check that hasn't passed. The
   job is marked `failed` with the full `gh` error in `error`.

For local development without `gh`, the default `pr_strategy="auto"`
gracefully falls back to a local `git merge --ff-only` and emits a
`pr_skipped` event for the audit log.

## Out of scope (Phase 2.4+)

The following are deliberately not in Phase 2.2/2.3:

- **Webhook receiver** for inbound `pull_request` / `check_run` events.
  Phase 2.2 polls only. → **ЗАКРЫТО Phase 2.3 v0.7.0**
- **Auto-merge labels** (branch protection + `gh pr merge --auto`).
  → **ЗАКРЫТО Phase 2.3 v0.7.0**
- **PR review templating** (CODEOWNERS-aware reviewers, issue-link
  auto-resolution). → **ЗАКРЫТО Phase 2.4 v0.8.0**
- **Multi-PR-per-job / stacked PRs**. → **ЗАКРЫТО Phase 2.4 v0.8.0**
- **`pull_request_review.approved` short-circuit**.
  → **ЗАКРЫТО Phase 2.4 v0.8.0**
- **Multi-tenant** `gh` config (single global `$GITHUB_TOKEN`).
- **Rich PR UI** in the Web frontend (clickable `pr_url`, status
  badges).

## Webhooks (Phase 2.3 v0.7.0)

Inbound GitHub webhook receiver (`POST /api/v1/agents/webhooks/github`)
для real-time обновлений статуса job'а вместо polling + branch-protection-
aware auto-merge.

### Зачем

Phase 2.2 polls `gh pr view` каждые 15с, максимум 5 минут
(`pr_wait_timeout_s`). При медленном CI (5-20 мин) → таймаут. При
долгом review (часы-дни) → polling бессмысленен. Webhook решает обе
проблемы:

- **Real-time updates** — GitHub шлёт нам `check_run` сразу после CI
  completion; мы обновляем `JobStore` мгновенно (без polling).
- **Auto-merge** — Phase 2.2 вызывает `gh pr merge` сразу после
  green CI. Если branch protection требует approval, merge не
  происходит. `gh pr merge --auto` (Phase 2.3) включает auto-merge
  и ждёт branch protection conditions.

### Setup

1. **Сгенерируйте shared secret** (32+ символов):
   ```bash
   openssl rand -hex 32
   # → например: 5a4f...  (64 hex chars)
   ```

2. **Установите в env**:
   ```bash
   export HARNESS_WEBHOOK_SECRET="5a4f..."
   # и опционально:
   export AUTO_MERGE_METHOD="squash"  # squash | merge | rebase
   export AUTO_MERGE_LABEL="harness-auto-merge"
   ```

3. **Настройте GitHub webhook** (Settings → Webhooks → Add webhook):
   - **Payload URL:** `https://your-host/api/v1/agents/webhooks/github`
   - **Content type:** `application/json`
   - **Secret:** тот же `HARNESS_WEBHOOK_SECRET`
   - **Events:** "Let me select individual events" → отметьте
     `Pull requests`, `Check runs`, `Pull request reviews`
   - **Active:** ✓

4. **Restart harness server** (для подхвата нового secret).

### Event → status mapping

| Event | Action / State | Effect |
|-------|----------------|--------|
| `pull_request` | `closed` + `merged=true` | Job (matched by `pr_number`) → `merged` |
| `check_run` | `conclusion="success"` | No-op (polling loop подхватит на следующей итерации) |
| `check_run` | `conclusion="failure"` | Job → `failed` ("PR CI failed") |
| `pull_request_review` | `state="changes_requested"` | Job → `failed` ("PR review requested changes") |
| `pull_request_review` | `state="approved"` | No-op (Phase 2.4 review flow) |
| (любой другой) | — | 200 + logged + ignored |

### Безопасность (HMAC)

- GitHub шлёт `X-Hub-Signature-256: sha256=<hmac>` в каждом webhook.
- Harness проверяет HMAC-SHA256 с shared secret
  (`settings.webhook_secret`) используя `hmac.compare_digest`
  (timing-safe).
- Bad signature → 401. Missing signature → 401. Empty secret (webhooks
  disabled) → 503.

### Idempotency

GitHub может redeliverить webhook (e.g. server was down 5 min). Harness
гарантирует идемпотентность через `UNIQUE(delivery_id)` constraint в
`webhook_events` таблице. Redelivery → 200 + `{"processed": false,
"detail": "duplicate delivery_id (already processed)"}`.

### CLI: --pr-auto-merge

```bash
# Open draft PR + enable auto-merge (ждёт branch protection).
harness agents run code "fix typo" --pr-auto-merge --background

# Open ready-for-review PR + auto-merge с rebase method.
harness agents run code "fix typo" --pr-ready --auto-merge \
  --auto-merge-method rebase --background
```

Job status flow с `--pr-auto-merge`:
```
verifying → pr_creating → pr_open → pr_waiting_checks
          → pr_auto_merge_enabled (ждём webhook)
          → merged (через inbound pull_request webhook)
```

Fallback: если `gh pr merge --auto` fails (branch protection не
настроена для этой ветки) → очередь сразу вызывает `gh pr merge`
(Phase 2.2 behaviour). Job заканчивается как `merged` без
`pr_auto_merge_enabled`.

### Тестирование webhooks локально

Используйте `ngrok` или `smee.io` для туннелирования GitHub → local:

```bash
# Terminal 1: harness server. Set MY_HMAC_KEY in the GitHub
# repo's webhook configuration first; pass the same value
# here.
export MY_HMAC_KEY=...   # replace ... with the real key
harness serve

# Terminal 2: tunnel
ngrok http 8765
# → https://abc123.ngrok.io

# Terminal 3: simulate the webhook delivery (same key).
export MY_HMAC_KEY=...   # replace ... with the real key
BODY='{"action":"closed","number":42,"pull_request":{"html_url":"https://x","head":{"sha":"h"},"state":"closed","merged":true}}'
SIG=$(echo -n "$BODY" | openssl dgst -sha256 -hmac "$MY_HMAC_KEY" | sed 's/^.*= //')
curl -X POST "https://abc123.ngrok.io/api/v1/agents/webhooks/github" \
  -H "X-Hub-Signature-256: sha256=$SIG" \
  -H "X-GitHub-Event: pull_request" \
  -H "X-GitHub-Delivery: "test-1" \
  -H "Content-Type: application/json" \
  --data "$BODY"
# → 200 {"delivery_id": "test-1", "event_type": "pull_request", "processed": true}
```

## Stacked PRs (Phase 2.4 v0.8.0)

One job can now spawn N dependent PRs (a "stack"). PR-B's
`base_branch` is PR-A's branch, so PR-B is automatically rebased
onto PR-A when PR-A merges. This is the GitHub stacked-PR
convention.

### Зачем

A single large task often doesn't fit into one reviewable PR. You
want to split the work into:

- Slice 1: core logic
- Slice 2: tests
- Slice 3: docs / changelog

…each small enough to review, each mergeable independently. Without
stacking, the alternative is one huge PR (slow review) or three
separate jobs with manual sequencing (error-prone).

### Quick start

```bash
# Preview the split (no git mutations, no gh)
harness agents split-plan .harness/worktrees/wt-1 --split-into 3
# → plan: 3 slice(s) via 'auto' strategy
# → slice 1/3: harness/wt-1/step-0 — src/core.py, src/utils.py
# → slice 2/3: harness/wt-1/step-1 — tests/test_core.py
# → slice 3/3: harness/wt-1/step-2 — docs/refactor.md

# Enqueue a stacked run (background required)
harness agents run code "refactor X" --split-into 3 --pr --background
# → job_id=... stack_id=abc123
# → 3 child PRs created, each stacked on the previous

# Inspect the stack
curl http://localhost:8765/api/v1/agents/stacks/abc123
# → {"stack_id":"abc123","parent":{...},"children":[{...},{...},{...}]}
```

### Strategies (4)

| Strategy | Grouping | When to use |
|----------|----------|-------------|
| `auto` (default) | If diff ≤ `max_files_per_slice`, single slice; else `directory` | Most tasks; no need to think about it |
| `files` | Round-robin, ≤ `max_files_per_slice` per slice | Pure size balancing, ignores directory boundaries |
| `directory` | Group by top-level directory prefix (`src/`, `tests/`, `docs/`) | Most "natural" split for code/test/docs layouts |
| `size` | Balance by LOC (greedy LPT) | Even workload, expensive (needs `git diff --shortstat` per file) |

Override per-run: `--split-strategy files`. Settings:
`pr_split_strategy`, `pr_split_max_files_per_slice`,
`pr_split_min_slices`, `pr_split_max_slices`.

### File-list override (CI use case)

```bash
# Generate file list from main
git diff --name-only main > /tmp/stack.txt

# Enqueue with explicit list (planner groups these only)
harness agents run code "..." --stack-files /tmp/stack.txt \
  --split-into 3 --pr --background
```

### PR body templating

`harness/agents/templates/pr_body.md` (default) substitutes
7 placeholders: `{task}`, `{head_branch}`, `{base_branch}`,
`{stack_line}`, `{issue_lines}`, `{reviewer_lines}`,
`{test_summary}`. Override with `settings.pr_template_path`.

Issue numbers are auto-extracted from the task text via
`pr_issue_link_re` (default `r"#(\d+)"`). A task like
`"fix #123, refs #456"` renders the body with
`Closes #123` and `Refs #456` lines.

### Recovery semantics

- **Process restart** while a stack is in flight: `recover_running()`
  cancels ALL in-flight rows (orchestrator + children). Operators
  re-enqueue manually. The webhook handler's
  `_maybe_promote_stack_parent` won't fire because there's no
  active JobStore transaction.
- **Slice failure mid-stack** (e.g. `create_pr` fails for slice 2):
  `_run_stack_phase._cancel_stack` closes the previously-opened
  sibling PRs (`gh pr close --delete-branch`) and marks the
  orchestrator row `failed`. No orphan PRs in the repo.
- **Webhook race with polling**: both paths check the terminal
  status before updating (`update_status` is idempotent for
  `pr_open` → `merging_pr` → `merged`).

### Approved review short-circuit

Phase 2.3 had an explicit no-op for `pull_request_review.approved`
(no `auto-merge` trigger). Phase 2.4 closes it:

- A `pull_request_review.approved` event transitions the job to
  `merging_pr` (or `pr_auto_merge_enabled` if `auto_merge=True`).
- The injected `merge_pr` / `enable_auto_merge` callables are
  wired at server startup (`server/app.py` lifespan).
- A `pull_request.closed+merged` webhook then marks the job
  `merged` (and, for stacks, checks `all_stack_children_merged`
  → promotes the parent orchestrator row to `merged`).

### API additions

- `POST /api/v1/agents/jobs` accepts `split_into`, `split_strategy`,
  `stack_id` (Phase 2.4 fields).
- `GET /api/v1/agents/stacks/{stack_id}` returns
  `{stack_id, parent: JobRecord, children: [JobRecord]}` for
  UIs that want to render the stack.
- `GET /api/v1/agents/jobs/{job_id}` includes the 4 new
  stack fields (`pr_stack_id`, `stack_position`, `stack_size`,
  `depends_on_pr_number`).

### Limitations (Phase 2.5+)

- **Cross-repo stacks**: stacks are 1 repo. Cross-repo stacking
  is not supported in Phase 2.4.
- **Outbound webhooks**: Phase 2.4's webhook receiver is
  inbound-only. Outbound webhooks (notify external systems
  of job state changes) are Phase 4.
- **Web UI**: the `GET /stacks/{id}` endpoint is JSON-only;
  a React tree view is Phase 6.
- **Auto-merge label**: the queue does NOT call
  `gh pr edit --add-label harness-auto-merge`. Operators
  configure branch protection to require the label and
  pre-apply it (or do it via a separate workflow).

---

## Outbound webhooks (Phase 2.5 v0.9.0)

The merge queue can optionally POST to one or more HTTP(S)
URLs when a job hits a high-signal lifecycle event. This is
the lightweight "ping my dashboard / Slack / Telegram"
layer that sits between the in-process `_emit()` bus
(Phase 2.1) and the full Phase 4 hook layer with plugin
discovery.

### Зачем

Without outbound webhooks, the only way to know that a
stack of PRs has finished merging is to poll `GET
/stacks/{id}` (or `GET /jobs/{id}` for a single PR). With
them, the receiver gets a fire-and-forget POST as soon as
the state transition happens — useful for:

- A Slack channel that needs to know "the v2.4 stack landed"
  without polling.
- An internal dashboard that updates its "open PRs" tile.
- A monitoring system that alerts on `failed` events.

### Settings

```ini
# .env
HARNESS_OUTBOUND_WEBHOOK_URLS=https://hooks.slack.com/x/y/z,https://my-dash/notify
HARNESS_OUTBOUND_WEBHOOK_TOKEN=...   # replace ... with the bearer
HARNESS_OUTBOUND_WEBHOOK_TIMEOUT_S=5.0
HARNESS_OUTBOUND_WEBHOOK_MAX_RETRIES=3
```

All four are opt-in — empty `URLS` (the default) disables
outbound entirely. No outbound = no HTTP traffic, no
overhead. Existing Phase 2.4 deployments keep working
unchanged.

### 4 forwarded event kinds

The dispatcher is intentionally narrow — it forwards only
the events that are worth a notification:

| `kind` | When | Where it fires from |
|--------|------|---------------------|
| `merged` | A single PR is merged (auto-merge or webhook). | `merge_queue._emit("merged", ...)` |
| `failed` | Any job failed (single or stack child). | `merge_queue._emit("failed", ...)` |
| `stack_merged` | The parent orchestrator row of a stack was promoted. | `webhook_handler.dispatch_event` after `_maybe_promote_stack_parent` |
| `pr_waiting_review` | A PR is green but a human review is required. | `merge_queue._run_pr_phase` after `wait_for_checks` returns `review_required` |

Other events (`pr_creating`, `running_code`, `pr_open`,
`code_done`, `running_review`, etc.) are deliberately
ignored by the dispatcher. If you want those too, wait
for Phase 4's full hook layer (12 hook points + plugin
discovery).

### Payload shape

Every POST body is JSON, mirroring the in-process
`JobEvent` shape:

```json
{
  "event": "job_event",
  "job_id": "abc123def456",
  "kind": "merged",
  "pr_url": "https://github.com/o/r/pull/42",
  "pr_number": 42
}
```

`stack_merged` events use a slightly different shape
(it's a stack-level event, not a job-level one):

```json
{
  "event": "stack_merged",
  "job_id": "<parent orchestrator id>",
  "kind": "stack_merged",
  "stack_id": "abc123",
  "children_count": 2
}
```

The receiver should treat unknown fields as forward-compat
noise and rely on `kind` + `event` to dispatch.

### Auth

Every POST includes `Authorization: Bearer <token>` if
`outbound_webhook_token` is set (the recommended config).
Empty token = no `Authorization` header (NOT recommended
in production — anyone who can reach the URL can read the
events). Phase 4 will replace this with HMAC signing.

### Retry semantics

- 2xx → success, no retry.
- 4xx → no retry (client error, the receiver is misconfigured).
  We log a warning so the operator notices.
- 5xx / timeout / connection error → retry with
  exponential backoff: `min(initial * 2^attempt, max) +
  random.uniform(0, jitter)` seconds. Up to
  `max_retries` attempts total. After exhaustion, log a
  warning and drop the event (the job's lifecycle is
  NOT affected — outbound is best-effort).
- Multiple URLs are dispatched **concurrently** (one slow
  receiver doesn't starve the others).

### Manual test with a local sink

```bash
# Terminal 1: a 1-line HTTP sink that prints every POST.
python -c "
import http.server, json
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get('content-length', 0))
        body = self.rfile.read(n)
        print('OUTBOUND:', self.path, json.loads(body))
        self.send_response(200); self.end_headers()
http.server.HTTPServer(('127.0.0.1', 9999), H).serve_forever()
"

# Terminal 2: enqueue a job with outbound.
HARNESS_OUTBOUND_WEBHOOK_URLS=http://127.0.0.1:9999/hook \
HARNESS_OUTBOUND_WEBHOOK_TOKEN=test-tok \
harness agents run code "refactor X" --pr --background

# Terminal 1 shows: OUTBOUND: /hook {"event": "job_event", ...}
```

### Out of scope (Phase 2.5)

- **HMAC signing.** Plain bearer token. Phase 4 will add a
  signature header + replay protection.
- **Per-URL routing.** All events go to all configured URLs.
- **Persistent retry queue.** A delivery that fails on every
  attempt is dropped. Phase 4 will add a durable queue.
- **More than 4 event kinds.** Other lifecycle events stay
  internal until the Phase 4 hook layer.

### CLI override

For one-off runs (e.g. testing in dev), the CLI accepts
the same env vars directly; there is no `--outbound-urls`
flag because the env-var-only path keeps the CLI surface
small. Set `HARNESS_OUTBOUND_WEBHOOK_URLS=...` and the
CLI process picks it up at startup.

---

## Cross-repo stacks (Phase 2.5 v0.9.0)

A single task can now open PRs in **N different git repos**
(one PR per repo). Use case: a refactor that touches
several sibling services in a poly-repo.

### Setup

```bash
harness agents run code "refactor cross-service" \
  --split-into 2 \
  --stack-repos /path/to/repo-a,/path/to/repo-b \
  --pr --background
```

- `--stack-repos` is a comma-separated list of absolute paths.
- `len(stack_repos)` MUST equal `--split-into`. The CLI
  validates this and exits 2 on mismatch.
- Each path must exist on disk (validated).

### How it differs from a same-repo stack

- Phase 2.4 stacks: 1 worktree, N branches (`git checkout
  -B harness/<id>/step-N`). Per-repo lock serialised.
- Phase 2.5 cross-repo: N worktrees (1 per repo), 1
  branch per worktree. The orchestrator's per-repo
  `RepoLockRegistry` lock chain ensures two slices in the
  same repo never run concurrently.
- Each slice's `create_pr`, `add_pr_label`, and `merge_pr`
  calls use the per-slice repo as the `cwd`.

### Recovery & failure modes

- A slice's `_commit_slice` / `_push_branch` failure →
  cascade-cancel previously-opened siblings (same as
  Phase 2.4).
- A `create_pr` failure for a slice → same cascade.
- The orchestrator row stores `stack_repos` as a JSON
  list in the `merge_jobs.stack_repos` column (Phase 2.5
  schema migration). The column is NULL for single-repo
  jobs, populated for cross-repo.

### API

`POST /api/v1/agents/jobs` accepts `stack_repos`:

```json
{
  "prompt": "refactor cross-service",
  "split_into": 2,
  "pr_mode": "draft",
  "stack_repos": ["/path/to/repo-a", "/path/to/repo-b"]
}
```

`GET /jobs/{id}` returns the same field in the response
for observability. `GET /stacks/{stack_id}` shows each
child's `repo` (so an operator can verify the per-slice
target).

---

## Rate-limit handling (Phase 2.5 v0.9.0)

Every `gh` call in `harness/agents/pr_integration.py`
goes through `_gh_with_retry`, a thin wrapper that
retries 403 / 429 responses with exponential backoff
and (when present) honours `Retry-After: N` from the
`gh` stderr.

### Settings

```ini
HARNESS_PR_RATE_LIMIT_MAX_RETRIES=5           # default 5
HARNESS_PR_RATE_LIMIT_INITIAL_BACKOFF_S=2.0    # default 2s
HARNESS_PR_RATE_LIMIT_MAX_BACKOFF_S=60.0       # default 60s
HARNESS_PR_RATE_LIMIT_JITTER_S=0.5             # default 0.5s
```

`max_retries=0` disables retry entirely (Phase 2.4
behaviour: first 429 → `GHUnavailable` raised).

### What counts as "rate limit"

`_gh_with_retry` triggers on stderr that contains any
of:

- `API rate limit exceeded`
- `rate limit`
- `secondary rate limit`
- `abuse detection`
- `HTTP 429`
- `HTTP 403`

The exact set is in `_RATE_LIMIT_PATTERNS` in
`pr_integration.py`. The patterns are conservative
(false positives are worse than misses — a non-rate-limit
error is best surfaced immediately so the caller can
diagnose it).

### What doesn't retry

Non-`gh` failures (network, auth, repo state) are NOT
retried. They surface to the caller immediately as
`GHUnavailable` (or the caller's specific error). The
intent: rate-limit retry is the only thing we
heuristically detect; everything else is the user's
problem.

### Per-`_gh` retries

The wrapper delegates to the module-level `_gh` (which
tests monkeypatch). Existing Phase 2.2/2.3/2.4 tests
that swap `_gh` for a fake transport continue to work
unchanged — they don't simulate 403/429, so no retry
kicks in.

---

## Auto-add label (Phase 2.5 v0.9.0)

When `job.auto_merge=True` and `settings.auto_add_label=True`
(the default), the queue automatically calls
`gh pr edit <N> --add-label harness-auto-merge`
immediately after `gh pr create` succeeds. The label
is the configured `auto_merge_label` (default
`harness-auto-merge`).

### Failure handling

If the label call fails (e.g. the label doesn't exist
in the repo, or the token lacks `repo` scope), we log
a warning and **continue**. The auto-merge step will
surface the real branch-protection error if the missing
label was the only blocker.

Disable per-job (rare): set `HARNESS_AUTO_ADD_LABEL=false`
in the env, or pass `--no-auto-add-label` (not yet
implemented; the env-var path is the recommended one).

### Stack parity

For stacked PRs, the same auto-label runs per slice —
each slice's PR gets the label after `create_pr`
succeeds. Stacks that mix `--auto-merge` and
`--no-auto-merge` (rare; mostly a config-drift issue)
get labels only for the slices whose `auto_merge=True`.

