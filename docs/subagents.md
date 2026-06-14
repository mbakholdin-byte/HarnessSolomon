# Sub-agents — Solomon Harness Phase 2.0 + 2.1

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

## Cost-aware T1→T2→T3 cascade (Phase 2.1)

The router returns a `confidence` float (0.0–1.0) alongside the
agent name. The `TierSelector` (in `harness/agents/cascade.py`)
maps that confidence to a tier:

| Confidence band             | Tier | Default model  | Cost                |
|-----------------------------|------|----------------|---------------------|
| `>= subagent_confidence_high` (0.85) | T1   | `qwen3:8b` (Ollama) | local, $0         |
| `[low, high)` (0.55–0.85)   | T2   | `glm-4.7` (cloud)   | mid-tier cloud    |
| `< subagent_confidence_low` (0.55)  | T3   | `MiniMax-M2.7` (cloud) | premium cloud |

When the router returned `fallback=True` (e.g. parse failure),
the selector **forces T3** — we don't take the cheap-local risk
when the router itself wasn't sure.

When `subagent_t1_model` is empty (e.g. CI without Ollama), the
T1 band is **skipped** and the cascade degrades to T2/T3 on the
same thresholds. There is no failure on the T1-missing path.

**Settings (override via env or `.env`):**

```bash
SUBAGENT_T1_MODEL=qwen3:8b
SUBAGENT_T2_MODEL=glm-4.7
SUBAGENT_CONFIDENCE_HIGH=0.85
SUBAGENT_CONFIDENCE_LOW=0.55
```

The model validator rejects `low >= high` at load time so a
misconfigured cascade fails fast instead of degenerating to a
binary T1/T3 with no T2 band.

**Wiring the cascade into a run** (programmatic):

```python
from harness.agents.cascade import select_tier

decision = select_tier(router_output.confidence, fallback=router_output.fallback)
result = await runner.run(
    spec, prompt,
    model_override=decision.chosen_model,  # or None to keep spec.model
)
```

**CLI smoke** (uses confidence=0.95 to deterministically pick T1):

```bash
python -m harness agents run explore "list 4 built-ins" --no-worktree --cascade
# stderr: cascade: tier=T1 confidence=0.95 model=qwen3:8b ...
```

## Persistent background mode (Phase 2.1)

`MergeQueue.enqueue_async()` returns a `job_id` immediately and runs
the job in a background `asyncio.Task`. Status and event log persist
in a small SQLite table (`merge_jobs` + `merge_events`).

**Enqueue:**

```bash
python -m harness agents run code "add type hints" --background
# job_id=8a3f9b2c1d4e5f6a
#   status: use `harness agents jobs 8a3f9b2c1d4e5f6a` to poll
```

**Poll a single job:**

```bash
python -m harness agents jobs 8a3f9b2c1d4e5f6a
# job_id=8a3f9b2c1d4e5f6a
#   worktree_id : cli-4155
#   status      : merged
#   model       : MiniMax-M2.7
#   cost        : $0.0023
#   started_at  : 2026-06-14T12:31:29.706000
#   finished_at : 2026-06-14T12:32:14.913000
#   error       : (none)
```

**List recent jobs:**

```bash
python -m harness agents jobs --recent 20
# job_id              status          model           cost   worktree_id  started_at
# -----------------------------------------------------------------------------------
# 8a3f9b2c1d4e5f6a    merged          MiniMax-M2.7    $0.0023 cli-4155   2026-06-14T12:31:29
# ...
```

**Storage:** the JobStore lives at
`<settings.db_path.parent>/agent-jobs.db`. The CLI picks it up
automatically; programmatic users construct
`JobStore(path)` and pass it via `MergeQueue(..., store=store)`.

**Resume after restart:** the same store file is read on next
startup. `JobStore.recover_running()` (called automatically by
the FastAPI lifespan handler) marks any `running_*` jobs as
`cancelled` with `error="process restarted"`. Operators can
re-enqueue manually with the original `worktree_id`.

**Status values:** `queued`, `running_code`, `running_review`,
`verifying`, `merged`, `failed`, `timeout`, `cancelled`.

## Per-agent memory namespacing (Phase 2.1)

By default, every sub-agent shares the parent
`UnifiedMemory(agent_id="solomon")` — there's no isolation, and
the search/recall surface is one big pool.

To give a custom sub-agent its own namespace, add
`memory_namespace: <kebab-case>` to its `.md` frontmatter:

```markdown
---
name: code-review
model: MiniMax-M2.7
tools: [read_file, grep, glob]
permissions: read-only
memory_namespace: code-review   # own hmem / mem0 / hybrid / file dirs
max_iterations: 8
---

You review code changes against our internal style guide.
```

The runner threads `spec.memory_namespace` into a
`UnifiedMemory(agent_id="code-review")` whose 4 adapters
use disjoint storage paths:

| Layer | Adapter        | How the namespace is propagated       |
|-------|----------------|---------------------------------------|
| L1    | hmem           | `HmemAdapter(agent="code-review")`     |
| L2    | mem0           | `Mem0Adapter(user_id="code-review", collection="solomon-code-review-memories")` |
| L3    | hybrid (SQLite)| `HybridAdapter(project="code-review", default_tags=["#agent/code-review"])` |
| L4    | file (Markdown)| `FileAdapter(memory_dir=<root>/code-review)` |

`UnifiedMemory.write()` auto-stamps:

- `memory.metadata["agent_id"]` (unless the caller set it explicitly)
- `memory.tags.append("#agent/<id>")` (skipped for the default
  `solomon` namespace to avoid polluting existing memories)
- `memory.provenance.append(ProvenanceEntry(layer="L_meta",
  source="unified", id=<agent_id>))` so the audit trail records
  which facade stamped the entry

**Programmatic factory:**

```python
def unified_memory_for_spec(spec: AgentSpec) -> UnifiedMemory:
    return UnifiedMemory(
        hmem_dir=settings.hmem_dir,
        mem0_dir=settings.mem0_dir,
        hybrid_dir=settings.hybrid_dir,
        file_dir=settings.file_dir,
        agent_id=spec.memory_namespace or "solomon",
    )

runner = AgentRunner(router=router, repo=repo,
                     unified_memory_factory=unified_memory_for_spec)
```

`AgentRunner.get_unified_memory(spec)` is cached by `spec.name`
so the same spec reuses the same `UnifiedMemory` across runs.

## Out of scope (Phase 2.2+)

The following are deliberately not in Phase 2.1:

- **Real GitHub PR integration** (Phase 2.2) — the merge queue is
  in-process (`git merge --ff-only` into the local main). PR creation
  lands in Phase 2.2.
- **Parallel cross-repo merge queue** (Phase 2.2) — jobs are serialised
  by a single `asyncio.Lock`.
- **Cascade calibration** (Phase 5) — the thresholds `0.85` / `0.55`
  are educated guesses. We will measure them on a real eval set
  (Phase 5) and adjust.
- **Hot-reload `.harness/agents/*.md`** (Phase 4) — registry reads
  the directory on every call. A file-watcher is Phase 4.
- **MemPalaceAdapter** for L2.5 (separate track) — not a Phase 2.1
  concern despite the L2.5 placeholder comment in `harness/memory/`.
