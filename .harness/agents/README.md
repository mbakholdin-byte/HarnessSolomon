# Sub-agent overrides — drop your `.md` files here

This directory is for **user-editable** sub-agent specs. Each `.md` file
defines one sub-agent via YAML frontmatter (the body is the system prompt).

**Resolution order** (later wins):

1. Built-in specs shipped with the package (`harness/agents/builtin/*.md`)
2. **Your** `.harness/agents/<name>.md` (this directory — overrides built-ins)

To override a built-in, create a file with the **same `name`** in the
frontmatter. To add a new agent, pick a unique name not already in the
built-in set. The filename should match the `name:` field (case-sensitive).

## Example — replace `explore` with a custom prompt

```markdown
---
name: explore
model: MiniMax-M2.7
tools: [read_file, grep, glob]
permissions: read-only
max_iterations: 12
worktree_required: true
---

You are the explore sub-agent. Focus on finding security-sensitive code:
authentication, authorisation, and any call to `eval`, `exec`, `subprocess`,
`shell=True`. Report each finding with a file:line reference.
```

## Frontmatter schema

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `name` | string (kebab-case) | required | must match filename |
| `model` | model id | `MiniMax-M2.7` | must exist in `harness.server.llm.models.MODELS` |
| `tools` | list[string] | `[read_file, grep, glob]` | tool names from `harness.server.agent.tools` |
| `permissions` | `read-only` \| `scoped-write` \| `full` | `read-only` | `read-only` strips `write_file` / `edit_file` regardless of `tools` |
| `system_prompt` | string | empty (body is used) | either inline or from the markdown body |
| `max_iterations` | int (1–20) | 5 | agent loop cap per task |
| `worktree_required` | bool | `true` | if false, runs in `self.repo` directly |
| `allowed_paths` | list[string] | `[]` | empty = entire worktree; globs restrict further |

See `docs/subagents.md` (Phase 2.0 Step 7) for the full guide.
