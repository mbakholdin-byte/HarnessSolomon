# Phase 3 — Operator Guide

Phase 3 ships three production-grade features in a single release
(`v1.0.0`):

1. **Context compaction** — sliding window + LLM summary for long
   chat histories. Prevents context overflow on 50+ turn sessions.
2. **Local ONNX embeddings** — `multilingual-e5-small` (RU + EN,
   384-dim, ~120 MB) for semantic memory search. No cloud calls.
3. **Pre-LLM redaction** — 12 categories of PII / secrets scrubbed
   at 9 sink points (LLM messages, PR title/body, commit msg,
   branch name, JobStore prompt, outbound webhooks, `.env` reads,
   inbound webhooks). Default ON, opt-out via env var.

This guide covers operator-facing configuration. Developer-facing
docs live in the standard module docstrings and `docs/CHANGELOG.md`.

---

## 1. Context compaction

### What it does

`ContextCompactor` collapses long `messages` lists before each
LLM call. Two-phase algorithm:

1. **Sliding window** — drop the oldest non-system messages,
   preserving tool-call ↔ tool-result pairs and the recent
   `compaction_keep_recent_turns` tail.
2. **LLM summarisation** — if sliding alone is insufficient, the
   dropped turns are summarised by the configured `compaction_summarizer_model`
   (default T1 = local Qwen3 8B via Ollama, free). The summary
   is inserted as a single `user` message after the system
   prompt.

The summary is optionally written to `UnifiedMemory` (L2 mem0)
with tag `#compact` for cross-session semantic retrieval.

### Configuration

All settings are in `harness/config.py` and overridable via
`HARNESS_*` env vars (Pydantic v2 `BaseSettings`):

| Setting | Default | Description |
|---|---|---|
| `compaction_enabled` | `True` | Master switch. Set False to disable. |
| `compaction_threshold_ratio` | `0.75` | Trigger compaction when usage > 75% of model context. |
| `compaction_target_ratio` | `0.50` | After compaction, target 50% of model context. |
| `compaction_keep_recent_turns` | `6` | Minimum recent turns kept verbatim. |
| `compaction_summarizer_model` | `""` → `subagent_t1_model` (Qwen3 8B) | Summariser model id. |
| `compaction_summarizer_fallback` | `""` → `subagent_t2_model` (cloud mid-tier) | Fallback if primary errors. |
| `compaction_summarizer_max_input_tokens` | `0` → `16000` | Hard cap on summariser input. |
| `compaction_persist_to_memory` | `True` | Write summary to L2 with tag `#compact`. |

Invariants enforced by the `Settings` validator:
- `compaction_target_ratio < compaction_threshold_ratio` (both
  open unit interval).
- `compaction_keep_recent_turns >= 2` (need at least one
  user/assistant pair for the LLM to maintain context).

### When to disable

Set `HARNESS_COMPACTION_ENABLED=false` if:
- All your models have 200K+ context (compaction is wasted work).
- You're benchmarking raw model performance and don't want the
  sliding window to interfere.
- The T1 summariser (Qwen3 8B via Ollama) is unavailable AND
  the cloud fallback is too expensive.

### Cost & latency

- T1 (Qwen3 8B) summarisation: $0 per call (local). Latency:
  ~2-5s for a 4K-token input.
- T2 fallback: ~$0.10 / 1M tokens (cloud). Latency: ~1-2s.
- Each chat turn that triggers compaction adds one summariser
  call. For a 200-message session with 50K tokens compacted
  across 10 turns: $0 + ~30s total summariser time (T1).

### Trust boundary

`runner.py` does NOT import the `ContextCompactor` (or the
LLMRouter classifier, MergeQueue, or AdversarialVerify). The
compactor is injected via `AgentLoop.__init__(compactor=...)`
and `ChatSession.__init__(compactor=...)`. The static
trust-boundary test in `test_agent_runner.py:516-575`
continues to hold.

---

## 2. ONNX local embeddings

### What it does

`OnnxEmbedder` loads `intfloat/multilingual-e5-small` via
ONNX Runtime. Embeddings are L2-normalised 384-dim float32
vectors. Used by `DenseRetriever` (cosine over stored vectors)
and `HybridRetriever` (RRF k=60 fusion with BM25).

The model is multilingual (RU + EN + 100+ languages) and runs
fully offline after the first download.

### Installation

The ONNX backend is an **optional extra**:

```bash
pip install -e ".[embeddings]"
```

This pulls in:
- `onnxruntime>=1.18` (CPU build; Windows wheels are prebuilt)
- `numpy>=1.26` (for vector ops)

The `tokenizers` package is already in the base venv (transitive
via `litellm`).

### First-run model download

`OnnxEmbedder` lazy-loads the model on the first call. If the
ONNX file is not present in `embeddings_dir`, the operator
needs to download it manually:

```bash
# Option 1: huggingface_hub (recommended)
python -c "from huggingface_hub import snapshot_download; snapshot_download(repo_id='Xenova/multilingual-e5-small-onnx', local_dir='./models/embeddings')"

# Option 2: direct curl
mkdir -p models/embeddings
curl -L "https://huggingface.co/Xenova/multilingual-e5-small-onnx/resolve/main/model.onnx" -o models/embeddings/model.onnx
curl -L "https://huggingface.co/Xenova/multilingual-e5-small-onnx/resolve/main/tokenizer.json" -o models/embeddings/tokenizer.json
```

The operator can also point `HARNESS_EMBEDDINGS_DIR` to an
existing HuggingFace cache to share the model across projects.

### Configuration

| Setting | Default | Description |
|---|---|---|
| `embeddings_dir` | `<project_root>/models/embeddings` | Where the ONNX file + tokenizer live. |
| `embedding_model` | `intfloat/multilingual-e5-small` | HF model id. |
| `embedding_precision` | `int8` | `int8` (~30MB, ~30ms/query) or `fp32` (~120MB, ~50ms/query). |
| `embedding_dim` | `384` | Vector dim. Must match the model. |

### Asymmetric prefixes (E5)

E5 requires a different prefix for queries vs documents:

| Side | Prefix | Example |
|---|---|---|
| Query | `query: ` | `embed_query("What is X?")` embeds `"query: What is X?"` |
| Document | `passage: ` | `embed_documents(["X is a ..."])` embeds `["passage: X is a ..."]` |

The embedder applies these automatically via the
`embed_query` / `embed_documents` methods. **Do NOT prefix
your text manually** — the embedder will double-prefix and
recall will collapse.

### Versioning

Each stored vector carries `metadata.embedding_version` (e.g.
`multilingual-e5-small-int8@1`). When you bump the model or
precision, the version changes and `DenseRetriever` filters out
old-version vectors from the dense path. BM25 still finds them,
so no information is lost — re-embed at your leisure (Phase 3.5
will ship a migration tool).

### Privacy

`PrivacyAwareEmbedder` is the recommended wrapper. It runs
`redaction.redact()` on every text BEFORE embedding, so PII
and secrets never enter the vector space. The default
`UnifiedMemory.write()` extension (when an embedder is injected)
should always use `PrivacyAwareEmbedder`, not `OnnxEmbedder`
directly.

---

## 3. Pre-LLM redaction

### What it does

Every external sink point that carries user-controlled content
runs `redaction.redact()` before persistence or transmission:

| # | Sink | Where | Default behaviour |
|---|---|---|---|
| 1 | LLM messages (system + user + tool results) | `runner.py`, `loop.py` | Always redacted. |
| 2 | PR title | `merge_queue.py:827` | Always redacted. |
| 3 | PR body | `merge_queue.py:864` | Always redacted. |
| 4 | Commit message (stacked slices) | `merge_queue.py:1493` | Always redacted. |
| 5 | Branch name (user-supplied `--worktree-id`) | `cli.py:214` | Redacted when user provides explicit id. |
| 6 | JobStore `prompt` column | `merge_queue.py:296` | Always redacted. |
| 7 | Outbound webhook payloads | `outbound.py:_deliver_one` | Always redacted (defence in depth). |
| 8 | `read_file` tool output | `runtime.py:_read_file` | Always redacted. |
| 9 | Inbound webhook payload (post-HMAC verify) | `webhook_handler.py:handle_raw` | Always redacted. |

### Categories

12 stdlib `re` patterns (no third-party deps):

| Category | Examples |
|---|---|
| `EMAIL` | `alice@example.com` |
| `PHONE` | `+1 (555) 123-4567` |
| `IPV4` | `192.168.1.42` |
| `GITHUB_TOKEN` | `ghp_abc...`, `github_pat_...`, `gho_...`, etc. |
| `AWS_ACCESS_KEY` | `AKIAIOSFODNN7EXAMPLE` |
| `AWS_SECRET` | `aws_secret_access_key=...` |
| `OPENAI_KEY` | `sk-proj-abc...` |
| `ANTHROPIC_KEY` | `sk-ant-api03-...` |
| `ENV_ASSIGNMENT` | `DB_PASSWORD=hunter2` |
| `JWT` | `eyJhbG...` (3 segments) |
| `PEM_PRIVATE_KEY` | `-----BEGIN RSA PRIVATE KEY-----` |
| `SLACK_TOKEN` | `xoxb-...` |

### Replacement format

`<CATEGORY>` placeholders (NOT `***REDACTED***`). The LLM can
use the category to reason about the redacted content (e.g. for
an email it can infer "the user wants me to send something to
<EMAIL>").

### Idempotency

`redact(redact(x)) == redact(x)`. Placeholders contain no
recognisable secret, so re-running cannot double-match. This
matters for tooling that re-scans text (e.g. outbound webhook
payloads re-encoded as JSON).

### Configuration

| Setting | Default | Description |
|---|---|---|
| `redaction_enabled` | `True` | Master switch. Set False to disable. |
| `redaction_categories` | `""` (all 12 defaults) | Comma-separated category list to narrow the pattern set. |
| `redaction_audit_log` | `False` | When True, mirror to `data/audit/redaction-YYYY-MM-DD.ndjson` + JobStore event log. |

### When to disable

Set `HARNESS_REDACTION_ENABLED=false` only for:
- Tests (the test fixtures rely on raw text).
- Offline single-tenant deployments where the operator is the
  only user and they want minimal overhead.
- Benchmarks comparing redacted vs. unredacted quality.

**Do not disable in multi-user deployments or any environment
where the LLM API is shared.** The redaction is the only
barrier between a user's secrets and the LLM provider.

### Audit log

When `redaction_audit_log=True`, every redaction event is
mirrored to:

1. **JSONL file**: `<embeddings_dir>/../audit/redaction-YYYY-MM-DD.ndjson`
   (rotated daily, append-only).
2. **JobStore event log**: `kind="redaction"` with payload
   `{sink, categories, count, ts}`.

The original secret is **never** logged — only category names
and counts. The audit log is for compliance review, not for
forensic recovery of the leaked value.

### Known limitations

Phase 3 does NOT detect secrets that are:
- Base64-encoded (`echo ghp_abc | base64`)
- Hex-encoded (`echo ghp_abc | xxd -p`)
- Inside a JSON string with escaped quotes (`"secret": "\\nghp_abc"`)
- Obfuscated with comments (`ghp_/* */abc`)

Phase 3.5+ will address these with a configurable plug-in
pattern loader (`redaction_extra_patterns_path`).

---

## 4. CLI smoke tests

```bash
# 1. Verify settings load
python -c "from harness.config import settings; print(settings.compaction_enabled, settings.embedding_model, settings.redaction_enabled)"

# 2. Verify redaction at the CLI prompt
harness agent run --prompt "Email me at alice@example.com token ghp_abc123" --no-stream
# → The JobStore prompt column should contain <EMAIL> and <GITHUB_TOKEN> placeholders.

# 3. Verify compaction on a long session
harness chat --session long-session-id  # 50+ msgs
# Check Mem0 store for entries with tag #compact.

# 4. Verify embeddings + hybrid search
harness memory search "what was the config for X" --mode hybrid
# Returns top-k with scores (cosine + BM25 RRF-merged).

# 5. Verify redaction sinks via CLI (manual)
harness agent run --prompt "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE" --no-stream
# → PR title, body, commit message all contain <AWS_ACCESS_KEY>.
```

---

## 5. Upgrading from v0.9.0 (Phase 2.5)

Backward compat:
- `compaction_enabled=False` → all pre-Phase-3 behaviour preserved.
- `redaction_enabled=False` → all sinks pass raw text.
- `UnifiedMemory(embedder=None)` → `metadata.embedding` is not
  populated, `search_scored` falls back to `search`.
- All 822 Phase 1.6+2.2+2.3+2.4+2.5 tests pass without changes.

Breaking changes: **none**. Phase 3 is additive on the public
API surface. (An earlier draft of the plan considered breaking
`UnifiedMemory.search` to return `list[tuple[Memory, float]]`,
but we kept `search` as `list[Memory]` and added a new
`search_scored` method to preserve the contract.)

New dependencies: **0 required**, **2 optional** (`onnxruntime`,
`numpy` via `pip install -e ".[embeddings]"`).

---

## 6. Metrics summary

- 5 commits (Step 0..4) — `feat(phase3): Step 0/1/2/3` + closeout
- 822 → 962 mock tests passed (0 regressions, +140 new)
- 14 new files (~1200 LoC production + ~900 LoC tests)
- 12 modified files
- Tag: `v1.0.0`

See `docs/CHANGELOG.md` for the full change list and
`docs/roadmap.md` for the high-level plan.
