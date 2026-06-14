"""Solomon Harness — configuration.

Loads from environment variables (and .env file in dev).
Single source of truth for all paths, ports, API keys.
"""
from __future__ import annotations

from pathlib import Path

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
        return self


settings = Settings()
