# Configuration Reference

> **Auto-generated** from `config.py` on 2026-06-21 13:39
> Total: **233** settings in **42** sections

All settings can be overridden via environment variables (uppercase + underscores).

> **Related API endpoints:** See [API ↔ Configuration Cross-Reference](api-config-map.md) for which endpoints use which settings.

## Table of Contents

- [Configuration](#agents) (1 settings)
- [Authentication and authorization](#auth) (4 settings)
- [Configuration](#auto) (4 settings)
- [Configuration](#cli) (2 settings)
- [Context compaction](#compaction) (14 settings)
- [Configuration](#context) (1 settings)
- [Configuration](#cors) (1 settings)
- [Configuration](#db) (1 settings)
- [Configuration](#embedding) (3 settings)
- [Configuration](#embeddings) (1 settings)
- [Configuration](#eval) (3 settings)
- [General settings (single-word or uncategorized)](#general) (2 settings)
- [Configuration](#github) (1 settings)
- [Configuration](#hooks) (73 settings)
- [Hot-reload configuration](#hot) (3 settings)
- [Configuration](#legacy) (1 settings)
- [LLM provider settings](#llm) (2 settings)
- [Logging configuration](#log) (1 settings)
- [Configuration](#manual) (1 settings)
- [Configuration](#max) (1 settings)
- [Configuration](#minimax) (1 settings)
- [Configuration](#moonshot) (1 settings)
- [Metrics, traces, and monitoring](#observability) (26 settings)
- [Configuration](#outbound) (4 settings)
- [Configuration](#plugins) (5 settings)
- [Configuration](#pr) (16 settings)
- [Configuration](#pre) (3 settings)
- [Privacy zones and redaction](#privacy) (6 settings)
- [Configuration](#project) (1 settings)
- [Configuration](#prompt) (2 settings)
- [Configuration](#redaction) (3 settings)
- [Configuration](#reflection) (5 settings)
- [Scratchpad / L0 system prompt](#scratchpad) (7 settings)
- [Configuration](#session) (1 settings)
- [Sub-agent routing and configuration](#subagent) (7 settings)
- [Tier-based LLM routing (T1/T2/T3)](#tier) (6 settings)
- [Tool execution and sandboxing](#tool) (6 settings)
- [Plugin trust registry](#trust) (3 settings)
- [Configuration](#web) (3 settings)
- [Outbound webhook delivery](#webhook) (3 settings)
- [Configuration](#ws) (3 settings)
- [Configuration](#zhipuai) (1 settings)

---

## agents — Configuration
<a id="agents"></a>

| Setting | Type | Default |
|---------|------|---------|
| `agents_dir` | `Path` | Path('.harness/agents') — Directory for user-editable sub-agent .md files (overrides built-ins). Resolved relative to settings.project_root. |

---

## auth — Authentication and authorization
<a id="auth"></a>

| Setting | Type | Default |
|---------|------|---------|
| `auth_db_path` | `Path` | PROJECT_ROOT / 'data' / 'harness-scope.db' — SQLite path for the Phase 1.6 scope-gated API token store. Lives one level above the sessions DB. Stores SHA-256 hashes of tokens, never plaintext. |
| `auth_token_bytes` | `int` | 32 — Number of random bytes used to generate a new token's plaintext (returned once at creation time, then discarded). 32 bytes = 256 bits = 64 hex chars, well above OWASP minimums for a server-issued opaque token. |
| `auth_default_scopes` | `str` | '' — Comma-separated scope names applied to tokens created via the CLI when ``--scopes`` is not explicitly passed. Empty string = no scopes (caller must specify). Ignored when the bootstrap admin token is generated — bootstrap always gets ALL_SCOPES. |
| `auth_required` | `bool` | True — Master switch for the scope-gated API. When True, all ``/api/v1/*`` routes require a valid Bearer token with the appropriate scope; ``/api/v1/capabilities`` remains public. When False, the server runs in 'open dev mode' (no auth checks, useful for local development and the test suite). Legacy ``/api/*`` routes (sessions, chat, models, health) are always open in Phase 1.6 regardless of this setting. |

---

## auto — Configuration
<a id="auto"></a>

| Setting | Type | Default |
|---------|------|---------|
| `auto_merge_label` | `str` | 'harness-auto-merge' — Label required by ``gh pr merge --auto`` (GitHub branch-protection typically requires a specific label to enable auto-merge). Set per-repo in the GitHub branch-protection rule. The merge queue adds this label automatically before calling ``enable_auto_merge``. |
| `auto_merge_method` | `str` | 'squash' — Default merge method for ``gh pr merge --auto``. One of ``squash`` (default), ``merge`` (merge commit), ``rebase`` (rebase + ff). Override per-job via the CLI ``--auto-merge-method`` flag. |
| `auto_merge_delete_branch` | `bool` | True — Whether ``gh pr merge --auto`` should pass ``--delete-branch`` to clean up the head branch after a successful merge. Default True (matches Phase 2.2 behavior). |
| `auto_add_label` | `bool` | True — Phase 2.5: when ``job.auto_merge=True``, automatically add the configured ``auto_merge_label`` to the PR via ``gh pr edit --add-label`` immediately after ``create_pr`` succeeds. This is what the Phase 2.3 docs promised but did not implement — branch protection rules that require this label can now enforce it. Disable to skip the label call (the label is then expected to be already present on the PR, e.g. via a GitHub Action). Default True (most setups want this). |

---

## cli — Configuration
<a id="cli"></a>

| Setting | Type | Default |
|---------|------|---------|
| `cli_follow_default_batch_size` | `int` | 10 — Phase 4.12 v1.22.0: default batch size for the ``--follow`` tail loop. When the audit/metrics source emits more than this many lines in a single read, they are yielded in batches of this size. Override per-invocation via the CLI ``--batch-size N`` flag. Default 10 — balances output latency against per-batch processing cost. |
| `cli_follow_state_dir` | `Path` | Path('~/.harness').expanduser() — Phase 4.12 v1.22.0: directory where the ``--follow`` tail loop stores persistent state files (``.follow-state-{kind}.json``). Each state file records the last-read byte offset + inode so ``--resume`` can continue from where the previous run left off. Default ``~/.harness`` (operator's home directory). Override to place state alongside project data (e.g. ``<project_root>/.harness/follow-state/``). |

---

## compaction — Context compaction
<a id="compaction"></a>

| Setting | Type | Default |
|---------|------|---------|
| `compaction_enabled` | `bool` | True — Phase 3: when True, ``ContextCompactor`` collapses long chat history before each LLM call via a sliding window plus an LLM-generated summary. Default True. Set False to disable compaction (e.g. when the model has a 200K context window and cost is not a concern). |
| `compaction_threshold_ratio` | `float` | 0.75 — Phase 3: trigger compaction when message tokens exceed this fraction of the model's context window. Default 0.75 (compact at 75% of ctx). The compactor trims the history to ``compaction_target_ratio`` of ctx afterwards. |
| `compaction_target_ratio` | `float` | 0.5 — Phase 3: after compaction, target this fraction of the model's context window. Default 0.50 (50% of ctx) gives headroom for new turns before the next compact. Must be less than ``compaction_threshold_ratio``. |
| `compaction_keep_recent_turns` | `int` | 6 — Phase 3: minimum number of recent turns to keep verbatim regardless of token count. The sliding window never drops the last N user/assistant turns. Default 6 — enough for the LLM to maintain conversational coherence. |
| `compaction_summarizer_model` | `str` | '' — Phase 3: model id used to summarise dropped turns. Empty string = ``settings.subagent_t1_model`` (Qwen3 8B local, free). The summarizer runs on the dropped turns only, so context overhead is bounded. |
| `compaction_summarizer_fallback` | `str` | '' — Phase 3: model id to fall back to if the primary summarizer fails (timeout, error, unavailable). Empty string = ``settings.subagent_t2_model`` (cloud mid-tier). Set to a known-good model id to override the default. |
| `compaction_summarizer_max_input_tokens` | `int` | 0 — Phase 3: hard cap on the input size passed to the summarizer. 0 = auto (half of T1 model context = 16K for Qwen3 8B). Turns beyond the cap are dropped before the summariser call (sliding window already reduces size; this is the last-mile safety net). |
| `compaction_persist_to_memory` | `bool` | True — Phase 3: when True, the compaction summary is written to UnifiedMemory (L2 mem0) with tag ``#compact`` so it can be retrieved across sessions via semantic search. Default True. Set False for ephemeral (in-memory only) compaction. |
| `compaction_persistent_store` | `bool` | True — Phase 3.5: when True, the compactor uses ``CompactStore`` (SQLite) to cache compaction results by ``(session_id, source_hash)``. On a cache hit the LLM summariser is skipped entirely (zero cost on reconnect). Default True. Set False to disable the persistent cache and use pure in-memory compaction (Phase 3 behavior). |
| `compaction_cache_max_versions` | `int` | 5 — Phase 3.5: maximum number of compaction versions to retain per session in the persistent store. Older versions beyond this cap are pruned (not yet implemented — reserved for Phase 4 retention policy). Must be &gt;= 1. Default 5. |
| `compaction_audit_log` | `bool` | False — Phase 3.5: when True, every compaction event is appended to ``data/audit/compaction-YYYY-MM-DD.ndjson`` with a JSON line per event. Mirrors the Phase 3 ``redaction_audit_log`` pattern. Default False (disabled — enable for compliance / debugging). |
| `compaction_trigger` | `Literal['token', 'turn', 'time', 'hybrid']` | 'token' — Phase 3 v1.5.0: which trigger to use for auto-compaction. ``token`` = existing token-threshold behaviour (backward compat default). ``turn`` = fire every N user turns. ``time`` = fire after N minutes of inactivity. ``hybrid`` = OR semantics — first trigger wins. Change via env var ``HARNESS_COMPACTION_TRIGGER=turn``. |
| `compaction_turn_interval` | `int` | 20 — Phase 3 v1.5.0: user turns between compactions when ``compaction_trigger in {"turn", "hybrid"}``. Default 20 — long enough to amortise compaction cost, short enough to keep context fresh. |
| `compaction_time_idle_minutes` | `int` | 30 — Phase 3 v1.5.0: minutes of session inactivity before firing time-based compaction when ``compaction_trigger in {"time", "hybrid"}``. Default 30 — long enough to ignore brief pauses, short enough to prevent stale context on resume. |

---

## context — Configuration
<a id="context"></a>

| Setting | Type | Default |
|---------|------|---------|
| `context_tracking_enabled` | `bool` | True — Enable per-session cumulative context tracking for tier router. When True, ``AgentContext`` accumulates prompt/completion tokens across turns and provides ``get_context_size()`` for ``TierSelector.select_heuristic(context_size=...)``. Default True. |

---

## cors — Configuration
<a id="cors"></a>

| Setting | Type | Default |
|---------|------|---------|
| `cors_origins` | `list[str]` |  — Allowed CORS origins |

---

## db — Configuration
<a id="db"></a>

| Setting | Type | Default |
|---------|------|---------|
| `db_path` | `Path` | PROJECT_ROOT / 'data' / 'harness.db' — SQLite path for session metadata index |

---

## embedding — Configuration
<a id="embedding"></a>

| Setting | Type | Default |
|---------|------|---------|
| `embedding_model` | `str` | 'intfloat/multilingual-e5-small' — Phase 3: default ONNX model id used by ``OnnxEmbedder``. Multilingual (RU+EN), 384-dim, 118M params, ~120MB on disk. Override to swap models; stored vectors are tagged with ``EMBEDDING_MODEL_VERSION`` so version drift is detected. |
| `embedding_precision` | `Literal['fp32', 'int8']` | 'int8' — Phase 3: numeric precision for the ONNX model. ``int8`` (default) is ~30 MB on disk and ~30ms per query on CPU. ``fp32`` is ~120 MB and ~50ms per query but slightly higher recall. Phase 3 default favours the smaller footprint for operators running on laptop hardware. |
| `embedding_dim` | `int` | 384 — Phase 3: embedding vector dimension. Must match the model's output dim. Default 384 = ``multilingual-e5-small``. Stored in ``Memory.metadata.embedding_dim`` for schema validation when vectors are loaded back from the L4 file mirror. |

---

## embeddings — Configuration
<a id="embeddings"></a>

| Setting | Type | Default |
|---------|------|---------|
| `embeddings_dir` | `Path` | PROJECT_ROOT / 'models' / 'embeddings' — Phase 3: directory where ONNX embedding models are cached. Default ``<project_root>/models/embeddings``. Override with ``HARNESS_EMBEDDINGS_DIR`` to share a single cache across projects (or to point at an existing HuggingFace cache). |

---

## eval — Configuration
<a id="eval"></a>

| Setting | Type | Default |
|---------|------|---------|
| `eval_filler_filter_enabled` | `bool` | True — Phase 5.2B: when True, the PrecisionMetric pipeline drops filler documents (LLM preambles, too-short / too-long turns) before precision@k scoring. Disable for corpora where every turn is a candidate fact. |
| `eval_reranker_enabled` | `bool` | True — Phase 5.2B: when True, the PrecisionMetric pipeline applies length-normalised re-ranking after BM25 retrieval so extreme-length outliers don't dominate the top-K. Disable to measure raw BM25 precision. |
| `eval_filler_max_doc_len` | `int` | 2000 — Phase 5.2B: documents longer than this (chars) are treated as fillers by FillerDetector. Catches log dumps, stack traces pasted whole, etc. Lower for tight corpora; raise for long-form documents. |

---

## general — General settings (single-word or uncategorized)
<a id="general"></a>

| Setting | Type | Default |
|---------|------|---------|
| `host` | `str` | '0.0.0.0' — Bind host |
| `port` | `int` | 8765 — Bind port. Default 8765 because Windows 11 + Docker Desktop reserves 8000/8001 via hns (WSAEACCES). See _output/2026-06/14.06/ports-map.md |

---

## github — Configuration
<a id="github"></a>

| Setting | Type | Default |
|---------|------|---------|
| `github_token_env` | `str` | 'GITHUB_TOKEN' — Name of the env var that holds the GitHub token. The token value is read at PR-creation time and passed to ``gh`` via the environment (never on the command line). Default: GITHUB_TOKEN (the standard GitHub Actions convention). |

---

## hooks — Configuration
<a id="hooks"></a>

| Setting | Type | Default |
|---------|------|---------|
| `hooks_enabled` | `bool` | True — Phase 4.0: master switch for the entire hooks framework. When False, no hooks fire and the registry is bypassed. Per-event enables are ignored when this is False. Default True. |
| `hooks_default_max_ms` | `int` | 3000 — Phase 4.0: per-hook timeout in milliseconds. Applied via ``asyncio.wait_for``. On timeout, the runner returns ``decision='allow'`` (fail-open) and logs a warning. Default 3000ms (3s). |
| `hooks_max_per_event` | `int` | 10 — Phase 4.0: maximum number of hooks that can be registered for a single event. Excess hooks are silently dropped with a warning. Default 10. |
| `hooks_max_recursion_depth` | `int` | 3 — Phase 4.0: maximum recursion depth for hooks (e.g. ``OnMemoryWrite`` fires from inside a hook that writes memory). Depth-bounded to prevent infinite loops. Default 3. |
| `hooks_subprocess_specs` | `str` | '' — Phase 4.0: comma-separated hook specs for subprocess transport. Format: ``<EventType>:subprocess:<path>[:<timeout_ms>]``. Empty = no subprocess hooks. |
| `hooks_http_specs` | `str` | '' — Phase 4.0: comma-separated hook specs for HTTP transport. Format: ``<EventType>:http:<url>[:<timeout_ms>][:<auth>]``. Empty = no HTTP hooks. |
| `hooks_llm_specs` | `str` | '' — Phase 4.0: comma-separated hook specs for LLM-as-hook transport. Format: ``<EventType>:llm:<model>:<timeout_ms>:<prompt>``. Empty = no LLM hooks. Note: LLM hooks add latency + cost; use sparingly (e.g. for hard-to-formalise decisions). |
| `hooks_filter_chain` | `str` | '' — Phase 4.0: comma-separated match_glob filters applied to ALL events. Format: ``<field>=<pattern>``. Example: ``session_id=*-prod,tool_name=!rm``. Empty = no global filter. Per-hook matchers take precedence. |
| `hooks_fail_open` | `bool` | True — Phase 4.0: when True, a hook timeout or exception is treated as ``decision='allow'`` (the operation proceeds). Set False to fail-closed (the operation is blocked). Default True (safer for chat loop). |
| `hooks_redact_payloads` | `bool` | True — Phase 4.0: when True, hook payloads are redacted via RedactionEngine BEFORE being passed to any hook (builtin, subprocess, http, llm). PII / secrets never leave the trust boundary. Default True. |
| `hooks_audit_log` | `bool` | False — Phase 4.0: when True, every hook decision is written to ``<project_root>/data/audit/hooks-YYYY-MM-DD.ndjson`` (append-only, rotated daily). Default False (no audit overhead in production). Enable for compliance / forensic review. |
| `hooks_subprocess_allowed_paths` | `str` | '.harness/hooks/**' — Phase 4.0: glob pattern for allowed subprocess hook script paths. Scripts outside the pattern are rejected at registration time. Default ``.harness/hooks/**`` (project-local). |
| `hooks_rate_limit_capacity` | `float` | 60.0 — Phase 4.8 v1.18.0: token bucket burst capacity per hook. Each ``consume()`` decrements by 1; refill is lazy. Default 60 — a hook may burst up to 60 dispatches, then sustain ``hooks_rate_limit_refill_per_sec`` per second. Set to 0 to deny ALL dispatches (kill switch). |
| `hooks_rate_limit_refill_per_sec` | `float` | 1.0 — Phase 4.8 v1.18.0: sustained refill rate (tokens/sec) for each hook's token bucket. Default 1.0 — one dispatch per second sustained. Set to 0 to disable refill (burst only, then hard stop until the process restarts). |
| `hooks_rate_limit_enabled` | `bool` | True — Phase 4.8 v1.18.0: master switch for per-hook rate limiting. When False, ``HookRateLimiter.check()`` always returns True (no tokens consumed). Default True. |
| `hooks_circuit_breaker_threshold` | `int` | 5 — Phase 4.8 v1.18.0: number of consecutive failures before a hook's circuit opens. Default 5 — tolerates transient errors but trips on a genuinely broken hook. Once open, all dispatches for that hook are skipped until the cooldown elapses, then a single probe call is allowed (half-open). |
| `hooks_circuit_breaker_cooldown_s` | `float` | 60.0 — Phase 4.8 v1.18.0: seconds the circuit stays open before transitioning to half-open. Default 60.0 — one probe per minute for a broken hook. Lower for faster recovery (at the cost of more probe traffic to the failing hook). |
| `hooks_circuit_breaker_enabled` | `bool` | True — Phase 4.8 v1.18.0: master switch for per-hook circuit breaking. When False, ``HookCircuitBreaker.check()`` always returns allow (failures are not recorded). Default True. |
| `hooks_on_memory_write_silent_layers` | `str` | 'L1' — Phase 4.0: comma-separated memory layers whose writes do NOT fire ``OnMemoryWrite`` hooks (e.g. hand-curated layers where audit volume is undesirable). Default ``L1`` (hmem). |
| `hooks_on_compaction_skip_cache_hit` | `bool` | True — Phase 4.0: when True, ``OnCompaction`` is NOT fired on compaction cache hits (returns the cached summary). Set False to fire on every compaction. Default True (reduces audit volume). |
| `hooks_pre_tool_use_enabled` | `bool` | True — Phase 4.0: enable PreToolUse event. |
| `hooks_post_tool_use_enabled` | `bool` | True — Phase 4.0: enable PostToolUse event. |
| `hooks_stop_enabled` | `bool` | True — Phase 4.0: enable Stop event. |
| `hooks_subagent_start_enabled` | `bool` | True — Phase 4.0: enable SubagentStart event. |
| `hooks_subagent_stop_enabled` | `bool` | True — Phase 4.0: enable SubagentStop event. |
| `hooks_session_start_enabled` | `bool` | True — Phase 4.0: enable SessionStart event. |
| `hooks_session_end_enabled` | `bool` | True — Phase 4.0: enable SessionEnd event. |
| `hooks_user_prompt_submit_enabled` | `bool` | True — Phase 4.0: enable UserPromptSubmit event. |
| `hooks_pre_compact_enabled` | `bool` | True — Phase 4.0: enable PreCompact event. |
| `hooks_instructions_loaded_enabled` | `bool` | True — Phase 4.0: enable InstructionsLoaded event. |
| `hooks_permission_request_enabled` | `bool` | True — Phase 4.0: enable PermissionRequest event. |
| `hooks_on_memory_write_enabled` | `bool` | True — Phase 4.0: enable OnMemoryWrite event. |
| `hooks_on_routing_decision_enabled` | `bool` | True — Phase 4.0: enable OnRoutingDecision event. |
| `hooks_on_compaction_enabled` | `bool` | True — Phase 4.0: enable OnCompaction event. |
| `hooks_elicitation_enabled` | `bool` | True — Phase 4.3: enable Elicitation event (interactive prompts). |
| `hooks_notification_enabled` | `bool` | True — Phase 4.3: enable Notification event (fire-and-forget push). |
| `hooks_builtin_log_enabled` | `bool` | True — Phase 4.0: enable builtin LogHook. |
| `hooks_builtin_validate_enabled` | `bool` | True — Phase 4.0: enable builtin ValidateHook. |
| `hooks_builtin_block_dangerous_enabled` | `bool` | True — Phase 4.0: enable builtin BlockDangerousHook. |
| `hooks_builtin_inject_context_enabled` | `bool` | False — Phase 4.0: enable builtin InjectContextHook (off by default — L0 already injected via Phase 3 v1.2.1). |
| `hooks_builtin_autosave_enabled` | `bool` | True — Phase 4.0: enable builtin AutosaveHook. |
| `hooks_builtin_confirm_dangerous_enabled` | `bool` | True — Phase 4.3: enable builtin ConfirmDangerousHook (Elicitation default answer injector). |
| `hooks_builtin_notify_terminal_enabled` | `bool` | True — Phase 4.3: enable builtin NotifyTerminalHook (Notification fanout to stderr). |
| `hooks_builtin_secret_detect_enabled` | `bool` | True — Phase 4.10: enable builtin SecretDetectHook (PreToolUse regex scan for AWS / GitHub / OpenAI keys, PEM, JWT, password literals). Fail-closed: a match always blocks. Default True. |
| `hooks_builtin_sql_injection_guard_enabled` | `bool` | True — Phase 4.10: enable builtin SqlInjectionGuardHook (PreToolUse regex scan for string-interpolated SQL). Fail-closed. Default True. |
| `hooks_builtin_unsafe_import_block_enabled` | `bool` | True — Phase 4.10: enable builtin UnsafeImportBlockHook (PreToolUse regex scan for dangerous imports in *.py content). Fail-closed. Default True. |
| `hooks_unsafe_imports_blocklist` | `str` | 'os.system,subprocess,eval,exec,pickle,yaml.load,requests.post' — Phase 4.10: comma-separated list of dangerous module/method names that unsafe_import_block will reject when seen in *.py content. Defaults match the OWASP Python security cheat sheet: ``os.system`` (shell escape), ``subprocess`` (when paired with ``shell=True``), ``eval``/``exec`` (arbitrary code), ``pickle`` (deserialisation RCE), ``yaml.load`` (without SafeLoader), ``requests.post`` (when missing a timeout). Operators can extend or narrow via the ``HARNESS_HOOKS_UNSAFE_IMPORTS_BLOCKLIST`` env var. Empty string disables ALL checks (use with care). |
| `hooks_notify_webhook_url` | `str` | '' — Phase 4.3+: URL to POST Notification events to (empty = webhook channel disabled). |
| `hooks_notify_webhook_secret` | `str` | '' — Phase 4.3+: HMAC-SHA256 secret for X-Harness-Signature header (empty = no signature). |
| `hooks_notify_webhook_timeout_s` | `float` | 5.0 — Phase 4.3+: webhook POST timeout in seconds. |
| `hooks_notify_desktop_enabled` | `bool` | False — Phase 4.3+: enable desktop channel for Notification (opt-in — uses PowerShell msg/osascript/notify-send). |
| `hooks_notify_slack_webhook_url` | `str` | '' — Phase 4.6 v1.16.0: Slack incoming webhook URL (e.g. ``https://hooks.slack.com/services/T.../B.../...``). Empty = Slack channel disabled (no-op). The URL acts as the secret — it is NEVER logged or echoed in error messages. |
| `hooks_notify_slack_channel` | `str` | '' — Phase 4.6 v1.16.0: optional Slack channel override (e.g. ``#harness-alerts`` or ``C012345``). Empty string = use the webhook's default channel (configured in the Slack app settings). Ignored when ``hooks_notify_slack_webhook_url`` is empty. |
| `hooks_notify_slack_username` | `str` | 'Solomon Harness' — Phase 4.6 v1.16.0: bot display name for Slack messages. Default ``Solomon Harness``. Override per-deployment via ``HARNESS_HOOKS_NOTIFY_SLACK_USERNAME``. |
| `hooks_notify_slack_timeout_s` | `float` | 5.0 — Phase 4.6 v1.16.0: per-request timeout (seconds) for the Slack webhook POST. Mirrors ``hooks_notify_webhook_timeout_s``. Slack webhooks typically respond in &lt;500ms; 5s is a generous cap that tolerates slow corporate proxies. |
| `hooks_notify_teams_webhook_url` | `str` | '' — Phase 4.6 v1.16.0: Microsoft Teams incoming webhook URL (Office 365 connector, e.g. ``https://outlook.office.com/webhook/...``). Empty = Teams channel disabled (no-op). The URL is the secret — NEVER logged or echoed in error messages. |
| `hooks_notify_teams_timeout_s` | `float` | 5.0 — Phase 4.6 v1.16.0: per-request timeout (seconds) for the Teams webhook POST. Mirrors ``hooks_notify_slack_timeout_s``. Default 5s (matches Slack). |
| `hooks_notify_max_retries` | `int` | 3 — Phase 4.8 v1.18.0: maximum retry attempts per channel before the payload is moved to the deadletter queue. ``0`` disables retry entirely (a single attempt — first transient error goes straight to the DLQ). Default 3 gives up to 4 attempts total (initial + 3 retries). |
| `hooks_notify_retry_initial_delay_ms` | `int` | 100 — Phase 4.8 v1.18.0: initial backoff (milliseconds) before the first retry. Subsequent retries double this value up to ``hooks_notify_retry_max_delay_ms``. Default 100ms — long enough to ride out a brief connection blip, short enough to not stall the Notification fanout (Notification is fire-and-forget and runs in a background task). |
| `hooks_notify_retry_max_delay_ms` | `int` | 5000 — Phase 4.8 v1.18.0: cap on the per-retry backoff. The exponential schedule ``initial * 2^attempt`` is capped at this value. Default 5000ms (5s). With ``initial=100`` and ``max=5000`` the sequence is 100, 200, 400, 800, 1600, 3200, 5000, 5000, ... (capped). |
| `hooks_notify_dlq_enabled` | `bool` | True — Phase 4.8 v1.18.0: when True, failed notifications are persisted to the ``notify_dlq`` SQLite table in ``agent-jobs.db`` so an operator can inspect / replay them. When False, failed notifications are dropped (v1.17.0 behaviour — only the observability counter ``notify_dlq_total`` is incremented). Default True. |
| `hooks_elicitation_ws_enabled` | `bool` | True — Phase 4.3+: enable ElicitationBroker + /api/v1/elicitation/ws endpoint (default True; disable for headless deployments). |
| `hooks_elicitation_ws_timeout_s` | `float` | 30.0 — Phase 4.3+: how long to wait for a human answer before falling back to default_answer. |
| `hooks_elicitation_longpoll_enabled` | `bool` | False — Phase 4.3+ v1.15.0: enable HTTP long-poll fallback for Elicitation (/api/v1/elicitation/poll + /answer). Default False — WS is the primary transport; this is the fallback for environments where WS is unavailable. When False, the long-poll endpoints return 403. |
| `hooks_elicitation_longpoll_timeout_s` | `float` | 30.0 — Phase 4.3+ v1.15.0: max seconds a GET /poll request will block waiting for a pending question before returning an empty body / 404. Default 30s (matches the broker timeout). |
| `hooks_elicitation_longpoll_interval_s` | `float` | 0.25 — Phase 4.3+ v1.15.0: polling interval (seconds) for the internal broker.pending() check loop inside the long-poll endpoint. Lower = snappier response at higher CPU cost. |
| `hooks_elicitation_sse_enabled` | `bool` | False — Phase 4.11 v1.21.0: enable the Server-Sent Events transport for Elicitation (``GET /api/v1/elicitation/sse``). Default False — SSE is opt-in because each subscriber holds a long-lived worker. When False, the endpoint returns 403. |
| `hooks_elicitation_sse_heartbeat_s` | `float` | 15.0 — Phase 4.11 v1.21.0: interval (seconds) between SSE ``: keep-alive`` comments. Proxies (nginx, Cloudflare) typically close idle connections at 60s — 15s keeps the stream alive with comfortable margin. Set to 0 to disable heartbeats (not recommended behind a reverse proxy). |
| `hooks_elicitation_sse_max_session_age_s` | `float` | 3600.0 — Phase 4.11 v1.21.0: maximum wall-clock seconds a single SSE subscription is allowed to run before the server gracefully closes the stream. Prevents leaky long-lived connections from accumulating when a client disconnects without the server noticing (e.g. laptop lid closed, NAT timeout without TCP RST). Default 1 hour. Clients may reconnect — the broker is stateless across reconnects (deduplication is per-stream). |
| `hooks_observability_admin_enabled` | `bool` | True — Phase 4.11 Task B v1.21.0: master switch for the admin observability JSON endpoints (``/api/v1/observability/{metrics,health/deep,audit/recent}``). When False, the endpoints are not mounted (404). Default True. The endpoints are always scope-gated via ``Scope.OBSERVABILITY_READ`` regardless of this flag. |
| `hooks_observability_admin_audit_max_limit` | `int` | 500 — Phase 4.11 Task B v1.21.0: hard cap on the ``limit`` query parameter for ``GET /api/v1/observability/audit/recent``. Requests with ``limit`` above this value are rejected with HTTP 422. Default 500 — large enough for forensic review of recent hook activity, small enough to bound response size on a busy deployment. |
| `hooks_observability_admin_metrics_filter` | `str` | '' — Phase 4.11 Task B v1.21.0: optional regex filter applied to the metric names returned by ``GET /api/v1/observability/metrics``. When non-empty, only metrics whose name matches the pattern are included in the JSON snapshot. Default empty (return all metrics). The same filter can be overridden per-request via the ``?filter=`` query parameter. |
| `hooks_admin_enabled` | `bool` | True — v1.31.0: master switch for the hooks admin REST API (``/api/v1/hooks/*``). When False, the endpoints are not mounted (404). Default True. |

---

## hot — Hot-reload configuration
<a id="hot"></a>

| Setting | Type | Default |
|---------|------|---------|
| `hot_reload_enabled` | `bool` | True — Phase 4.2: master switch for hot-reload of .harness/agents/*.md and .harness/hooks/*.json. Default True in dev. Set False in production for stability. |
| `hot_reload_debounce_ms` | `int` | 200 — Phase 4.2: debounce window for file changes. Multiple changes arriving within this window are batched into a single reload event. 200ms = sweet spot for editor saves. |
| `hot_reload_poll_interval_s` | `float` | 1.0 — Phase 4.2: polling interval (seconds) for the polling fallback path. Only used if watchfiles is not installed. |

---

## legacy — Configuration
<a id="legacy"></a>

| Setting | Type | Default |
|---------|------|---------|
| `legacy_apis_gone_enabled` | `bool` | False — Phase 4.12 v1.22.0: when True, legacy ``/api/*`` endpoints (any path starting with ``/api/`` but NOT ``/api/v1/``) return HTTP 410 Gone with RFC 8594 ``Deprecation``/``Sunset`` headers and a JSON body pointing at the migration guide. Default False (opt-in) — flipping to True is a hard cutover that breaks legacy clients. Combine with the existing ``LegacyApiDeprecationMiddleware`` headers (which stay on regardless) for a staged deprecation: headers first (Phase 4.1), then 410 (Phase 4.12) once telemetry confirms clients have migrated. |

---

## llm — LLM provider settings
<a id="llm"></a>

| Setting | Type | Default |
|---------|------|---------|
| `llm_usage_tracking_enabled` | `bool` | True — Enable NDJSON logging of LLM usage for calibration. |
| `llm_usage_log_path` | `Path` | Path('data/llm_usage.jsonl') — Path to NDJSON file for LLM usage events. |

---

## log — Logging configuration
<a id="log"></a>

| Setting | Type | Default |
|---------|------|---------|
| `log_level` | `str` | 'INFO' — Logging level |

---

## manual — Configuration
<a id="manual"></a>

| Setting | Type | Default |
|---------|------|---------|
| `manual_compact_max_ms` | `int` | 30000 — Phase 3 v1.4.0: per-call timeout (milliseconds) for the manual ``/compact`` invocation. Larger than ``reflection_max_ms`` because summarisation is the expensive step. Default 30000 (30 seconds). When exceeded the call returns a partial result (what the cache had) — the chat loop is not blocked. |

---

## max — Configuration
<a id="max"></a>

| Setting | Type | Default |
|---------|------|---------|
| `max_iterations` | `int` | 5 — Max agent loop iterations per task (safety cap) |

---

## minimax — Configuration
<a id="minimax"></a>

| Setting | Type | Default |
|---------|------|---------|
| `minimax_api_key` | `str` | '' — MiniMax API key |

---

## moonshot — Configuration
<a id="moonshot"></a>

| Setting | Type | Default |
|---------|------|---------|
| `moonshot_api_key` | `str` | '' — Moonshot (Kimi) API key |

---

## observability — Metrics, traces, and monitoring
<a id="observability"></a>

| Setting | Type | Default |
|---------|------|---------|
| `observability_enabled` | `bool` | True — Phase 4.1: master switch. False → all observability is no-op. Mirrors hooks_enabled pattern (Phase 4.0). |
| `observability_jsonl_enabled` | `bool` | True — Phase 4.1: write structured JSONL logs to data/logs/. Default True (cheap, ~1ms per log line). Thread-safe. |
| `observability_prometheus_enabled` | `bool` | False — Phase 4.1: enable /metrics endpoint. Default OFF (zero overhead). Set True for production deployments with Prometheus scrape. |
| `observability_otlp_enabled` | `bool` | False — Phase 4.1: export spans via OTLP. Default OFF (requires OTel SDK extras + collector endpoint). No-op if SDK not installed. |
| `observability_log_dir` | `Path` | PROJECT_ROOT / 'data' / 'logs' — Phase 4.1: directory for harness-YYYY-MM-DD.jsonl files. Rotated daily at midnight (date suffix in filename). |
| `observability_log_max_files` | `int` | 30 — Phase 4.1: max retained rotated log files. Older files are deleted by a background task (once per hour). 30 = ~1 month retention. |
| `observability_log_max_file_size_mb` | `int` | 100 — Phase 4.1: rotate file by size (in addition to daily rotation). If a single file exceeds this, rotate early. 0 = size-based disabled. |
| `observability_metrics_path` | `str` | '/metrics' — Phase 4.1: path for Prometheus scrape. Standard is /metrics. |
| `observability_metrics_namespace` | `str` | 'harness' — Phase 4.1: metric name prefix. All metrics start with this. |
| `observability_otlp_endpoint` | `str` | '' — Phase 4.1: OTLP collector endpoint (e.g. http://localhost:4317). Empty = no OTLP export. |
| `observability_otlp_headers` | `str` | '' — Phase 4.1: OTLP headers (comma-separated key=value). E.g. 'api-key=abc123,x-source=harness'. |
| `observability_trace_sample_ratio` | `float` | 1.0 — Phase 4.1: trace sampling ratio. 1.0 = sample every request. 0.1 = sample 10% (reduce collector load). |
| `observability_health_ready_timeout_s` | `float` | 2.0 — Phase 4.1: per-probe timeout for /health/ready. Default 2s. If a DB takes &gt;2s to respond, mark as timeout. |
| `observability_health_deep_timeout_s` | `float` | 5.0 — Phase 4.1: total timeout for /health/deep. Default 5s. Sum of all probes (sqlite+qdrant+neo4j+queue+...). |
| `observability_health_require_qdrant` | `bool` | False — Phase 4.1: when True, /health/ready returns 503 if Qdrant is down. Default False (degraded, not unhealthy). |
| `observability_health_require_neo4j` | `bool` | False — Phase 4.1: when True, /health/ready returns 503 if Neo4j is down. |
| `observability_cost_enabled` | `bool` | True — Phase 4.1: compute cost_usd for every LLM call. Default True. If False, cost is always 0.0 in logs/metrics. |
| `observability_cost_overrides` | `str` | '' — Phase 4.1: JSON overrides for cost table. Format: \'{'{'}"gpt-4o": [3.00, 12.00]{'}'}\'. Empty = use DEFAULT_COSTS table. |
| `observability_log_http_requests` | `bool` | True — Phase 4.1: log HTTP requests (request_started, request_finished). |
| `observability_log_llm_calls` | `bool` | True — Phase 4.1: log LLM calls (model, tokens, cost, latency). |
| `observability_log_tool_calls` | `bool` | True — Phase 4.1: log tool calls (tool_name, ok, duration_ms). |
| `observability_log_hook_dispatches` | `bool` | True — Phase 4.1: log hook dispatch events (Phase 4.0 hooks). |
| `observability_log_compactions` | `bool` | True — Phase 4.1: log compaction events (mode, cache_hit, latency). |
| `observability_log_merge_queue_events` | `bool` | True — Phase 4.1: log merge queue events (Phase 2.x). |
| `observability_log_outbound_deliveries` | `bool` | True — Phase 4.1: log outbound webhook deliveries (Phase 2.5). |
| `observability_log_privacy_decisions` | `bool` | True — Phase 4.1: log privacy zone decisions (Phase 3 v1.5.0). |

---

## outbound — Configuration
<a id="outbound"></a>

| Setting | Type | Default |
|---------|------|---------|
| `outbound_webhook_urls` | `str` | '' — Phase 2.5: comma-separated list of HTTP(S) URLs that receive POST events for critical lifecycle moments (``merged``, ``failed``, ``stack_merged``, ``pr_waiting_review``). Empty string (default) disables outbound entirely. Each URL is called with ``Authorization: Bearer <outbound_webhook_token>`` and a JSON body mirroring the :class:`JobEvent` shape: ``{event, job_id, kind, ...payload}``. Failed deliveries (4xx/5xx/timeout) are retried up to ``outbound_webhook_max_retries`` times with exponential backoff; after exhaustion we log a warning but do NOT fail the underlying job. The intent is to integrate with Slack / Telegram / an internal dashboard without blocking the merge queue. |
| `outbound_webhook_token` | `str` | '' — Phase 2.5: shared bearer token sent in the ``Authorization`` header of every outbound webhook. The receiver is expected to validate it and reject unauthorized requests. Leave empty to send no ``Authorization`` header (NOT recommended in production — anyone who can reach the URL can read the events). Phase 4 will replace this with HMAC signing. |
| `outbound_webhook_timeout_s` | `float` | 5.0 — Phase 2.5: per-request HTTP timeout (seconds) for the outbound webhook delivery. If the receiver is slower than this, the call is aborted and retried. Default 5.0s — a slow downstream should not stall the merge queue. |
| `outbound_webhook_max_retries` | `int` | 3 — Phase 2.5: how many times a single outbound POST is retried on 4xx / 5xx / timeout. With ``max_retries=3`` the receiver gets up to 4 attempts (initial + 3). Set to 0 to fire-and-forget without retry. Default 3. |

---

## plugins — Configuration
<a id="plugins"></a>

| Setting | Type | Default |
|---------|------|---------|
| `plugins_enabled` | `bool` | False — Phase 6.2A v1.27.0: master switch for the plugin loader. When False (default — opt-in), the lifespan startup does NOT scan ``plugins_dir`` and no plugin code is executed. Flip to True to enable. Plugins are untrusted user code; the loader restricts their globals namespace and AST-blocks imports of ``harness.agents`` / ``harness.server``. |
| `plugins_dir` | `Path` | Path('.harness/plugins') — Phase 6.2A v1.27.0: directory scanned for ``*.py`` plugin files. Resolved relative to ``settings.project_root``. Default ``.harness/plugins`` (project-local, user-editable). Non-existent directory is silently skipped (no error). |
| `plugins_allowed` | `list[str]` |  — Phase 6.2A v1.27.0: whitelist of plugin stems allowed to load. Empty list (default) = ALL discovered plugins are allowed. Set to e.g. ``['example_logger', 'my_metrics']`` to load only those, silently skipping the rest. Stems are the ``*.py`` filename without extension. |
| `plugins_dispatch_enabled` | `bool` | True — Phase 6.3 v1.28.0: master switch for the PluginDispatcher. When True (default), the HookRunner invokes plugin callbacks registered via PluginRegistry.register_hook in-process. When False, the dispatcher is a no-op — hooks fire as in Phase 6.1 even if plugins were loaded. Use this to disable plugin dispatch at runtime without unloading the plugins themselves (e.g. for debugging). |
| `plugins_admin_enabled` | `bool` | True — v1.31.0: master switch for the plugins admin REST API (``/api/v1/plugins/*``). When False, the endpoints are not mounted (404). Default True. |

---

## pr — Configuration
<a id="pr"></a>

| Setting | Type | Default |
|---------|------|---------|
| `pr_default_target_branch` | `str` | 'main' — Target branch the PR is opened against. Override per-job via ``MergeJob.pr_target_branch`` or the CLI ``--pr-target`` flag. |
| `pr_poll_interval_s` | `float` | 15.0 — Seconds between ``gh pr view`` polls while waiting for CI checks / review decisions. Used by ``wait_for_checks()``. |
| `pr_wait_timeout_s` | `float` | 300.0 — Wall-clock cap (seconds) for waiting on PR checks/review. After this, the job is marked ``failed`` with ``error='PR checks timed out after Ns'``. Set higher for repos with slow CI. |
| `pr_strategy` | `str` | 'auto' — PR-mode strategy: ``auto`` (PR-IF-REMOTE — if ``origin`` exists AND ``gh auth status`` is ok, open PR; otherwise fall back to local ff-merge + warning), ``strict`` (PR is required; failure on missing gh is a hard error), ``off`` (never open a PR, always local merge). |
| `pr_split_strategy` | `str` | 'auto' — Strategy used by :class:`~harness.agents.pr_split.SplitPlanner` to split a job's diff into N PR slices. One of:\n  - ``auto`` (default): if diff &lt;= max_files_per_slice, return one slice (single-PR path); else fall back to ``directory``.\n  - ``files``: round-robin slices of at most ``max_files_per_slice`` files each.\n  - ``directory``: group by top-level directory prefix (e.g. ``src/*`` in one slice, ``tests/*`` in another).\n  - ``size``: balance by LOC (uses ``git diff --shortstat`` per file). Most expensive but most even.\nOverride per-job via the CLI ``--split-strategy`` flag. |
| `pr_split_max_files_per_slice` | `int` | 10 — Maximum number of files per slice for the ``files`` and ``auto`` strategies. The default of 10 keeps each PR small and reviewable. Ignored by the ``directory`` strategy (which uses directory boundaries, not file counts). |
| `pr_split_min_slices` | `int` | 1 — If the diff is smaller than ``min_slices * max_files_per_slice``, the planner collapses to a single slice (the legacy single-PR path). Default 1 — never split a small diff. |
| `pr_split_max_slices` | `int` | 8 — Hard cap on the number of slices in a stack. Prevents a user from requesting ``--split-into 100`` and overwhelming the GitHub API. Default 8 — the largest reasonable stacked PR. |
| `pr_template_path` | `str` | '' — Optional path to a custom PR body template. The file should contain a Markdown template with ``{task}``, ``{head_branch}``, ``{base_branch}``, ``{slice_index}``, ``{slice_total}``, ``{stack_id}``, ``{issue_numbers}``, ``{codeowners_reviewers}`` placeholders. Empty string = use the built-in default template (see ``harness/agents/templates/pr_body.md``). |
| `pr_issue_link_re` | `str` | '#(\\d+)' — Regular expression (single capturing group) used to extract issue numbers from the job's task text. Default ``#(\\d+)`` matches bare ``#123`` references. Operators can supply a more restrictive pattern (e.g. ``(?:Closes\|Refs\|Fixes) #(\\d+)``) to limit auto-linking to explicit phrases only. |
| `pr_review_timeout_s` | `int` | 86400 — Phase 2.4: how long the queue will wait for a PR review decision (``approved`` or ``changes_requested``) after the CI checks pass. Default 86400 (24 hours). After the timeout, the job is marked ``failed`` with ``error='PR review timeout'``. |
| `pr_review_poll_interval_s` | `int` | 30 — Phase 2.4: polling interval for the review-state check (complement to the webhook-based short-circuit). Default 30s. Webhooks short-circuit this loop when they arrive, so the interval only matters when webhooks are disabled or GitHub is slow to deliver them. |
| `pr_rate_limit_max_retries` | `int` | 5 — Phase 2.5: how many times ``_gh_with_retry`` will retry a ``gh`` subprocess that returned 403 or 429 (rate limited). After this many failed attempts, the call raises :class:`GHUnavailable` and the PR phase fails the same way as a missing ``gh`` binary. Set to 0 to disable retry entirely (Phase 2.4 behaviour). Default 5. |
| `pr_rate_limit_initial_backoff_s` | `float` | 2.0 — Phase 2.5: initial sleep (seconds) before the first retry. Subsequent retries multiply by 2 up to ``pr_rate_limit_max_backoff_s``. If the ``gh`` stderr contains a ``Retry-After: N`` line (parsed via regex), we honor ``N`` instead of the exponential schedule. Default 2.0s — a good balance between responsiveness and not hammering GitHub. |
| `pr_rate_limit_max_backoff_s` | `float` | 60.0 — Phase 2.5: maximum sleep between retries. The exponential schedule ``initial * 2^attempt`` is capped at this value. Default 60.0s (one minute). With ``initial=2.0`` and ``max=60.0`` the sequence is approximately 2, 4, 8, 16, 32, 60, 60, ... (5 retries by default). |
| `pr_rate_limit_jitter_s` | `float` | 0.5 — Phase 2.5: random uniform jitter (seconds) added to each backoff sleep. Reduces the thundering-herd effect when many background jobs hit the same 429 burst. Default 0.5s. Set to 0 to disable (deterministic — useful in tests). |

---

## pre — Configuration
<a id="pre"></a>

| Setting | Type | Default |
|---------|------|---------|
| `pre_compact_enabled` | `bool` | True — Phase 3 v1.5.0: enable the pre-compaction hook. ``False`` → no state snapshot is saved before compaction. Server-wide kill switch via env var ``HARNESS_PRE_COMPACT_ENABLED=false``. |
| `pre_compact_max_ms` | `int` | 5000 — Phase 3 v1.5.0: per-call timeout for the pre-compaction hook (milliseconds). On timeout, the hook is skipped (fail-open) and compaction proceeds. Default 5000ms = 5s. |
| `pre_compact_save_fields` | `str` | 'messages_last_n,plan_step,hot_l0,metadata' — Phase 3 v1.5.0: comma-separated list of state fields to capture in the pre-compact snapshot. Valid fields: ``messages_last_n`` (last 5 user/assistant), ``plan_step`` (current scratchpad plan step), ``hot_l0`` (scratchpad L0 snapshot), ``metadata`` (tokens/turns/last_compact_at). Empty string → save nothing (hook becomes no-op). |

---

## privacy — Privacy zones and redaction
<a id="privacy"></a>

| Setting | Type | Default |
|---------|------|---------|
| `privacy_zones_enabled` | `bool` | True — Phase 3 v1.5.0: master switch for path-based privacy zones. ``False`` → :class:`harness.privacy.PrivacyZoneFilter` is a no-op (``check()`` always returns ``("allow", None)``). Server-wide kill switch via env var ``HARNESS_PRIVACY_ZONES_ENABLED=false``. |
| `privacy_zone_patterns` | `str` | '' — Phase 3 v1.5.0: comma-separated list of glob patterns. Empty string → use built-in defaults ``['private/**', '*.env', '.env/**', 'secrets/**', ``'``_credentials/**', '.ssh/**']``. Override per-pattern via ``privacy_zone_per_action``. Glob syntax matches :mod:`harness.privacy.path_match` (``*``, ``**``, ``?``, anchored ``/``, trailing ``/``, negation ``!``). |
| `privacy_zone_default_action` | `Literal['block', 'redact', 'skip']` | 'block' — Phase 3 v1.5.0: fallback action for patterns without an explicit override in ``privacy_zone_per_action``. ``block`` = return error to LLM (most conservative), ``redact`` = replace content with ``[PRIVATE: <reason>]`` placeholder, ``skip`` = silent skip (return empty result). |
| `privacy_zone_per_action` | `str` | '' — Phase 3 v1.5.0: per-pattern action overrides. Format: ``"private/**=redact,secrets/*=block"``. Comma-separated ``pattern=action`` pairs. Actions must be one of ``block``, ``redact``, ``skip``. Empty string → all patterns use ``privacy_zone_default_action``. |
| `privacy_zones_audit_log` | `bool` | False — Phase 3 v1.5.0: emit ``privacy_zone_blocked`` / ``privacy_zone_redacted`` / ``privacy_zone_skipped`` events to :class:`harness.context.scratchpad_audit.ScratchpadAudit` on every non-``allow`` decision. Off by default — operators opt in via ``HARNESS_PRIVACY_ZONES_AUDIT_LOG=true``. |
| `privacy_zones_admin_enabled` | `bool` | False — Phase 5.3 v1.25.0: master switch for the privacy zones admin CRUD REST API (``/api/v1/privacy/zones``). When False, the endpoints return 404 (admin surface not mounted). When True, endpoints are scope-gated via ``privacy.read`` (GET) and ``privacy.write`` (POST/PUT/DELETE). Default False (opt-in) — operators who want runtime CRUD management flip this to True. |

---

## project — Configuration
<a id="project"></a>

| Setting | Type | Default |
|---------|------|---------|
| `project_root` | `Path` | Path('C:/MyAI') — Project root for file tools (paths are resolved under this) |

---

## prompt — Configuration
<a id="prompt"></a>

| Setting | Type | Default |
|---------|------|---------|
| `prompt_cache_enabled` | `bool` | True — Phase 3 v1.4.0: master switch for prompt caching. When True AND ``prompt_cache_strategy`` matches the current model, ``LLMRouter`` injects ``cache_control`` markers (Anthropic) or relies on the provider's prefix caching (vLLM). Default True — caching is a pure cost/latency optimisation, no functional change when the provider ignores the markers. |
| `prompt_cache_strategy` | `Literal['anthropic', 'vllm', 'off']` | 'off' — Phase 3 v1.4.0: which prompt-caching strategy to apply. ``anthropic`` injects ``cache_control: {type: ephemeral}`` on the system message and last 2 turns when the model id starts with ``anthropic/``. ``vllm`` is a no-op at the call site — vLLM prefix caching is enabled at the engine level (operator must configure vLLM externally); we just keep the setting here for visibility. ``off`` disables caching entirely. Default ``off`` — operators opt in by setting the env var ``HARNESS_PROMPT_CACHE_STRATEGY``. |

---

## redaction — Configuration
<a id="redaction"></a>

| Setting | Type | Default |
|---------|------|---------|
| `redaction_enabled` | `bool` | True — Phase 3: when True, all 9 sink points (LLM messages, PR title/body, commit msg, branch name, JobStore prompt, outbound webhooks, .env read_file, inbound webhooks) run PII / secret redaction before persistence or external transmission. Default True (opt-out — safe baseline for an open-source tool). Set False only for tests or isolated offline use. |
| `redaction_categories` | `str` | '' — Phase 3: comma-separated list of redaction categories to enable. Empty string = all 12 default categories (email, phone, IPv4, GitHub PAT variants, AWS keys, OpenAI/Anthropic keys, .env assignments, JWT, PEM, Slack tokens). Use this to narrow the pattern set on a per-deployment basis without editing code. |
| `redaction_audit_log` | `bool` | False — Phase 3: when True, every redaction event is mirrored to ``data/audit/redaction-YYYY-MM-DD.ndjson`` (append-only, rotated daily) and to the JobStore event log (kind="redaction"). Default False (no audit overhead in production). Enable for compliance / forensic review. |

---

## reflection — Configuration
<a id="reflection"></a>

| Setting | Type | Default |
|---------|------|---------|
| `reflection_enabled` | `bool` | True — Phase 3 v1.4.0: when True, ``SessionLifecycle.__aexit__`` calls ``ReflectionLoop.reflect(events)`` to extract structured lessons from the session's event log. Disable to skip reflection entirely (useful for short-lived CI jobs that don't need lesson extraction). |
| `reflection_max_lessons` | `int` | 5 — Phase 3 v1.4.0: maximum number of lessons to extract per session. Caps the LLM's response size. Default 5 — enough for typical end-of-session extraction, not so many that the response overwhelms the prompt. |
| `reflection_max_ms` | `int` | 10000 — Phase 3 v1.4.0: per-call timeout (milliseconds) for the reflection extraction. When exceeded the lifecycle fires the next steps (session close) without lessons (fail-open). Default 10000 (10 seconds) — a small T1 model typically completes in &lt;2s, the cap is only relevant for contended providers. |
| `reflection_model` | `str` | '' — Phase 3 v1.4.0: primary reflection model id. Empty string = fall back to ``subagent_t1_model`` (default ``qwen3:8b`` local). Set to a cloud model id (e.g. ``glm-4.7``) if you want reflection to skip the local tier and go straight to a faster cloud summariser. |
| `reflection_fallback_model` | `str` | '' — Phase 3 v1.4.0: fallback reflection model id. Empty string = fall back to ``subagent_t2_model`` (default ``glm-4.7``). Used when ``reflection_model`` is unavailable or returns an error. Mirrors the compactor's summariser/fallback cascade at ``compaction.py:201-208``. |

---

## scratchpad — Scratchpad / L0 system prompt
<a id="scratchpad"></a>

| Setting | Type | Default |
|---------|------|---------|
| `scratchpad_enabled` | `bool` | True — Phase 3 v1.2.0: when True, the agent runtime exposes 4 scratchpad tools (``scratchpad_write_note`` / ``scratchpad_read_notes`` / ``scratchpad_plan_step`` / ``scratchpad_mark_done``) bound to a per-``(session_id, agent_id)`` ``ScratchpadStore``. Default True. Set False to disable the scratchpad feature entirely (tools return a graceful error when invoked). |
| `scratchpad_max_notes_per_session` | `int` | 100 — Phase 3 v1.2.0: hard cap on the total number of notes (L0+L1+L2) for a single ``(session_id, agent_id)`` pair. Older rows beyond the cap are pruned on insert. Default 100. Must be &gt;= 1. |
| `scratchpad_l0_max_bytes` | `int` | 1024 — Phase 3 v1.2.0: maximum total size of L0 notes in bytes. L0 is the hot layer injected into the system prompt on every turn; keep tight. Default 1024 (1KB). Must be &gt;= 128. Writes / promotes that would push the L0 total over the cap trigger an auto-prune of the oldest L0 row; single notes larger than the cap are rejected. |
| `scratchpad_audit_log` | `bool` | False — Phase 3 v1.2.0: when True, every scratchpad event (``write`` / ``read`` / ``promote`` / ``plan_step`` / ``mark_done`` / ``l0_cap_exceeded``) is appended to ``data/audit/scratchpad-YYYY-MM-DD.ndjson``. Default False (opt-in — enable for compliance / debugging). |
| `scratchpad_inject_l0_to_system_prompt` | `bool` | True — Phase 3 v1.2.1: when True (default), the runner reads L0 notes from the scratchpad on every ``run`` / ``stream`` call and prepends them to the system prompt as a ``## Hot context (L0 notes — this session, auto-injected)`` section. This is the ``L0``-layer fulfilment of the Anthropic "Write context" strategy: hot facts / plan / state are visible to the model without needing an extra ``scratchpad_read_notes`` tool round-trip. Set False to fall back to v1.2.0 behaviour (LLM must call ``scratchpad_read_notes`` to consult L0). |
| `scratchpad_l2_qdrant_url` | `str | None` | None — Phase 3 v1.3.0: optional Qdrant server URL (e.g. ``http://localhost:6333``) for L2 note embeddings. When set AND the server is reachable, L2 notes are stored in a dedicated Qdrant collection for dense+BM25 hybrid retrieval. When ``None`` (default) OR the server is unreachable, the harness falls back to the in-SQLite ``SqliteL2Store`` (vector column in ``scratchpad_notes`` as BLOB) — no new required dependencies, works offline. |
| `scratchpad_l2_qdrant_collection` | `str` | 'scratchpad_l2' — Phase 3 v1.3.0: Qdrant collection name for L2 note embeddings. Default ``scratchpad_l2``. Override to share a single Qdrant instance across multiple Harness deployments (one collection per environment). |

---

## session — Configuration
<a id="session"></a>

| Setting | Type | Default |
|---------|------|---------|
| `session_dir` | `Path` | PROJECT_ROOT / 'data' / 'sessions' — JSONL session mirror directory |

---

## subagent — Sub-agent routing and configuration
<a id="subagent"></a>

| Setting | Type | Default |
|---------|------|---------|
| `subagent_default_model` | `str` | 'MiniMax-M2.7' — Default LLM model id for built-in sub-agents (must be in catalog) |
| `subagent_judges` | `int` | 2 — Number of adversarial judges (used by AdversarialVerify). |
| `subagent_timeout_s` | `float` | 300.0 — Wall-clock cap (seconds) for a single sub-agent run (used by MergeQueue). |
| `subagent_t1_model` | `str` | 'qwen3:8b' — Tier-1 model id (cheap local, e.g. Ollama). Used by TierSelector when router confidence &gt;= subagent_confidence_high. Set to empty string to disable T1 (cascade falls back to T2). |
| `subagent_t2_model` | `str` | 'glm-4.7' — Tier-2 model id (cloud mid-tier). Used when confidence is in [subagent_confidence_low, subagent_confidence_high). |
| `subagent_confidence_high` | `float` | 0.6 — Confidence &gt;= this threshold -&gt; Tier-1 (cheap local). Calibrated Phase 7.5 on 37K production events. See docs/MODEL_REGISTRY.md and docs/calibration-report-v133.md. |
| `subagent_confidence_low` | `float` | 0.3 — Confidence in [low, high) -&gt; Tier-2. Below low -&gt; Tier-3 (premium). Calibrated Phase 7.5 on 37K production events. See docs/MODEL_REGISTRY.md and docs/calibration-report-v133.md. |

---

## tier — Tier-based LLM routing (T1/T2/T3)
<a id="tier"></a>

| Setting | Type | Default |
|---------|------|---------|
| `tier_routing_heuristic_enabled` | `bool` | True — Phase 7.5 v1.33.0: master switch for the heuristic tier selector. When True, ``TierSelector.select_heuristic`` is consulted before the confidence cascade; when False, it returns ``None`` unconditionally (fall-through to explicit ``model:`` config). Default True. |
| `tier_routing_t1_max_prompt_chars` | `int` | 1000 — Phase 7.5: maximum prompt length (chars) for T1 eligibility. Prompts longer than this skip T1. Calibrated up from 500 (v1.26.0) for wider T1 zone. Default 1000. |
| `tier_routing_t1_max_context_tokens` | `int` | 2000 — Phase 7.6: maximum context size (tokens) for T1 eligibility. Contexts larger than this skip T1. Recalibrated down from 8000 (v1.33.0) — synthetic benchmark v2 shows narrower T1 context band improves accuracy. Default 2000. |
| `tier_routing_t3_min_prompt_chars` | `int` | 10000 — Phase 7.6: minimum prompt length (chars) that forces T3. Prompts this long are routed to the premium tier directly. Recalibrated up from 3000 (v1.33.0) — synthetic benchmark v2 shows higher bar reduces unnecessary T3 routing. Default 10000. |
| `tier_routing_t3_min_context_tokens` | `int` | 16000 — Phase 7.5: minimum context size (tokens) that forces T3. Large contexts get the premium tier to maximise quality. Calibrated down from 32000 (v1.26.0). Default 16000. |
| `tier_routing_complexity_keywords` | `list[str]` |  — Phase 7.5: case-insensitive keywords that trigger T3 routing when present in the prompt. Indicates a complex task that benefits from the premium tier. Unchanged from v1.26.0. Default: ['reasoning', 'analyze', 'prove', 'derive', 'evaluate']. |

---

## tool — Tool execution and sandboxing
<a id="tool"></a>

| Setting | Type | Default |
|---------|------|---------|
| `tool_offload_enabled` | `bool` | True — Phase 3 v1.3.1: when True, tool results that exceed ``tool_offload_threshold_bytes`` are persisted to L2 scratchpad and replaced with a small stub in the message history. The LLM can pull the full body via ``scratchpad_read_offloaded(id=N)`` or search across offloaded content via ``scratchpad_search_offloaded(query)``. Set False to disable offload entirely (the full content is kept inline, which can blow past the context window for large tool results). |
| `tool_offload_threshold_bytes` | `int` | 25600 — Phase 3 v1.3.1: minimum byte count to trigger offload. Default 25600 (25 KB) — matches the Anthropic context-engineering playbook's '&gt;25k tokens' rule of thumb. Lower for stricter offload (preserves chat budget), higher for fewer offloads (preserves the inline preview). |
| `tool_offload_preview_lines` | `int` | 3 — Phase 3 v1.3.1: number of non-empty lines from the original tool output to include in the stub preview. Default 3 — gives the LLM enough context to decide whether to fetch the full body without spending more than ~600 chars of the message budget. |
| `tool_offload_preview_max_chars` | `int` | 600 — Phase 3 v1.3.1: hard cap on the stub preview size in characters. Default 600. Combined with ``tool_offload_preview_lines`` (3) this bounds the stub to ~3 lines × ~200 chars/line, well below the 25 KB threshold that triggered the offload. |
| `tool_offload_read_max_bytes` | `int` | 4096 — Phase 3 v1.3.1: default chunk size for ``scratchpad_read_offloaded`` when the LLM does not pass an explicit ``max_bytes``. Default 4096 (4 KB). The LLM can request larger chunks by passing ``max_bytes`` explicitly. |
| `tool_offload_max_ms` | `int` | 2000 — Phase 3 v1.3.1: per-call timeout (milliseconds) for the offload write. When exceeded the loop keeps the full tool content inline (fail-open) rather than blocking the chat. Default 2000 (2 seconds) — most offloads complete in &lt;100 ms on a local SQLite store, the cap is only relevant for very large writes or contended disks. |

---

## trust — Plugin trust registry
<a id="trust"></a>

| Setting | Type | Default |
|---------|------|---------|
| `trust_registry_path` | `Path` | Path('trust-registry.json') — Path to trust registry JSON file. Override via HARNESS_TRUST_REGISTRY_PATH. |
| `trust_registry_hot_reload` | `bool` | True — Enable hot-reload of trust registry file on change. |
| `trust_registry_poll_interval` | `int` | 5 — Polling interval (seconds) for trust registry hot-reload. |

---

## web — Configuration
<a id="web"></a>

| Setting | Type | Default |
|---------|------|---------|
| `web_ui_enabled` | `bool` | True — Master switch for the built-in Web UI. When True AND ``web_dist_path / 'index.html'`` exists, the FastAPI app mounts ``/ui`` as a StaticFiles + SPA fallback. Set False to disable the UI mount entirely (e.g. headless deployments). |
| `web_dist_path` | `Path` | Path('web/dist') — Directory containing the built Web UI (the Vite output). Resolved relative to the project root. Must contain ``index.html`` and an ``assets/`` directory. |
| `web_ui_route_prefix` | `str` | '/ui' — URL prefix under which the Web UI is served. Default ``/ui``. Serves the SPA at ``/ui`` and static assets at ``/ui/assets/``. |

---

## webhook — Outbound webhook delivery
<a id="webhook"></a>

| Setting | Type | Default |
|---------|------|---------|
| `webhook_secret` | `str` | '' — HMAC-SHA256 shared secret for the inbound GitHub webhook receiver. Set this to the same value configured in the GitHub repo's webhook settings. Empty string = webhooks disabled (the route returns 503). The value is NEVER logged or echoed in error messages — only the env var NAME is surfaced. The HMAC verification uses stdlib ``hmac.compare_digest`` for timing-safe comparison. |
| `webhook_path` | `str` | '/api/v1/agents/webhooks/github' — URL path where the GitHub webhook receiver is mounted. Configure this in the GitHub repo's webhook settings exactly. Default: ``/api/v1/agents/webhooks/github``. |
| `webhook_max_payload_kb` | `int` | 256 — Maximum accepted webhook payload size in KB. GitHub webhook payloads are typically &lt;5KB, but ``check_run`` events with verbose annotations can be larger. 256KB is a generous cap; the route returns 413 on overflow. |

---

## ws — Configuration
<a id="ws"></a>

| Setting | Type | Default |
|---------|------|---------|
| `ws_metrics_interval_s` | `float` | 1.0 — WI-04: interval (seconds) between metrics/health collection cycles published to WebSocket clients via the MetricsBroker. Default 1.0s. |
| `ws_heartbeat_s` | `float` | 30.0 — WI-04: WebSocket heartbeat timeout (seconds). If no ``ping`` message is received from the client within this window, the server closes the connection with code 4001. Default 30.0s. |
| `ws_max_backlog` | `int` | 100 — WI-04: maximum messages per subscriber queue before the oldest is dropped (backpressure). Default 100. |

---

## zhipuai — Configuration
<a id="zhipuai"></a>

| Setting | Type | Default |
|---------|------|---------|
| `zhipuai_api_key` | `str` | '' — ZhipuAI (GLM) API key |

---
