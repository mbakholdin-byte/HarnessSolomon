"""Solomon Harness — configuration.

Loads from environment variables (and .env file in dev).
Single source of truth for all paths, ports, API keys.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Harness settings (Pydantic v2)."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # === Server ===
    host: str = Field(default="0.0.0.0", description="Bind host")
    port: int = Field(
        default=8765,
        description=(
            "Bind port. Default 8765 because Windows 11 + Docker Desktop reserves "
            "8000/8001 via hns (WSAEACCES). See _output/2026-06/14.06/ports-map.md"
        ),
    )
    log_level: str = Field(default="INFO", description="Logging level")

    # === Storage ===
    project_root: Path = Field(
        default=Path("C:/MyAI"),
        description="Project root for file tools (paths are resolved under this)",
    )
    session_dir: Path = Field(
        default=PROJECT_ROOT / "data" / "sessions",
        description="JSONL session mirror directory",
    )
    db_path: Path = Field(
        default=PROJECT_ROOT / "data" / "harness.db",
        description="SQLite path for session metadata index",
    )

    # === LLM Providers (Phase 0: cloud only) ===
    minimax_api_key: str = Field(default="", description="MiniMax API key")
    zhipuai_api_key: str = Field(default="", description="ZhipuAI (GLM) API key")
    moonshot_api_key: str = Field(default="", description="Moonshot (Kimi) API key")

    # === CORS ===
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",  # Vite dev
            "http://127.0.0.1:5173",
        ],
        description="Allowed CORS origins",
    )

    # === Agent Loop ===
    max_iterations: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Max agent loop iterations per task (safety cap)",
    )

    # === Sub-agents (Phase 2) ===
    agents_dir: Path = Field(
        default=Path(".harness/agents"),
        description=(
            "Directory for user-editable sub-agent .md files (overrides built-ins). "
            "Resolved relative to settings.project_root."
        ),
    )
    subagent_default_model: str = Field(
        default="MiniMax-M2.7",
        description="Default LLM model id for built-in sub-agents (must be in catalog)",
    )
    subagent_judges: int = Field(
        default=2,
        ge=1,
        le=5,
        description="Number of adversarial judges (used by AdversarialVerify).",
    )
    subagent_timeout_s: float = Field(
        default=300.0,
        gt=0,
        description="Wall-clock cap (seconds) for a single sub-agent run (used by MergeQueue).",
    )

    # === Sub-agents cost-aware cascade (Phase 2.1) ===
    subagent_t1_model: str = Field(
        default="qwen3:8b",
        description=(
            "Tier-1 model id (cheap local, e.g. Ollama). Used by TierSelector "
            "when router confidence >= subagent_confidence_high. Set to empty "
            "string to disable T1 (cascade falls back to T2)."
        ),
    )
    subagent_t2_model: str = Field(
        default="glm-4.7",
        description=(
            "Tier-2 model id (cloud mid-tier). Used when confidence is in "
            "[subagent_confidence_low, subagent_confidence_high)."
        ),
    )
    subagent_confidence_high: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description=(
            "Confidence >= this threshold -> Tier-1 (cheap local). "
            "Calibrate via Phase 5 eval harness. See docs/MODEL_REGISTRY.md."
        ),
    )
    subagent_confidence_low: float = Field(
        default=0.55,
        ge=0.0,
        le=1.0,
        description=(
            "Confidence in [low, high) -> Tier-2. Below low -> Tier-3 (premium). "
            "Calibrate via Phase 5 eval harness. See docs/MODEL_REGISTRY.md."
        ),
    )

    # === Sub-agents GitHub PR integration (Phase 2.2) ===
    github_token_env: str = Field(
        default="GITHUB_TOKEN",
        description=(
            "Name of the env var that holds the GitHub token. The token "
            "value is read at PR-creation time and passed to ``gh`` via "
            "the environment (never on the command line). Default: "
            "GITHUB_TOKEN (the standard GitHub Actions convention)."
        ),
    )
    pr_default_target_branch: str = Field(
        default="main",
        description=(
            "Target branch the PR is opened against. Override per-job "
            "via ``MergeJob.pr_target_branch`` or the CLI ``--pr-target`` "
            "flag."
        ),
    )
    pr_poll_interval_s: float = Field(
        default=15.0,
        gt=0.0,
        description=(
            "Seconds between ``gh pr view`` polls while waiting for CI "
            "checks / review decisions. Used by ``wait_for_checks()``."
        ),
    )
    pr_wait_timeout_s: float = Field(
        default=300.0,
        gt=0.0,
        description=(
            "Wall-clock cap (seconds) for waiting on PR checks/review. "
            "After this, the job is marked ``failed`` with "
            "``error='PR checks timed out after Ns'``. Set higher for "
            "repos with slow CI."
        ),
    )
    pr_strategy: str = Field(
        default="auto",
        description=(
            "PR-mode strategy: ``auto`` (PR-IF-REMOTE — if ``origin`` "
            "exists AND ``gh auth status`` is ok, open PR; otherwise "
            "fall back to local ff-merge + warning), ``strict`` (PR is "
            "required; failure on missing gh is a hard error), ``off`` "
            "(never open a PR, always local merge)."
        ),
    )

    # === Scope-gated API (Phase 1.6) ===
    auth_db_path: Path = Field(
        default=PROJECT_ROOT / "data" / "harness-scope.db",
        description=(
            "SQLite path for the Phase 1.6 scope-gated API token store. "
            "Lives one level above the sessions DB. Stores SHA-256 hashes "
            "of tokens, never plaintext."
        ),
    )
    auth_token_bytes: int = Field(
        default=32,
        ge=16,
        le=64,
        description=(
            "Number of random bytes used to generate a new token's "
            "plaintext (returned once at creation time, then discarded). "
            "32 bytes = 256 bits = 64 hex chars, well above OWASP "
            "minimums for a server-issued opaque token."
        ),
    )
    auth_default_scopes: str = Field(
        default="",
        description=(
            "Comma-separated scope names applied to tokens created via "
            "the CLI when ``--scopes`` is not explicitly passed. Empty "
            "string = no scopes (caller must specify). Ignored when the "
            "bootstrap admin token is generated — bootstrap always gets "
            "ALL_SCOPES."
        ),
    )
    auth_required: bool = Field(
        default=True,
        description=(
            "Master switch for the scope-gated API. When True, all "
            "``/api/v1/*`` routes require a valid Bearer token with the "
            "appropriate scope; ``/api/v1/capabilities`` remains public. "
            "When False, the server runs in 'open dev mode' (no auth "
            "checks, useful for local development and the test suite). "
            "Legacy ``/api/*`` routes (sessions, chat, models, health) "
            "are always open in Phase 1.6 regardless of this setting."
        ),
    )

    # === Inbound GitHub webhooks + auto-merge (Phase 2.3) ===
    webhook_secret: str = Field(
        default="",
        description=(
            "HMAC-SHA256 shared secret for the inbound GitHub webhook "
            "receiver. Set this to the same value configured in the "
            "GitHub repo's webhook settings. Empty string = webhooks "
            "disabled (the route returns 503). The value is NEVER "
            "logged or echoed in error messages — only the env var "
            "NAME is surfaced. The HMAC verification uses stdlib "
            "``hmac.compare_digest`` for timing-safe comparison."
        ),
    )
    webhook_path: str = Field(
        default="/api/v1/agents/webhooks/github",
        description=(
            "URL path where the GitHub webhook receiver is mounted. "
            "Configure this in the GitHub repo's webhook settings "
            "exactly. Default: ``/api/v1/agents/webhooks/github``."
        ),
    )
    webhook_max_payload_kb: int = Field(
        default=256,
        ge=1,
        le=10240,
        description=(
            "Maximum accepted webhook payload size in KB. GitHub "
            "webhook payloads are typically <5KB, but ``check_run`` "
            "events with verbose annotations can be larger. 256KB "
            "is a generous cap; the route returns 413 on overflow."
        ),
    )
    auto_merge_label: str = Field(
        default="harness-auto-merge",
        description=(
            "Label required by ``gh pr merge --auto`` (GitHub "
            "branch-protection typically requires a specific label "
            "to enable auto-merge). Set per-repo in the GitHub "
            "branch-protection rule. The merge queue adds this "
            "label automatically before calling ``enable_auto_merge``."
        ),
    )
    auto_merge_method: str = Field(
        default="squash",
        description=(
            "Default merge method for ``gh pr merge --auto``. One of "
            "``squash`` (default), ``merge`` (merge commit), "
            "``rebase`` (rebase + ff). Override per-job via the CLI "
            "``--auto-merge-method`` flag."
        ),
    )
    auto_merge_delete_branch: bool = Field(
        default=True,
        description=(
            "Whether ``gh pr merge --auto`` should pass "
            "``--delete-branch`` to clean up the head branch after "
            "a successful merge. Default True (matches Phase 2.2 "
            "behavior)."
        ),
    )

    # === Phase 2.4: stacked / multi-PR + review flow ===
    pr_split_strategy: str = Field(
        default="auto",
        description=(
            "Strategy used by :class:`~harness.agents.pr_split.SplitPlanner` "
            "to split a job's diff into N PR slices. One of:\n"
            "  - ``auto`` (default): if diff <= max_files_per_slice, return "
            "one slice (single-PR path); else fall back to ``directory``.\n"
            "  - ``files``: round-robin slices of at most ``max_files_per_slice`` "
            "files each.\n"
            "  - ``directory``: group by top-level directory prefix "
            "(e.g. ``src/*`` in one slice, ``tests/*`` in another).\n"
            "  - ``size``: balance by LOC (uses ``git diff --shortstat`` per file). "
            "Most expensive but most even.\n"
            "Override per-job via the CLI ``--split-strategy`` flag."
        ),
    )
    pr_split_max_files_per_slice: int = Field(
        default=10,
        ge=1,
        le=1000,
        description=(
            "Maximum number of files per slice for the ``files`` and "
            "``auto`` strategies. The default of 10 keeps each PR "
            "small and reviewable. Ignored by the ``directory`` strategy "
            "(which uses directory boundaries, not file counts)."
        ),
    )
    pr_split_min_slices: int = Field(
        default=1,
        ge=1,
        description=(
            "If the diff is smaller than ``min_slices * max_files_per_slice``, "
            "the planner collapses to a single slice (the legacy single-PR "
            "path). Default 1 — never split a small diff."
        ),
    )
    pr_split_max_slices: int = Field(
        default=8,
        ge=1,
        le=64,
        description=(
            "Hard cap on the number of slices in a stack. Prevents a "
            "user from requesting ``--split-into 100`` and overwhelming "
            "the GitHub API. Default 8 — the largest reasonable stacked PR."
        ),
    )
    pr_template_path: str = Field(
        default="",
        description=(
            "Optional path to a custom PR body template. The file "
            "should contain a Markdown template with ``{task}``, "
            "``{head_branch}``, ``{base_branch}``, ``{slice_index}``, "
            "``{slice_total}``, ``{stack_id}``, ``{issue_numbers}``, "
            "``{codeowners_reviewers}`` placeholders. Empty string = "
            "use the built-in default template "
            "(see ``harness/agents/templates/pr_body.md``)."
        ),
    )
    pr_issue_link_re: str = Field(
        default=r"#(\d+)",
        description=(
            "Regular expression (single capturing group) used to "
            "extract issue numbers from the job's task text. Default "
            "``#(\\d+)`` matches bare ``#123`` references. Operators "
            "can supply a more restrictive pattern (e.g. ``(?:Closes|"
            "Refs|Fixes) #(\\d+)``) to limit auto-linking to explicit "
            "phrases only."
        ),
    )
    pr_review_timeout_s: int = Field(
        default=86400,
        ge=60,
        description=(
            "Phase 2.4: how long the queue will wait for a PR review "
            "decision (``approved`` or ``changes_requested``) after "
            "the CI checks pass. Default 86400 (24 hours). After the "
            "timeout, the job is marked ``failed`` with "
            "``error='PR review timeout'``."
        ),
    )
    pr_review_poll_interval_s: int = Field(
        default=30,
        ge=5,
        le=600,
        description=(
            "Phase 2.4: polling interval for the review-state check "
            "(complement to the webhook-based short-circuit). Default "
            "30s. Webhooks short-circuit this loop when they arrive, "
            "so the interval only matters when webhooks are disabled "
            "or GitHub is slow to deliver them."
        ),
    )

    # === Phase 2.5: auto-add label + rate limit + outbound webhooks ===
    auto_add_label: bool = Field(
        default=True,
        description=(
            "Phase 2.5: when ``job.auto_merge=True``, automatically "
            "add the configured ``auto_merge_label`` to the PR via "
            "``gh pr edit --add-label`` immediately after "
            "``create_pr`` succeeds. This is what the Phase 2.3 docs "
            "promised but did not implement — branch protection "
            "rules that require this label can now enforce it. "
            "Disable to skip the label call (the label is then "
            "expected to be already present on the PR, e.g. via a "
            "GitHub Action). Default True (most setups want this)."
        ),
    )
    pr_rate_limit_max_retries: int = Field(
        default=5,
        ge=0,
        le=20,
        description=(
            "Phase 2.5: how many times ``_gh_with_retry`` will retry "
            "a ``gh`` subprocess that returned 403 or 429 (rate "
            "limited). After this many failed attempts, the call "
            "raises :class:`GHUnavailable` and the PR phase fails "
            "the same way as a missing ``gh`` binary. Set to 0 to "
            "disable retry entirely (Phase 2.4 behaviour). Default 5."
        ),
    )
    pr_rate_limit_initial_backoff_s: float = Field(
        default=2.0,
        ge=0.0,
        le=60.0,
        description=(
            "Phase 2.5: initial sleep (seconds) before the first "
            "retry. Subsequent retries multiply by 2 up to "
            "``pr_rate_limit_max_backoff_s``. If the ``gh`` stderr "
            "contains a ``Retry-After: N`` line (parsed via regex), "
            "we honor ``N`` instead of the exponential schedule. "
            "Default 2.0s — a good balance between responsiveness "
            "and not hammering GitHub."
        ),
    )
    pr_rate_limit_max_backoff_s: float = Field(
        default=60.0,
        ge=1.0,
        le=600.0,
        description=(
            "Phase 2.5: maximum sleep between retries. The "
            "exponential schedule ``initial * 2^attempt`` is capped "
            "at this value. Default 60.0s (one minute). With "
            "``initial=2.0`` and ``max=60.0`` the sequence is "
            "approximately 2, 4, 8, 16, 32, 60, 60, ... (5 retries "
            "by default)."
        ),
    )
    pr_rate_limit_jitter_s: float = Field(
        default=0.5,
        ge=0.0,
        le=10.0,
        description=(
            "Phase 2.5: random uniform jitter (seconds) added to "
            "each backoff sleep. Reduces the thundering-herd effect "
            "when many background jobs hit the same 429 burst. "
            "Default 0.5s. Set to 0 to disable (deterministic — "
            "useful in tests)."
        ),
    )
    outbound_webhook_urls: str = Field(
        default="",
        description=(
            "Phase 2.5: comma-separated list of HTTP(S) URLs that "
            "receive POST events for critical lifecycle moments "
            "(``merged``, ``failed``, ``stack_merged``, "
            "``pr_waiting_review``). Empty string (default) "
            "disables outbound entirely. Each URL is called with "
            "``Authorization: Bearer <outbound_webhook_token>`` "
            "and a JSON body mirroring the :class:`JobEvent` "
            "shape: ``{event, job_id, kind, ...payload}``. "
            "Failed deliveries (4xx/5xx/timeout) are retried up "
            "to ``outbound_webhook_max_retries`` times with "
            "exponential backoff; after exhaustion we log a "
            "warning but do NOT fail the underlying job. The "
            "intent is to integrate with Slack / Telegram / an "
            "internal dashboard without blocking the merge queue."
        ),
    )
    outbound_webhook_token: str = Field(
        default="",
        description=(
            "Phase 2.5: shared bearer token sent in the "
            "``Authorization`` header of every outbound webhook. "
            "The receiver is expected to validate it and reject "
            "unauthorized requests. Leave empty to send no "
            "``Authorization`` header (NOT recommended in "
            "production — anyone who can reach the URL can read "
            "the events). Phase 4 will replace this with HMAC "
            "signing."
        ),
    )
    outbound_webhook_timeout_s: float = Field(
        default=5.0,
        ge=0.5,
        le=60.0,
        description=(
            "Phase 2.5: per-request HTTP timeout (seconds) for the "
            "outbound webhook delivery. If the receiver is slower "
            "than this, the call is aborted and retried. Default "
            "5.0s — a slow downstream should not stall the merge "
            "queue."
        ),
    )
    outbound_webhook_max_retries: int = Field(
        default=3,
        ge=0,
        le=10,
        description=(
            "Phase 2.5: how many times a single outbound POST is "
            "retried on 4xx / 5xx / timeout. With ``max_retries=3`` "
            "the receiver gets up to 4 attempts (initial + 3). Set "
            "to 0 to fire-and-forget without retry. Default 3."
        ),
    )

    # === Phase 3: Compaction (sliding window + LLM summary) ===
    compaction_enabled: bool = Field(
        default=True,
        description=(
            "Phase 3: when True, ``ContextCompactor`` collapses long "
            "chat history before each LLM call via a sliding window "
            "plus an LLM-generated summary. Default True. Set False "
            "to disable compaction (e.g. when the model has a 200K "
            "context window and cost is not a concern)."
        ),
    )
    compaction_threshold_ratio: float = Field(
        default=0.75,
        gt=0.0,
        lt=1.0,
        description=(
            "Phase 3: trigger compaction when message tokens exceed "
            "this fraction of the model's context window. Default 0.75 "
            "(compact at 75% of ctx). The compactor trims the history "
            "to ``compaction_target_ratio`` of ctx afterwards."
        ),
    )
    compaction_target_ratio: float = Field(
        default=0.50,
        gt=0.0,
        lt=1.0,
        description=(
            "Phase 3: after compaction, target this fraction of the "
            "model's context window. Default 0.50 (50% of ctx) gives "
            "headroom for new turns before the next compact. Must be "
            "less than ``compaction_threshold_ratio``."
        ),
    )
    compaction_keep_recent_turns: int = Field(
        default=6,
        ge=2,
        le=64,
        description=(
            "Phase 3: minimum number of recent turns to keep verbatim "
            "regardless of token count. The sliding window never drops "
            "the last N user/assistant turns. Default 6 — enough for "
            "the LLM to maintain conversational coherence."
        ),
    )
    compaction_summarizer_model: str = Field(
        default="",
        description=(
            "Phase 3: model id used to summarise dropped turns. Empty "
            "string = ``settings.subagent_t1_model`` (Qwen3 8B local, "
            "free). The summarizer runs on the dropped turns only, "
            "so context overhead is bounded."
        ),
    )
    compaction_summarizer_fallback: str = Field(
        default="",
        description=(
            "Phase 3: model id to fall back to if the primary "
            "summarizer fails (timeout, error, unavailable). Empty "
            "string = ``settings.subagent_t2_model`` (cloud mid-tier). "
            "Set to a known-good model id to override the default."
        ),
    )
    compaction_summarizer_max_input_tokens: int = Field(
        default=0,
        ge=0,
        description=(
            "Phase 3: hard cap on the input size passed to the "
            "summarizer. 0 = auto (half of T1 model context = 16K "
            "for Qwen3 8B). Turns beyond the cap are dropped before "
            "the summariser call (sliding window already reduces "
            "size; this is the last-mile safety net)."
        ),
    )
    compaction_persist_to_memory: bool = Field(
        default=True,
        description=(
            "Phase 3: when True, the compaction summary is written to "
            "UnifiedMemory (L2 mem0) with tag ``#compact`` so it can be "
            "retrieved across sessions via semantic search. Default "
            "True. Set False for ephemeral (in-memory only) compaction."
        ),
    )

    # === Phase 3.5: Persistent compact store (3) ===
    compaction_persistent_store: bool = Field(
        default=True,
        description=(
            "Phase 3.5: when True, the compactor uses "
            "``CompactStore`` (SQLite) to cache compaction results by "
            "``(session_id, source_hash)``. On a cache hit the LLM "
            "summariser is skipped entirely (zero cost on reconnect). "
            "Default True. Set False to disable the persistent cache "
            "and use pure in-memory compaction (Phase 3 behavior)."
        ),
    )
    compaction_cache_max_versions: int = Field(
        default=5,
        ge=1,
        description=(
            "Phase 3.5: maximum number of compaction versions to "
            "retain per session in the persistent store. Older "
            "versions beyond this cap are pruned (not yet "
            "implemented — reserved for Phase 4 retention policy). "
            "Must be >= 1. Default 5."
        ),
    )
    compaction_audit_log: bool = Field(
        default=False,
        description=(
            "Phase 3.5: when True, every compaction event is "
            "appended to ``data/audit/compaction-YYYY-MM-DD.ndjson`` "
            "with a JSON line per event. Mirrors the Phase 3 "
            "``redaction_audit_log`` pattern. Default False "
            "(disabled — enable for compliance / debugging)."
        ),
    )

    # === Phase 3 v1.2.0: Scratchpad (Write context) ===
    scratchpad_enabled: bool = Field(
        default=True,
        description=(
            "Phase 3 v1.2.0: when True, the agent runtime exposes "
            "4 scratchpad tools (``scratchpad_write_note`` / "
            "``scratchpad_read_notes`` / ``scratchpad_plan_step`` / "
            "``scratchpad_mark_done``) bound to a per-``(session_id, "
            "agent_id)`` ``ScratchpadStore``. Default True. Set "
            "False to disable the scratchpad feature entirely "
            "(tools return a graceful error when invoked)."
        ),
    )
    scratchpad_max_notes_per_session: int = Field(
        default=100,
        ge=1,
        description=(
            "Phase 3 v1.2.0: hard cap on the total number of notes "
            "(L0+L1+L2) for a single ``(session_id, agent_id)`` pair. "
            "Older rows beyond the cap are pruned on insert. "
            "Default 100. Must be >= 1."
        ),
    )
    scratchpad_l0_max_bytes: int = Field(
        default=1024,
        ge=128,
        description=(
            "Phase 3 v1.2.0: maximum total size of L0 notes in bytes. "
            "L0 is the hot layer injected into the system prompt on "
            "every turn; keep tight. Default 1024 (1KB). Must be "
            ">= 128. Writes / promotes that would push the L0 total "
            "over the cap trigger an auto-prune of the oldest L0 row; "
            "single notes larger than the cap are rejected."
        ),
    )
    scratchpad_audit_log: bool = Field(
        default=False,
        description=(
            "Phase 3 v1.2.0: when True, every scratchpad event "
            "(``write`` / ``read`` / ``promote`` / ``plan_step`` / "
            "``mark_done`` / ``l0_cap_exceeded``) is appended to "
            "``data/audit/scratchpad-YYYY-MM-DD.ndjson``. Default "
            "False (opt-in — enable for compliance / debugging)."
        ),
    )

    # === Phase 3: Embeddings (ONNX local) ===
    embeddings_dir: Path = Field(
        default=PROJECT_ROOT / "models" / "embeddings",
        description=(
            "Phase 3: directory where ONNX embedding models are "
            "cached. Default ``<project_root>/models/embeddings``. "
            "Override with ``HARNESS_EMBEDDINGS_DIR`` to share a "
            "single cache across projects (or to point at an "
            "existing HuggingFace cache)."
        ),
    )
    embedding_model: str = Field(
        default="intfloat/multilingual-e5-small",
        description=(
            "Phase 3: default ONNX model id used by ``OnnxEmbedder``. "
            "Multilingual (RU+EN), 384-dim, 118M params, ~120MB on disk. "
            "Override to swap models; stored vectors are tagged with "
            "``EMBEDDING_MODEL_VERSION`` so version drift is detected."
        ),
    )
    embedding_precision: Literal["fp32", "int8"] = Field(
        default="int8",
        description=(
            "Phase 3: numeric precision for the ONNX model. ``int8`` "
            "(default) is ~30 MB on disk and ~30ms per query on CPU. "
            "``fp32`` is ~120 MB and ~50ms per query but slightly "
            "higher recall. Phase 3 default favours the smaller "
            "footprint for operators running on laptop hardware."
        ),
    )
    embedding_dim: int = Field(
        default=384,
        ge=64,
        le=4096,
        description=(
            "Phase 3: embedding vector dimension. Must match the "
            "model's output dim. Default 384 = ``multilingual-e5-small``. "
            "Stored in ``Memory.metadata.embedding_dim`` for schema "
            "validation when vectors are loaded back from the L4 file "
            "mirror."
        ),
    )

    # === Phase 3: Privacy (pre-LLM redaction) ===
    redaction_enabled: bool = Field(
        default=True,
        description=(
            "Phase 3: when True, all 9 sink points (LLM messages, PR "
            "title/body, commit msg, branch name, JobStore prompt, "
            "outbound webhooks, .env read_file, inbound webhooks) run "
            "PII / secret redaction before persistence or external "
            "transmission. Default True (opt-out — safe baseline for "
            "an open-source tool). Set False only for tests or "
            "isolated offline use."
        ),
    )
    redaction_categories: str = Field(
        default="",
        description=(
            "Phase 3: comma-separated list of redaction categories to "
            "enable. Empty string = all 12 default categories "
            "(email, phone, IPv4, GitHub PAT variants, AWS keys, "
            "OpenAI/Anthropic keys, .env assignments, JWT, PEM, "
            "Slack tokens). Use this to narrow the pattern set on a "
            "per-deployment basis without editing code."
        ),
    )
    redaction_audit_log: bool = Field(
        default=False,
        description=(
            "Phase 3: when True, every redaction event is mirrored to "
            "``data/audit/redaction-YYYY-MM-DD.ndjson`` (append-only, "
            "rotated daily) and to the JobStore event log (kind="
            "\"redaction\"). Default False (no audit overhead in "
            "production). Enable for compliance / forensic review."
        ),
    )

    @model_validator(mode="after")
    def _cascade_thresholds_ordered(self) -> "Settings":
        """Guard against a misconfigured cascade + Phase 2.4 split strategy.

        Validates:

          - ``subagent_confidence_low < subagent_confidence_high`` (Phase 2.1)
            No confidence value would fall in the [low, high) T2 band and the
            cascade would degenerate to a binary T1/T3.
          - ``pr_strategy ∈ {auto, strict, off}`` (Phase 2.2)
          - ``auto_merge_method ∈ {squash, merge, rebase}`` (Phase 2.3)
          - ``pr_split_strategy ∈ {auto, files, directory, size}`` (Phase 2.4)
          - ``pr_split_min_slices <= pr_split_max_slices`` (Phase 2.4)

        We reject the configuration at load time so the user notices
        immediately, not on the first router call.
        """
        if self.subagent_confidence_low >= self.subagent_confidence_high:
            raise ValueError(
                f"subagent_confidence_low ({self.subagent_confidence_low}) must be "
                f"< subagent_confidence_high ({self.subagent_confidence_high})"
            )
        if self.pr_strategy not in ("auto", "strict", "off"):
            raise ValueError(
                f"pr_strategy must be one of 'auto' / 'strict' / 'off', "
                f"got {self.pr_strategy!r}"
            )
        if self.auto_merge_method not in ("squash", "merge", "rebase"):
            raise ValueError(
                f"auto_merge_method must be one of 'squash' / 'merge' / 'rebase', "
                f"got {self.auto_merge_method!r}"
            )
        if self.pr_split_strategy not in ("auto", "files", "directory", "size"):
            raise ValueError(
                f"pr_split_strategy must be one of 'auto' / 'files' / "
                f"'directory' / 'size', got {self.pr_split_strategy!r}"
            )
        if self.pr_split_min_slices > self.pr_split_max_slices:
            raise ValueError(
                f"pr_split_min_slices ({self.pr_split_min_slices}) must be "
                f"<= pr_split_max_slices ({self.pr_split_max_slices})"
            )
        # Phase 2.5: rate-limit backoff must be in increasing order.
        if self.pr_rate_limit_initial_backoff_s > self.pr_rate_limit_max_backoff_s:
            raise ValueError(
                f"pr_rate_limit_initial_backoff_s "
                f"({self.pr_rate_limit_initial_backoff_s}) must be <= "
                f"pr_rate_limit_max_backoff_s ({self.pr_rate_limit_max_backoff_s})"
            )
        # Phase 3: compaction ratios must be sane (target < threshold).
        if self.compaction_enabled:
            if self.compaction_target_ratio >= self.compaction_threshold_ratio:
                raise ValueError(
                    f"compaction_target_ratio "
                    f"({self.compaction_target_ratio}) must be < "
                    f"compaction_threshold_ratio "
                    f"({self.compaction_threshold_ratio})"
                )
            # Phase 3.5: cache_max_versions must be sane (>= 1) when
            # the persistent store is enabled. Pydantic's ``ge=1``
            # field constraint already catches the lower bound; the
            # explicit guard below keeps the error message
            # context-specific and self-documenting.
            if self.compaction_persistent_store:
                if self.compaction_cache_max_versions < 1:
                    raise ValueError(
                        f"compaction_cache_max_versions "
                        f"({self.compaction_cache_max_versions}) must be >= 1"
                    )
        # Phase 3: precision must be one of the supported literals
        # (Pydantic enforces this via Literal type — explicit guard
        # kept for clarity in error messages).
        if self.embedding_precision not in ("fp32", "int8"):
            raise ValueError(
                f"embedding_precision must be 'fp32' or 'int8', "
                f"got {self.embedding_precision!r}"
            )
        return self


settings = Settings()
