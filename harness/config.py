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
    scratchpad_inject_l0_to_system_prompt: bool = Field(
        default=True,
        description=(
            "Phase 3 v1.2.1: when True (default), the runner reads L0 "
            "notes from the scratchpad on every ``run`` / ``stream`` "
            "call and prepends them to the system prompt as a "
            "``## Hot context (L0 notes — this session, "
            "auto-injected)`` section. This is the ``L0``-layer "
            "fulfilment of the Anthropic \"Write context\" strategy: "
            "hot facts / plan / state are visible to the model "
            "without needing an extra ``scratchpad_read_notes`` tool "
            "round-trip. Set False to fall back to v1.2.0 behaviour "
            "(LLM must call ``scratchpad_read_notes`` to consult L0)."
        ),
    )
    scratchpad_l2_qdrant_url: str | None = Field(
        default=None,
        description=(
            "Phase 3 v1.3.0: optional Qdrant server URL (e.g. "
            "``http://localhost:6333``) for L2 note embeddings. When "
            "set AND the server is reachable, L2 notes are stored in "
            "a dedicated Qdrant collection for dense+BM25 hybrid "
            "retrieval. When ``None`` (default) OR the server is "
            "unreachable, the harness falls back to the in-SQLite "
            "``SqliteL2Store`` (vector column in ``scratchpad_notes`` "
            "as BLOB) — no new required dependencies, works offline."
        ),
    )
    scratchpad_l2_qdrant_collection: str = Field(
        default="scratchpad_l2",
        description=(
            "Phase 3 v1.3.0: Qdrant collection name for L2 note "
            "embeddings. Default ``scratchpad_l2``. Override to "
            "share a single Qdrant instance across multiple Harness "
            "deployments (one collection per environment)."
        ),
    )

    # === Phase 3 v1.3.1: Tool result offload (>25k tokens) ===
    tool_offload_enabled: bool = Field(
        default=True,
        description=(
            "Phase 3 v1.3.1: when True, tool results that exceed "
            "``tool_offload_threshold_bytes`` are persisted to L2 "
            "scratchpad and replaced with a small stub in the message "
            "history. The LLM can pull the full body via "
            "``scratchpad_read_offloaded(id=N)`` or search across "
            "offloaded content via ``scratchpad_search_offloaded(query)``. "
            "Set False to disable offload entirely (the full content "
            "is kept inline, which can blow past the context window "
            "for large tool results)."
        ),
    )
    tool_offload_threshold_bytes: int = Field(
        default=25600,
        ge=1024,
        description=(
            "Phase 3 v1.3.1: minimum byte count to trigger offload. "
            "Default 25600 (25 KB) — matches the Anthropic "
            "context-engineering playbook's '>25k tokens' rule of "
            "thumb. Lower for stricter offload (preserves chat "
            "budget), higher for fewer offloads (preserves the "
            "inline preview)."
        ),
    )
    tool_offload_preview_lines: int = Field(
        default=3,
        ge=1,
        le=20,
        description=(
            "Phase 3 v1.3.1: number of non-empty lines from the "
            "original tool output to include in the stub preview. "
            "Default 3 — gives the LLM enough context to decide "
            "whether to fetch the full body without spending more "
            "than ~600 chars of the message budget."
        ),
    )
    tool_offload_preview_max_chars: int = Field(
        default=600,
        ge=64,
        le=4096,
        description=(
            "Phase 3 v1.3.1: hard cap on the stub preview size in "
            "characters. Default 600. Combined with "
            "``tool_offload_preview_lines`` (3) this bounds the "
            "stub to ~3 lines × ~200 chars/line, well below the "
            "25 KB threshold that triggered the offload."
        ),
    )
    tool_offload_read_max_bytes: int = Field(
        default=4096,
        ge=256,
        description=(
            "Phase 3 v1.3.1: default chunk size for "
            "``scratchpad_read_offloaded`` when the LLM does not "
            "pass an explicit ``max_bytes``. Default 4096 (4 KB). "
            "The LLM can request larger chunks by passing "
            "``max_bytes`` explicitly."
        ),
    )
    tool_offload_max_ms: int = Field(
        default=2000,
        ge=100,
        le=60000,
        description=(
            "Phase 3 v1.3.1: per-call timeout (milliseconds) for the "
            "offload write. When exceeded the loop keeps the full "
            "tool content inline (fail-open) rather than blocking "
            "the chat. Default 2000 (2 seconds) — most offloads "
            "complete in <100 ms on a local SQLite store, the cap "
            "is only relevant for very large writes or contended "
            "disks."
        ),
    )

    # === Phase 3 v1.4.0: Reflection loop (end-of-session lesson extraction) ===
    reflection_enabled: bool = Field(
        default=True,
        description=(
            "Phase 3 v1.4.0: when True, ``SessionLifecycle.__aexit__`` "
            "calls ``ReflectionLoop.reflect(events)`` to extract "
            "structured lessons from the session's event log. "
            "Disable to skip reflection entirely (useful for "
            "short-lived CI jobs that don't need lesson extraction)."
        ),
    )
    reflection_max_lessons: int = Field(
        default=5,
        ge=1,
        le=20,
        description=(
            "Phase 3 v1.4.0: maximum number of lessons to extract "
            "per session. Caps the LLM's response size. Default 5 — "
            "enough for typical end-of-session extraction, not so "
            "many that the response overwhelms the prompt."
        ),
    )
    reflection_max_ms: int = Field(
        default=10000,
        ge=100,
        le=60000,
        description=(
            "Phase 3 v1.4.0: per-call timeout (milliseconds) for the "
            "reflection extraction. When exceeded the lifecycle "
            "fires the next steps (session close) without lessons "
            "(fail-open). Default 10000 (10 seconds) — a small T1 "
            "model typically completes in <2s, the cap is only "
            "relevant for contended providers."
        ),
    )
    reflection_model: str = Field(
        default="",
        description=(
            "Phase 3 v1.4.0: primary reflection model id. Empty "
            "string = fall back to ``subagent_t1_model`` (default "
            "``qwen3:8b`` local). Set to a cloud model id (e.g. "
            "``glm-4.7``) if you want reflection to skip the local "
            "tier and go straight to a faster cloud summariser."
        ),
    )
    reflection_fallback_model: str = Field(
        default="",
        description=(
            "Phase 3 v1.4.0: fallback reflection model id. Empty "
            "string = fall back to ``subagent_t2_model`` (default "
            "``glm-4.7``). Used when ``reflection_model`` is "
            "unavailable or returns an error. Mirrors the compactor's "
            "summariser/fallback cascade at ``compaction.py:201-208``."
        ),
    )

    # === Phase 3 v1.4.0: Manual /compact (force-compact via CLI / HTTP / WS) ===
    manual_compact_max_ms: int = Field(
        default=30000,
        ge=1000,
        le=120000,
        description=(
            "Phase 3 v1.4.0: per-call timeout (milliseconds) for the "
            "manual ``/compact`` invocation. Larger than "
            "``reflection_max_ms`` because summarisation is the "
            "expensive step. Default 30000 (30 seconds). When "
            "exceeded the call returns a partial result (what "
            "the cache had) — the chat loop is not blocked."
        ),
    )

    # === Phase 3 v1.4.0: Prompt caching (Anthropic cache_control injection) ===
    prompt_cache_enabled: bool = Field(
        default=True,
        description=(
            "Phase 3 v1.4.0: master switch for prompt caching. "
            "When True AND ``prompt_cache_strategy`` matches the "
            "current model, ``LLMRouter`` injects ``cache_control`` "
            "markers (Anthropic) or relies on the provider's prefix "
            "caching (vLLM). Default True — caching is a pure "
            "cost/latency optimisation, no functional change when "
            "the provider ignores the markers."
        ),
    )
    prompt_cache_strategy: Literal["anthropic", "vllm", "off"] = Field(
        default="off",
        description=(
            "Phase 3 v1.4.0: which prompt-caching strategy to apply. "
            "``anthropic`` injects ``cache_control: {type: ephemeral}`` "
            "on the system message and last 2 turns when the model "
            "id starts with ``anthropic/``. ``vllm`` is a no-op at "
            "the call site — vLLM prefix caching is enabled at the "
            "engine level (operator must configure vLLM externally); "
            "we just keep the setting here for visibility. ``off`` "
            "disables caching entirely. Default ``off`` — operators "
            "opt in by setting the env var ``HARNESS_PROMPT_CACHE_STRATEGY``."
        ),
    )

    # === Phase 3 v1.5.0: Privacy zones (path-based defence) ===
    # Path-based privacy filter applied at tool sinks (read_file/grep/glob
    # in Step 3, scratchpad in Step 4 Tier 2). See :mod:`harness.privacy`
    # for the implementation. Content-level redaction (12 patterns) is
    # orthogonal and lives in :mod:`harness.redaction`.
    privacy_zones_enabled: bool = Field(
        default=True,
        description=(
            "Phase 3 v1.5.0: master switch for path-based privacy zones. "
            "``False`` → :class:`harness.privacy.PrivacyZoneFilter` is a "
            "no-op (``check()`` always returns ``(\"allow\", None)``). "
            "Server-wide kill switch via env var "
            "``HARNESS_PRIVACY_ZONES_ENABLED=false``."
        ),
    )
    privacy_zone_patterns: str = Field(
        default="",
        description=(
            "Phase 3 v1.5.0: comma-separated list of glob patterns. "
            "Empty string → use built-in defaults "
            "``['private/**', '*.env', '.env/**', 'secrets/**', "
            "``'``_credentials/**', '.ssh/**']``. Override per-pattern "
            "via ``privacy_zone_per_action``. Glob syntax matches "
            ":mod:`harness.privacy.path_match` (``*``, ``**``, ``?``, "
            "anchored ``/``, trailing ``/``, negation ``!``)."
        ),
    )
    privacy_zone_default_action: Literal["block", "redact", "skip"] = Field(
        default="block",
        description=(
            "Phase 3 v1.5.0: fallback action for patterns without an "
            "explicit override in ``privacy_zone_per_action``. ``block`` "
            "= return error to LLM (most conservative), ``redact`` = "
            "replace content with ``[PRIVATE: <reason>]`` placeholder, "
            "``skip`` = silent skip (return empty result)."
        ),
    )
    privacy_zone_per_action: str = Field(
        default="",
        description=(
            "Phase 3 v1.5.0: per-pattern action overrides. Format: "
            "``\"private/**=redact,secrets/*=block\"``. Comma-separated "
            "``pattern=action`` pairs. Actions must be one of ``block``, "
            "``redact``, ``skip``. Empty string → all patterns use "
            "``privacy_zone_default_action``."
        ),
    )
    privacy_zones_audit_log: bool = Field(
        default=False,
        description=(
            "Phase 3 v1.5.0: emit ``privacy_zone_blocked`` / "
            "``privacy_zone_redacted`` / ``privacy_zone_skipped`` events "
            "to :class:`harness.context.scratchpad_audit.ScratchpadAudit` "
            "on every non-``allow`` decision. Off by default — operators "
            "opt in via ``HARNESS_PRIVACY_ZONES_AUDIT_LOG=true``."
        ),
    )

    # === Phase 3 v1.5.0: Pre-compaction hook ===
    # Async callback fired BEFORE ``ContextCompactor._run_slow_path`` to "
    # save high-signal state (recent messages, plan, hot L0) for "
    # resume after /compact. Step 4 wires the default implementation."
    pre_compact_enabled: bool = Field(
        default=True,
        description=(
            "Phase 3 v1.5.0: enable the pre-compaction hook. ``False`` "
            "→ no state snapshot is saved before compaction. Server-wide "
            "kill switch via env var ``HARNESS_PRE_COMPACT_ENABLED=false``."
        ),
    )
    pre_compact_max_ms: int = Field(
        default=5000,
        ge=1,
        description=(
            "Phase 3 v1.5.0: per-call timeout for the pre-compaction "
            "hook (milliseconds). On timeout, the hook is skipped "
            "(fail-open) and compaction proceeds. Default 5000ms = 5s."
        ),
    )
    pre_compact_save_fields: str = Field(
        default="messages_last_n,plan_step,hot_l0,metadata",
        description=(
            "Phase 3 v1.5.0: comma-separated list of state fields to "
            "capture in the pre-compact snapshot. Valid fields: "
            "``messages_last_n`` (last 5 user/assistant), ``plan_step`` "
            "(current scratchpad plan step), ``hot_l0`` (scratchpad L0 "
            "snapshot), ``metadata`` (tokens/turns/last_compact_at). "
            "Empty string → save nothing (hook becomes no-op)."
        ),
    )

    # === Phase 3 v1.5.0: Time-based compaction trigger ===
    # Hybrid trigger complementing the existing token-based threshold. "
    # Step 5 implements the TimeBasedCompactionTrigger class."
    compaction_trigger: Literal["token", "turn", "time", "hybrid"] = Field(
        default="token",
        description=(
            "Phase 3 v1.5.0: which trigger to use for auto-compaction. "
            "``token`` = existing token-threshold behaviour (backward "
            "compat default). ``turn`` = fire every N user turns. "
            "``time`` = fire after N minutes of inactivity. ``hybrid`` "
            "= OR semantics — first trigger wins. Change via env var "
            "``HARNESS_COMPACTION_TRIGGER=turn``."
        ),
    )
    compaction_turn_interval: int = Field(
        default=20,
        ge=1,
        description=(
            "Phase 3 v1.5.0: user turns between compactions when "
            "``compaction_trigger in {\"turn\", \"hybrid\"}``. "
            "Default 20 — long enough to amortise compaction cost, "
            "short enough to keep context fresh."
        ),
    )
    compaction_time_idle_minutes: int = Field(
        default=30,
        ge=1,
        description=(
            "Phase 3 v1.5.0: minutes of session inactivity before "
            "firing time-based compaction when "
            "``compaction_trigger in {\"time\", \"hybrid\"}``. Default "
            "30 — long enough to ignore brief pauses, short enough to "
            "prevent stale context on resume."
        ),
    )

    # === Phase 4.0: Hooks framework (events + transports) ===
    hooks_enabled: bool = Field(
        default=True,
        description=(
            "Phase 4.0: master switch for the entire hooks framework. "
            "When False, no hooks fire and the registry is bypassed. "
            "Per-event enables are ignored when this is False. Default True."
        ),
    )
    hooks_default_max_ms: int = Field(
        default=3000,
        ge=100,
        le=60000,
        description=(
            "Phase 4.0: per-hook timeout in milliseconds. Applied via "
            "``asyncio.wait_for``. On timeout, the runner returns "
            "``decision='allow'`` (fail-open) and logs a warning. "
            "Default 3000ms (3s)."
        ),
    )
    hooks_max_per_event: int = Field(
        default=10,
        ge=1,
        le=100,
        description=(
            "Phase 4.0: maximum number of hooks that can be registered "
            "for a single event. Excess hooks are silently dropped with "
            "a warning. Default 10."
        ),
    )
    hooks_max_recursion_depth: int = Field(
        default=3,
        ge=1,
        le=10,
        description=(
            "Phase 4.0: maximum recursion depth for hooks (e.g. "
            "``OnMemoryWrite`` fires from inside a hook that writes "
            "memory). Depth-bounded to prevent infinite loops. Default 3."
        ),
    )
    hooks_subprocess_specs: str = Field(
        default="",
        description=(
            "Phase 4.0: comma-separated hook specs for subprocess "
            "transport. Format: ``<EventType>:subprocess:<path>[:<timeout_ms>]``. "
            "Empty = no subprocess hooks."
        ),
    )
    hooks_http_specs: str = Field(
        default="",
        description=(
            "Phase 4.0: comma-separated hook specs for HTTP transport. "
            "Format: ``<EventType>:http:<url>[:<timeout_ms>][:<auth>]``. "
            "Empty = no HTTP hooks."
        ),
    )
    hooks_llm_specs: str = Field(
        default="",
        description=(
            "Phase 4.0: comma-separated hook specs for LLM-as-hook "
            "transport. Format: "
            "``<EventType>:llm:<model>:<timeout_ms>:<prompt>``. "
            "Empty = no LLM hooks. Note: LLM hooks add latency + cost; "
            "use sparingly (e.g. for hard-to-formalise decisions)."
        ),
    )
    hooks_filter_chain: str = Field(
        default="",
        description=(
            "Phase 4.0: comma-separated match_glob filters applied to "
            "ALL events. Format: ``<field>=<pattern>``. Example: "
            "``session_id=*-prod,tool_name=!rm``. Empty = no global filter. "
            "Per-hook matchers take precedence."
        ),
    )
    hooks_fail_open: bool = Field(
        default=True,
        description=(
            "Phase 4.0: when True, a hook timeout or exception is treated "
            "as ``decision='allow'`` (the operation proceeds). Set False "
            "to fail-closed (the operation is blocked). Default True "
            "(safer for chat loop)."
        ),
    )
    hooks_redact_payloads: bool = Field(
        default=True,
        description=(
            "Phase 4.0: when True, hook payloads are redacted via "
            "RedactionEngine BEFORE being passed to any hook (builtin, "
            "subprocess, http, llm). PII / secrets never leave the trust "
            "boundary. Default True."
        ),
    )
    hooks_audit_log: bool = Field(
        default=False,
        description=(
            "Phase 4.0: when True, every hook decision is written to "
            "``<project_root>/data/audit/hooks-YYYY-MM-DD.ndjson`` "
            "(append-only, rotated daily). Default False (no audit "
            "overhead in production). Enable for compliance / forensic "
            "review."
        ),
    )
    hooks_subprocess_allowed_paths: str = Field(
        default=".harness/hooks/**",
        description=(
            "Phase 4.0: glob pattern for allowed subprocess hook script "
            "paths. Scripts outside the pattern are rejected at "
            "registration time. Default ``.harness/hooks/**`` (project-local)."
        ),
    )

    # === Phase 4.2: Hot-reload ===
    hot_reload_enabled: bool = Field(
        default=True,
        description=(
            "Phase 4.2: master switch for hot-reload of .harness/agents/*.md "
            "and .harness/hooks/*.json. Default True in dev. Set False in "
            "production for stability."
        ),
    )
    hot_reload_debounce_ms: int = Field(
        default=200, ge=0, le=5000,
        description=(
            "Phase 4.2: debounce window for file changes. Multiple changes "
            "arriving within this window are batched into a single reload "
            "event. 200ms = sweet spot for editor saves."
        ),
    )
    hot_reload_poll_interval_s: float = Field(
        default=1.0, gt=0, le=60.0,
        description=(
            "Phase 4.2: polling interval (seconds) for the polling "
            "fallback path. Only used if watchfiles is not installed."
        ),
    )
    hooks_on_memory_write_silent_layers: str = Field(
        default="L1",
        description=(
            "Phase 4.0: comma-separated memory layers whose writes do "
            "NOT fire ``OnMemoryWrite`` hooks (e.g. hand-curated layers "
            "where audit volume is undesirable). Default ``L1`` (hmem)."
        ),
    )
    hooks_on_compaction_skip_cache_hit: bool = Field(
        default=True,
        description=(
            "Phase 4.0: when True, ``OnCompaction`` is NOT fired on "
            "compaction cache hits (returns the cached summary). Set "
            "False to fire on every compaction. Default True (reduces "
            "audit volume)."
        ),
    )

    # === Per-event enable (15 settings) ===
    hooks_pre_tool_use_enabled: bool = Field(default=True, description="Phase 4.0: enable PreToolUse event.")
    hooks_post_tool_use_enabled: bool = Field(default=True, description="Phase 4.0: enable PostToolUse event.")
    hooks_stop_enabled: bool = Field(default=True, description="Phase 4.0: enable Stop event.")
    hooks_subagent_start_enabled: bool = Field(default=True, description="Phase 4.0: enable SubagentStart event.")
    hooks_subagent_stop_enabled: bool = Field(default=True, description="Phase 4.0: enable SubagentStop event.")
    hooks_session_start_enabled: bool = Field(default=True, description="Phase 4.0: enable SessionStart event.")
    hooks_session_end_enabled: bool = Field(default=True, description="Phase 4.0: enable SessionEnd event.")
    hooks_user_prompt_submit_enabled: bool = Field(default=True, description="Phase 4.0: enable UserPromptSubmit event.")
    hooks_pre_compact_enabled: bool = Field(default=True, description="Phase 4.0: enable PreCompact event.")
    hooks_instructions_loaded_enabled: bool = Field(default=True, description="Phase 4.0: enable InstructionsLoaded event.")
    hooks_permission_request_enabled: bool = Field(default=True, description="Phase 4.0: enable PermissionRequest event.")
    hooks_on_memory_write_enabled: bool = Field(default=True, description="Phase 4.0: enable OnMemoryWrite event.")
    hooks_on_routing_decision_enabled: bool = Field(default=True, description="Phase 4.0: enable OnRoutingDecision event.")
    hooks_on_compaction_enabled: bool = Field(default=True, description="Phase 4.0: enable OnCompaction event.")
    # Phase 4.3: Elicitation + Notification events.
    hooks_elicitation_enabled: bool = Field(default=True, description="Phase 4.3: enable Elicitation event (interactive prompts).")
    hooks_notification_enabled: bool = Field(default=True, description="Phase 4.3: enable Notification event (fire-and-forget push).")

    # === Builtin hook enable (7 settings) ===
    hooks_builtin_log_enabled: bool = Field(default=True, description="Phase 4.0: enable builtin LogHook.")
    hooks_builtin_validate_enabled: bool = Field(default=True, description="Phase 4.0: enable builtin ValidateHook.")
    hooks_builtin_block_dangerous_enabled: bool = Field(default=True, description="Phase 4.0: enable builtin BlockDangerousHook.")
    hooks_builtin_inject_context_enabled: bool = Field(default=False, description="Phase 4.0: enable builtin InjectContextHook (off by default — L0 already injected via Phase 3 v1.2.1).")
    hooks_builtin_autosave_enabled: bool = Field(default=True, description="Phase 4.0: enable builtin AutosaveHook.")
    # Phase 4.3: 2 new builtin hooks for Elicitation + Notification.
    hooks_builtin_confirm_dangerous_enabled: bool = Field(default=True, description="Phase 4.3: enable builtin ConfirmDangerousHook (Elicitation default answer injector).")
    hooks_builtin_notify_terminal_enabled: bool = Field(default=True, description="Phase 4.3: enable builtin NotifyTerminalHook (Notification fanout to stderr).")
    # Phase 4.3+ v1.11.0: webhook + desktop channels for Notification.
    hooks_notify_webhook_url: str = Field(default="", description="Phase 4.3+: URL to POST Notification events to (empty = webhook channel disabled).")
    hooks_notify_webhook_secret: str = Field(default="", description="Phase 4.3+: HMAC-SHA256 secret for X-Harness-Signature header (empty = no signature).")
    hooks_notify_webhook_timeout_s: float = Field(default=5.0, description="Phase 4.3+: webhook POST timeout in seconds.")
    hooks_notify_desktop_enabled: bool = Field(default=False, description="Phase 4.3+: enable desktop channel for Notification (opt-in — uses PowerShell msg/osascript/notify-send).")
    # Phase 4.3+ v1.12.0: WebSocket interactive transport for Elicitation.
    hooks_elicitation_ws_enabled: bool = Field(default=True, description="Phase 4.3+: enable ElicitationBroker + /api/v1/elicitation/ws endpoint (default True; disable for headless deployments).")
    hooks_elicitation_ws_timeout_s: float = Field(default=30.0, description="Phase 4.3+: how long to wait for a human answer before falling back to default_answer.")
    # Phase 4.3+ v1.15.0: HTTP long-poll fallback for Elicitation. Used
    # when the WebSocket transport is unavailable (corporate firewall,
    # reverse proxy without WS upgrade, mobile carrier with aggressive
    # connection recycling). Default False — WS-first policy; operators
    # opt in by setting HARNESS_HOOKS_ELICITATION_LONGPOLL_ENABLED=true.
    hooks_elicitation_longpoll_enabled: bool = Field(
        default=False,
        description=(
            "Phase 4.3+ v1.15.0: enable HTTP long-poll fallback for "
            "Elicitation (/api/v1/elicitation/poll + /answer). Default "
            "False — WS is the primary transport; this is the fallback "
            "for environments where WS is unavailable. When False, the "
            "long-poll endpoints return 403."
        ),
    )
    hooks_elicitation_longpoll_timeout_s: float = Field(
        default=30.0,
        description=(
            "Phase 4.3+ v1.15.0: max seconds a GET /poll request will "
            "block waiting for a pending question before returning an "
            "empty body / 404. Default 30s (matches the broker timeout)."
        ),
    )
    hooks_elicitation_longpoll_interval_s: float = Field(
        default=0.25,
        description=(
            "Phase 4.3+ v1.15.0: polling interval (seconds) for the "
            "internal broker.pending() check loop inside the long-poll "
            "endpoint. Lower = snappier response at higher CPU cost."
        ),
    )

    # === Phase 4.1: Observability — master switches ===
    observability_enabled: bool = Field(
        default=True,
        description=(
            "Phase 4.1: master switch. False → all observability is no-op. "
            "Mirrors hooks_enabled pattern (Phase 4.0)."
        ),
    )
    observability_jsonl_enabled: bool = Field(
        default=True,
        description=(
            "Phase 4.1: write structured JSONL logs to data/logs/. "
            "Default True (cheap, ~1ms per log line). Thread-safe."
        ),
    )
    observability_prometheus_enabled: bool = Field(
        default=False,
        description=(
            "Phase 4.1: enable /metrics endpoint. Default OFF (zero overhead). "
            "Set True for production deployments with Prometheus scrape."
        ),
    )
    observability_otlp_enabled: bool = Field(
        default=False,
        description=(
            "Phase 4.1: export spans via OTLP. Default OFF (requires OTel SDK "
            "extras + collector endpoint). No-op if SDK not installed."
        ),
    )

    # === JSONL logger ===
    observability_log_dir: Path = Field(
        default=PROJECT_ROOT / "data" / "logs",
        description=(
            "Phase 4.1: directory for harness-YYYY-MM-DD.jsonl files. "
            "Rotated daily at midnight (date suffix in filename)."
        ),
    )
    observability_log_max_files: int = Field(
        default=30, ge=1, le=365,
        description=(
            "Phase 4.1: max retained rotated log files. Older files are deleted "
            "by a background task (once per hour). 30 = ~1 month retention."
        ),
    )
    observability_log_max_file_size_mb: int = Field(
        default=100, ge=1, le=1024,
        description=(
            "Phase 4.1: rotate file by size (in addition to daily rotation). "
            "If a single file exceeds this, rotate early. 0 = size-based disabled."
        ),
    )

    # === Prometheus ===
    observability_metrics_path: str = Field(
        default="/metrics",
        description="Phase 4.1: path for Prometheus scrape. Standard is /metrics.",
    )
    observability_metrics_namespace: str = Field(
        default="harness",
        description="Phase 4.1: metric name prefix. All metrics start with this.",
    )

    # === OpenTelemetry ===
    observability_otlp_endpoint: str = Field(
        default="",
        description=(
            "Phase 4.1: OTLP collector endpoint (e.g. http://localhost:4317). "
            "Empty = no OTLP export."
        ),
    )
    observability_otlp_headers: str = Field(
        default="",
        description=(
            "Phase 4.1: OTLP headers (comma-separated key=value). "
            "E.g. 'api-key=abc123,x-source=harness'."
        ),
    )
    observability_trace_sample_ratio: float = Field(
        default=1.0, ge=0.0, le=1.0,
        description=(
            "Phase 4.1: trace sampling ratio. 1.0 = sample every request. "
            "0.1 = sample 10% (reduce collector load)."
        ),
    )

    # === Deep health ===
    observability_health_ready_timeout_s: float = Field(
        default=2.0, gt=0, le=30.0,
        description=(
            "Phase 4.1: per-probe timeout for /health/ready. "
            "Default 2s. If a DB takes >2s to respond, mark as timeout."
        ),
    )
    observability_health_deep_timeout_s: float = Field(
        default=5.0, gt=0, le=60.0,
        description=(
            "Phase 4.1: total timeout for /health/deep. "
            "Default 5s. Sum of all probes (sqlite+qdrant+neo4j+queue+...)."
        ),
    )
    observability_health_require_qdrant: bool = Field(
        default=False,
        description=(
            "Phase 4.1: when True, /health/ready returns 503 if Qdrant is down. "
            "Default False (degraded, not unhealthy)."
        ),
    )
    observability_health_require_neo4j: bool = Field(
        default=False,
        description=(
            "Phase 4.1: when True, /health/ready returns 503 if Neo4j is down."
        ),
    )

    # === Cost tracking ===
    observability_cost_enabled: bool = Field(
        default=True,
        description=(
            "Phase 4.1: compute cost_usd for every LLM call. "
            "Default True. If False, cost is always 0.0 in logs/metrics."
        ),
    )
    observability_cost_overrides: str = Field(
        default="",
        description=(
            "Phase 4.1: JSON overrides for cost table. Format: "
            "'{\"gpt-4o\": [3.00, 12.00]}'. Empty = use DEFAULT_COSTS table."
        ),
    )

    # === Per-event enable (8 settings) ===
    observability_log_http_requests: bool = Field(
        default=True,
        description="Phase 4.1: log HTTP requests (request_started, request_finished).",
    )
    observability_log_llm_calls: bool = Field(
        default=True,
        description="Phase 4.1: log LLM calls (model, tokens, cost, latency).",
    )
    observability_log_tool_calls: bool = Field(
        default=True,
        description="Phase 4.1: log tool calls (tool_name, ok, duration_ms).",
    )
    observability_log_hook_dispatches: bool = Field(
        default=True,
        description="Phase 4.1: log hook dispatch events (Phase 4.0 hooks).",
    )
    observability_log_compactions: bool = Field(
        default=True,
        description="Phase 4.1: log compaction events (mode, cache_hit, latency).",
    )
    observability_log_merge_queue_events: bool = Field(
        default=True,
        description="Phase 4.1: log merge queue events (Phase 2.x).",
    )
    observability_log_outbound_deliveries: bool = Field(
        default=True,
        description="Phase 4.1: log outbound webhook deliveries (Phase 2.5).",
    )
    observability_log_privacy_decisions: bool = Field(
        default=True,
        description="Phase 4.1: log privacy zone decisions (Phase 3 v1.5.0).",
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
        # Phase 4.1: cost_overrides must be valid JSON object (or empty)
        if self.observability_cost_overrides:
            import json as _json
            try:
                overrides = _json.loads(self.observability_cost_overrides)
            except _json.JSONDecodeError as e:
                raise ValueError(
                    f"observability_cost_overrides must be valid JSON: {e}"
                ) from e
            if not isinstance(overrides, dict):
                raise ValueError(
                    "observability_cost_overrides must be a JSON object "
                    "(e.g. '{\"gpt-4o\": [3.00, 12.00]}')"
                )
            for k, v in overrides.items():
                if not isinstance(k, str) or not isinstance(v, list) or len(v) != 2:
                    raise ValueError(
                        f"observability_cost_overrides: key={k!r} must map to "
                        "[input_cost_per_1k, output_cost_per_1k] (2 floats)"
                    )
        return self


settings = Settings()
