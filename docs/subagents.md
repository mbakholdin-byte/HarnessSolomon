# Sub-agents — Solomon Harness Phase 2.0

Sub-agents are isolated, role-specific LLM loops that run inside their own
`git worktree` and are dispatched by an LLM-as-router. Four built-in agents
ship with the package; you can override or extend them with `.md` files in
`.harness/agents/`.

This document is the operator's guide. See `harness/agents/` for the
implementation; the API surface is in `harness/agents/__init__.py`.

## Built-in agents

| Name     | Model          | Permissions | `max_iterations` | Tools (default) |
|----------|----------------|-------------|------------------|------------------|
| `explore` | `MiniMax-M2.7` | read-only   | 8                | `read_file`, `grep`, `glob` |
| `plan`    | `MiniMax-M2.7` | read-only   | 10               | `read_file`, `grep`, `glob` |
| `code`    | `MiniMax-M2.7` | full        | 8                | `read_file`, `write_file`, `edit_file`, `bash`, `grep`, `glob` |
| `review`  | `MiniMax-M2.7` | read-only   | 8                | `read_file`, `grep`, `glob` |

All four use the same `worktree_required: true` setting. To list what's
installed in a project:

```bash
python -m harness agents list
```

## Custom agents

Drop a `.md` file under `.harness/agents/` with YAML frontmatter. The
filename **must** match the `name:` field. The body of the markdown
becomes the system prompt.

```markdown
---
name: refactor-helper
model: MiniMax-M2.7
tools: [read_file, grep, glob, edit_file]
permissions: scoped-write
max_iterations: 12
worktree_required: true
allowed_paths: ["src/**", "tests/**"]
---

You are the refactor-helper. Find duplicate code and propose specific
edits. NEVER touch generated code or vendored dependencies.
```

Resolution order (later wins):

1. Built-in specs shipped with the package (`harness/agents/builtin/*.md`).
2. **Your** `.harness/agents/<name>.md` (this directory — overrides built-ins).

To restore a built-in, delete your override file.

### Frontmatter schema

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `name` | string (kebab-case) | required | must match filename |
| `model` | model id | `MiniMax-M2.7` | must exist in `harness.server.llm.models.MODELS` |
| `tools` | list[string] | `[read_file, grep, glob]` | tool names from `harness.server.agent.tools` |
| `permissions` | `read-only` \| `scoped-write` \| `full` | `read-only` | `read-only` strips `write_file` / `edit_file` regardless of `tools` (defence in depth) |
| `system_prompt` | string | empty (body is used) | either inline or from the markdown body |
| `max_iterations` | int (1–20) | 5 | agent loop cap per task |
| `worktree_required` | bool | `true` | if false, runs in `self.repo` directly (no isolation) |
| `allowed_paths` | list[string] | `[]` | empty = entire worktree; globs restrict further (Phase 2.1: enforced at runtime) |

## Worktree isolation

Every sub-agent with `worktree_required: true` runs inside its own
`git worktree` on a fresh branch `harness/<id>`.

- `<id>` is auto-generated as `wt-<8 hex>` (e.g. `wt-3a7beb7c`) unless
  you pass an explicit `worktree_id` to `AgentRunner.run`.
- The branch is created with one empty commit (`sub-agent start: ...`)
  so downstream `git merge --ff-only harness/<id>` has a valid target.
- After the agent completes, the worktree is removed (`git worktree
  remove --force`) but the branch is preserved as an orphan — the
  merge queue decides whether to merge it or delete it explicitly.

### Recovery from a crashed worktree

If a sub-agent crashes between `worktree add` and `__aexit__`, you may
find an orphan `harness/<id>` branch and a stale working tree. Cleanup:

```bash
# List all worktrees (look for entries under .harness/worktrees/).
git worktree list

# Force-remove a stale worktree.
git worktree remove --force .harness/worktrees/<id>

# Delete the orphan branch.
git branch -D harness/<id>
```

The merge queue's `WorktreeSession` self-heals on the next attempt: it
deletes the orphan branch before creating a fresh worktree.

## Adversarial verify

For critical answers (e.g. a code agent claiming "tests pass"), the
merge queue runs an **adversarial panel** — the same prompt is sent
through the same model `N=2` (or `N=3`) times at `temperature=0.4`,
and a majority must say `PASS`.

- `judges=1`: single-shot (no majority).
- `judges=2`: BOTH must PASS (1-1 split → reject). This is the
  "2/3 majority" relaxation for even-sized panels.
- `judges=3+`: majority wins.

Configure via `settings.subagent_judges` (default 2) and
`settings.subagent_timeout_s` (default 300).

## CLI quickstart

```bash
# List installed agents.
python -m harness agents list

# Run a single sub-agent in a worktree.
python -m harness agents run explore "find usages of foo in the repo"

# Override worktree (skip isolation, useful for smoke tests).
python -m harness agents run explore "list files" --no-worktree

# Run with a custom repo and a stable worktree id.
python -m harness agents run code "add a docstring" \
    --repo /path/to/repo --worktree-id my-task-1
```

## Programmatic API

```python
import asyncio
from pathlib import Path
from harness.agents.registry import load_agent
from harness.agents.runner import AgentRunner
from harness.server.llm.router import LLMRouter

async def main():
    spec = load_agent("explore", project_root=Path("."))
    router = LLMRouter()
    runner = AgentRunner(router=router, repo=Path("."))
    result = await runner.run(spec, "list the 4 built-in agent names")
    print(result.final_text)

asyncio.run(main())
```

For the full merge queue (code → review → verify → merge):

```python
from harness.agents.merge_queue import MergeJob, MergeQueue
from harness.agents.verify import AdversarialVerify

verifier = AdversarialVerify(router, judges=2)
queue = MergeQueue(runner, verifier)
result = await queue.enqueue(MergeJob(
    code_spec=code_spec, review_spec=review_spec,
    task="add /api/v1/widgets", worktree_id="api-widgets",
))
assert result.merged
```

## Trust boundary

Sub-agents **cannot spawn sub-agents**. This is enforced at the
import level (verified by static tests):

- `harness/agents/runner.py` does NOT import `LLMRouterClassifier`,
  `AdversarialVerify`, or `MergeQueue`.
- A code review pass should `grep -rn "from harness.agents" harness/agents/runner.py`
  to confirm only the allowed imports are present.

If you need a "sub-agent calls sub-agent" workflow, run the second
agent from the **parent** (your code), not from the first agent.

## Out of scope (Phase 2.1+)

The following are deliberately not in Phase 2.0:

- **Cost-aware T1→T2→T3 cascade** (Phase 2.1) — currently every sub-agent
  hits the same `MiniMax-M2.7` (T3 cloud). The router returns
  `confidence` so a future cascade can promote to a more capable model
  on low confidence, or fall back to a cheap local model on high
  confidence.
- **Persistent background mode** (Phase 2.1) — all sub-agents are
  await-to-completion. Progress reporting and resumption land in
  Phase 2.1.
- **Real GitHub PR integration** (Phase 2.2) — the merge queue is
  in-process (`git merge --ff-only` into the local main). PR creation
  lands in Phase 2.2.
- **Parallel cross-repo merge queue** (Phase 2.2) — jobs are serialised
  by a single `asyncio.Lock`.
- **Per-agent memory namespacing** in `UnifiedMemory` (Phase 2.1) —
  all sub-agents currently share the parent `solomon` memory scope.
- **MemPalaceAdapter** for L2.5 (separate track) — not a Phase 2
  concern despite the L2.5 placeholder comment in `harness/memory/`.
