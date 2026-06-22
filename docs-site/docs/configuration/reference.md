# Configuration Reference

> **Auto-generated** from `config.py` on 2026-06-21 20:22
> Total: **233** settings in **42** sections

All settings can be overridden via environment variables. By default, the env var name is the setting name uppercased (e.g. `subagent_judges` → `SUBAGENT_JUDGES`). If `alias=` is set on the field, the explicit alias is used instead.

**Constraints:** `ge=`/`le=`/`gt=`/`lt=` are Pydantic field constraints applied at validation time.

## Table of Contents

- [Sub-agent directory location](#agents) (1 settings)
- [Authentication and authorization (Bearer tokens, scopes)](#auth) (4 settings)
- [Auto-merge and PR automation defaults](#auto) (4 settings)
- [CLI subcommands and defaults](#cli) (2 settings)
- [Context compaction (pre, thresholds, force)](#compaction) (14 settings)
- [Context window and message handling](#context) (1 settings)
- [CORS allowed origins](#cors) (1 settings)
- [Database paths (sessions, scope tokens)](#db) (1 settings)
- [Embedding model configuration (model id, dimensions)](#embedding) (3 settings)
- [Embedding variant / secondary model](#embeddings) (1 settings)
- [Evaluation and calibration harness](#eval) (3 settings)
- [General settings (host, port, log_level, project_root)](#general) (2 settings)
- [GitHub integration (token, repo)](#github) (1 settings)
- [Hook patterns, filters, audit](#hooks) (73 settings)
- [Hot-reload configuration (file watcher, intervals)](#hot) (3 settings)
- [Legacy compatibility flags (deprecated, do not use in new code)](#legacy) (1 settings)
- [LLM provider settings (catalog, defaults)](#llm) (2 settings)
- [Logging configuration (level, format, sinks)](#log) (1 settings)
- [Manual override and CLI-only flags](#manual) (1 settings)
- [Maximum limits (iterations, payload sizes)](#max) (1 settings)
- [MiniMax (Anthropic-compatible) API key](#minimax) (1 settings)
- [Moonshot (Kimi) API key](#moonshot) (1 settings)
- [Metrics, traces, and monitoring](#observability) (26 settings)
- [Outbound notifications (Slack, Teams, webhooks)](#outbound) (4 settings)
- [Plugin discovery, dispatch, trust](#plugins) (5 settings)
- [Pull request automation (strategy, polling, timeout)](#pr) (16 settings)
- [Pre-execution hooks and checks](#pre) (3 settings)
- [Privacy zones and redaction](#privacy) (6 settings)
- [Project root paths and resolution](#project) (1 settings)
- [Prompt template configuration](#prompt) (2 settings)
- [PII redaction patterns (12 built-in)](#redaction) (3 settings)
- [Reflection patterns (T1→T2 escalation)](#reflection) (5 settings)
- [Scratchpad / L0 system prompt](#scratchpad) (7 settings)
- [Session storage and metadata](#session) (1 settings)
- [Sub-agent routing and configuration](#subagent) (7 settings)
- [Tier-based LLM routing (T1/T2/T3)](#tier) (6 settings)
- [Tool execution and sandboxing](#tool) (6 settings)
- [Plugin trust registry (signed manifests)](#trust) (3 settings)
- [Web UI server config (FastAPI mount, static paths)](#web) (3 settings)
- [Inbound + outbound webhook delivery](#webhook) (3 settings)
- [WebSocket transport (backpressure, heartbeat)](#ws) (3 settings)
- [ZhipuAI (GLM models) API key](#zhipuai) (1 settings)

---

## agents — Sub-agent directory location
<a id="agents"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `agents_dir` | `Path` | Path('.harness/agents') | `AGENTS_DIR` | — |

---

## auth — Authentication and authorization (Bearer tokens, scopes)
<a id="auth"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `auth_db_path` | `Path` | PROJECT_ROOT / 'data' / 'harness-scope.db' | `AUTH_DB_PATH` | — |
| `auth_token_bytes` | `int` | 32 | `AUTH_TOKEN_BYTES` | ge=16, le=64 |
| `auth_default_scopes` | `str` | '' | `AUTH_DEFAULT_SCOPES` | — |
| `auth_required` | `bool` | True | `AUTH_REQUIRED` | — |

---

## auto — Auto-merge and PR automation defaults
<a id="auto"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `auto_merge_label` | `str` | 'harness-auto-merge' | `AUTO_MERGE_LABEL` | — |
| `auto_merge_method` | `str` | 'squash' | `AUTO_MERGE_METHOD` | — |
| `auto_merge_delete_branch` | `bool` | True | `AUTO_MERGE_DELETE_BRANCH` | — |
| `auto_add_label` | `bool` | True | `AUTO_ADD_LABEL` | — |

---

## cli — CLI subcommands and defaults
<a id="cli"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `cli_follow_default_batch_size` | `int` | 10 | `CLI_FOLLOW_DEFAULT_BATCH_SIZE` | ge=1, le=10000 |
| `cli_follow_state_dir` | `Path` | Path('~/.harness').expanduser() | `CLI_FOLLOW_STATE_DIR` | — |

---

## compaction — Context compaction (pre, thresholds, force)
<a id="compaction"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `compaction_enabled` | `bool` | True | `COMPACTION_ENABLED` | — |
| `compaction_threshold_ratio` | `float` | 0.75 | `COMPACTION_THRESHOLD_RATIO` | gt=0.0, lt=1.0 |
| `compaction_target_ratio` | `float` | 0.5 | `COMPACTION_TARGET_RATIO` | gt=0.0, lt=1.0 |
| `compaction_keep_recent_turns` | `int` | 6 | `COMPACTION_KEEP_RECENT_TURNS` | ge=2, le=64 |
| `compaction_summarizer_model` | `str` | '' | `COMPACTION_SUMMARIZER_MODEL` | — |
| `compaction_summarizer_fallback` | `str` | '' | `COMPACTION_SUMMARIZER_FALLBACK` | — |
| `compaction_summarizer_max_input_tokens` | `int` | 0 | `COMPACTION_SUMMARIZER_MAX_INPUT_TOKENS` | ge=0 |
| `compaction_persist_to_memory` | `bool` | True | `COMPACTION_PERSIST_TO_MEMORY` | — |
| `compaction_persistent_store` | `bool` | True | `COMPACTION_PERSISTENT_STORE` | — |
| `compaction_cache_max_versions` | `int` | 5 | `COMPACTION_CACHE_MAX_VERSIONS` | ge=1 |
| `compaction_audit_log` | `bool` | False | `COMPACTION_AUDIT_LOG` | — |
| `compaction_trigger` | `Literal['token', 'turn', 'time', 'hybrid']` | 'token' | `COMPACTION_TRIGGER` | — |
| `compaction_turn_interval` | `int` | 20 | `COMPACTION_TURN_INTERVAL` | ge=1 |
| `compaction_time_idle_minutes` | `int` | 30 | `COMPACTION_TIME_IDLE_MINUTES` | ge=1 |

---

## context — Context window and message handling
<a id="context"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `context_tracking_enabled` | `bool` | True | `CONTEXT_TRACKING_ENABLED` | — |

---

## cors — CORS allowed origins
<a id="cors"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `cors_origins` | `list[str]` | — | `CORS_ORIGINS` | — |

---

## db — Database paths (sessions, scope tokens)
<a id="db"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `db_path` | `Path` | PROJECT_ROOT / 'data' / 'harness.db' | `DB_PATH` | — |

---

## embedding — Embedding model configuration (model id, dimensions)
<a id="embedding"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `embedding_model` | `str` | 'intfloat/multilingual-e5-small' | `EMBEDDING_MODEL` | — |
| `embedding_precision` | `Literal['fp32', 'int8']` | 'int8' | `EMBEDDING_PRECISION` | — |
| `embedding_dim` | `int` | 384 | `EMBEDDING_DIM` | ge=64, le=4096 |

---

## embeddings — Embedding variant / secondary model
<a id="embeddings"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `embeddings_dir` | `Path` | PROJECT_ROOT / 'models' / 'embeddings' | `EMBEDDINGS_DIR` | — |

---

## eval — Evaluation and calibration harness
<a id="eval"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `eval_filler_filter_enabled` | `bool` | True | `EVAL_FILLER_FILTER_ENABLED` | — |
| `eval_reranker_enabled` | `bool` | True | `EVAL_RERANKER_ENABLED` | — |
| `eval_filler_max_doc_len` | `int` | 2000 | `EVAL_FILLER_MAX_DOC_LEN` | ge=100 |

---

## general — General settings (host, port, log_level, project_root)
<a id="general"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `host` | `str` | '0.0.0.0' | `HOST` | — |
| `port` | `int` | 8765 | `PORT` | — |

---

## github — GitHub integration (token, repo)
<a id="github"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `github_token_env` | `str` | 'GITHUB_TOKEN' | `GITHUB_TOKEN_ENV` | — |

---

## hooks — Hook patterns, filters, audit
<a id="hooks"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `hooks_enabled` | `bool` | True | `HOOKS_ENABLED` | — |
| `hooks_default_max_ms` | `int` | 3000 | `HOOKS_DEFAULT_MAX_MS` | ge=100, le=60000 |
| `hooks_max_per_event` | `int` | 10 | `HOOKS_MAX_PER_EVENT` | ge=1, le=100 |
| `hooks_max_recursion_depth` | `int` | 3 | `HOOKS_MAX_RECURSION_DEPTH` | ge=1, le=10 |
| `hooks_subprocess_specs` | `str` | '' | `HOOKS_SUBPROCESS_SPECS` | — |
| `hooks_http_specs` | `str` | '' | `HOOKS_HTTP_SPECS` | — |
| `hooks_llm_specs` | `str` | '' | `HOOKS_LLM_SPECS` | — |
| `hooks_filter_chain` | `str` | '' | `HOOKS_FILTER_CHAIN` | — |
| `hooks_fail_open` | `bool` | True | `HOOKS_FAIL_OPEN` | — |
| `hooks_redact_payloads` | `bool` | True | `HOOKS_REDACT_PAYLOADS` | — |
| `hooks_audit_log` | `bool` | False | `HOOKS_AUDIT_LOG` | — |
| `hooks_subprocess_allowed_paths` | `str` | '.harness/hooks/**' | `HOOKS_SUBPROCESS_ALLOWED_PATHS` | — |
| `hooks_rate_limit_capacity` | `float` | 60.0 | `HOOKS_RATE_LIMIT_CAPACITY` | ge=0.0 |
| `hooks_rate_limit_refill_per_sec` | `float` | 1.0 | `HOOKS_RATE_LIMIT_REFILL_PER_SEC` | ge=0.0 |
| `hooks_rate_limit_enabled` | `bool` | True | `HOOKS_RATE_LIMIT_ENABLED` | — |
| `hooks_circuit_breaker_threshold` | `int` | 5 | `HOOKS_CIRCUIT_BREAKER_THRESHOLD` | ge=1 |
| `hooks_circuit_breaker_cooldown_s` | `float` | 60.0 | `HOOKS_CIRCUIT_BREAKER_COOLDOWN_S` | gt=0.0 |
| `hooks_circuit_breaker_enabled` | `bool` | True | `HOOKS_CIRCUIT_BREAKER_ENABLED` | — |
| `hooks_on_memory_write_silent_layers` | `str` | 'L1' | `HOOKS_ON_MEMORY_WRITE_SILENT_LAYERS` | — |
| `hooks_on_compaction_skip_cache_hit` | `bool` | True | `HOOKS_ON_COMPACTION_SKIP_CACHE_HIT` | — |
| `hooks_pre_tool_use_enabled` | `bool` | True | `HOOKS_PRE_TOOL_USE_ENABLED` | — |
| `hooks_post_tool_use_enabled` | `bool` | True | `HOOKS_POST_TOOL_USE_ENABLED` | — |
| `hooks_stop_enabled` | `bool` | True | `HOOKS_STOP_ENABLED` | — |
| `hooks_subagent_start_enabled` | `bool` | True | `HOOKS_SUBAGENT_START_ENABLED` | — |
| `hooks_subagent_stop_enabled` | `bool` | True | `HOOKS_SUBAGENT_STOP_ENABLED` | — |
| `hooks_session_start_enabled` | `bool` | True | `HOOKS_SESSION_START_ENABLED` | — |
| `hooks_session_end_enabled` | `bool` | True | `HOOKS_SESSION_END_ENABLED` | — |
| `hooks_user_prompt_submit_enabled` | `bool` | True | `HOOKS_USER_PROMPT_SUBMIT_ENABLED` | — |
| `hooks_pre_compact_enabled` | `bool` | True | `HOOKS_PRE_COMPACT_ENABLED` | — |
| `hooks_instructions_loaded_enabled` | `bool` | True | `HOOKS_INSTRUCTIONS_LOADED_ENABLED` | — |
| `hooks_permission_request_enabled` | `bool` | True | `HOOKS_PERMISSION_REQUEST_ENABLED` | — |
| `hooks_on_memory_write_enabled` | `bool` | True | `HOOKS_ON_MEMORY_WRITE_ENABLED` | — |
| `hooks_on_routing_decision_enabled` | `bool` | True | `HOOKS_ON_ROUTING_DECISION_ENABLED` | — |
| `hooks_on_compaction_enabled` | `bool` | True | `HOOKS_ON_COMPACTION_ENABLED` | — |
| `hooks_elicitation_enabled` | `bool` | True | `HOOKS_ELICITATION_ENABLED` | — |
| `hooks_notification_enabled` | `bool` | True | `HOOKS_NOTIFICATION_ENABLED` | — |
| `hooks_builtin_log_enabled` | `bool` | True | `HOOKS_BUILTIN_LOG_ENABLED` | — |
| `hooks_builtin_validate_enabled` | `bool` | True | `HOOKS_BUILTIN_VALIDATE_ENABLED` | — |
| `hooks_builtin_block_dangerous_enabled` | `bool` | True | `HOOKS_BUILTIN_BLOCK_DANGEROUS_ENABLED` | — |
| `hooks_builtin_inject_context_enabled` | `bool` | False | `HOOKS_BUILTIN_INJECT_CONTEXT_ENABLED` | — |
| `hooks_builtin_autosave_enabled` | `bool` | True | `HOOKS_BUILTIN_AUTOSAVE_ENABLED` | — |
| `hooks_builtin_confirm_dangerous_enabled` | `bool` | True | `HOOKS_BUILTIN_CONFIRM_DANGEROUS_ENABLED` | — |
| `hooks_builtin_notify_terminal_enabled` | `bool` | True | `HOOKS_BUILTIN_NOTIFY_TERMINAL_ENABLED` | — |
| `hooks_builtin_secret_detect_enabled` | `bool` | True | `HOOKS_BUILTIN_SECRET_DETECT_ENABLED` | — |
| `hooks_builtin_sql_injection_guard_enabled` | `bool` | True | `HOOKS_BUILTIN_SQL_INJECTION_GUARD_ENABLED` | — |
| `hooks_builtin_unsafe_import_block_enabled` | `bool` | True | `HOOKS_BUILTIN_UNSAFE_IMPORT_BLOCK_ENABLED` | — |
| `hooks_unsafe_imports_blocklist` | `str` | 'os.system,subprocess,eval,exec,pickle,yaml.load,requests.post' | `HOOKS_UNSAFE_IMPORTS_BLOCKLIST` | — |
| `hooks_notify_webhook_url` | `str` | '' | `HOOKS_NOTIFY_WEBHOOK_URL` | — |
| `hooks_notify_webhook_secret` | `str` | '' | `HOOKS_NOTIFY_WEBHOOK_SECRET` | — |
| `hooks_notify_webhook_timeout_s` | `float` | 5.0 | `HOOKS_NOTIFY_WEBHOOK_TIMEOUT_S` | — |
| `hooks_notify_desktop_enabled` | `bool` | False | `HOOKS_NOTIFY_DESKTOP_ENABLED` | — |
| `hooks_notify_slack_webhook_url` | `str` | '' | `HOOKS_NOTIFY_SLACK_WEBHOOK_URL` | — |
| `hooks_notify_slack_channel` | `str` | '' | `HOOKS_NOTIFY_SLACK_CHANNEL` | — |
| `hooks_notify_slack_username` | `str` | 'Solomon Harness' | `HOOKS_NOTIFY_SLACK_USERNAME` | — |
| `hooks_notify_slack_timeout_s` | `float` | 5.0 | `HOOKS_NOTIFY_SLACK_TIMEOUT_S` | — |
| `hooks_notify_teams_webhook_url` | `str` | '' | `HOOKS_NOTIFY_TEAMS_WEBHOOK_URL` | — |
| `hooks_notify_teams_timeout_s` | `float` | 5.0 | `HOOKS_NOTIFY_TEAMS_TIMEOUT_S` | — |
| `hooks_notify_max_retries` | `int` | 3 | `HOOKS_NOTIFY_MAX_RETRIES` | ge=0, le=20 |
| `hooks_notify_retry_initial_delay_ms` | `int` | 100 | `HOOKS_NOTIFY_RETRY_INITIAL_DELAY_MS` | ge=0, le=60000 |
| `hooks_notify_retry_max_delay_ms` | `int` | 5000 | `HOOKS_NOTIFY_RETRY_MAX_DELAY_MS` | ge=1, le=300000 |
| `hooks_notify_dlq_enabled` | `bool` | True | `HOOKS_NOTIFY_DLQ_ENABLED` | — |
| `hooks_elicitation_ws_enabled` | `bool` | True | `HOOKS_ELICITATION_WS_ENABLED` | — |
| `hooks_elicitation_ws_timeout_s` | `float` | 30.0 | `HOOKS_ELICITATION_WS_TIMEOUT_S` | — |
| `hooks_elicitation_longpoll_enabled` | `bool` | False | `HOOKS_ELICITATION_LONGPOLL_ENABLED` | — |
| `hooks_elicitation_longpoll_timeout_s` | `float` | 30.0 | `HOOKS_ELICITATION_LONGPOLL_TIMEOUT_S` | — |
| `hooks_elicitation_longpoll_interval_s` | `float` | 0.25 | `HOOKS_ELICITATION_LONGPOLL_INTERVAL_S` | — |
| `hooks_elicitation_sse_enabled` | `bool` | False | `HOOKS_ELICITATION_SSE_ENABLED` | — |
| `hooks_elicitation_sse_heartbeat_s` | `float` | 15.0 | `HOOKS_ELICITATION_SSE_HEARTBEAT_S` | — |
| `hooks_elicitation_sse_max_session_age_s` | `float` | 3600.0 | `HOOKS_ELICITATION_SSE_MAX_SESSION_AGE_S` | — |
| `hooks_observability_admin_enabled` | `bool` | True | `HOOKS_OBSERVABILITY_ADMIN_ENABLED` | — |
| `hooks_observability_admin_audit_max_limit` | `int` | 500 | `HOOKS_OBSERVABILITY_ADMIN_AUDIT_MAX_LIMIT` | ge=1, le=10000 |
| `hooks_observability_admin_metrics_filter` | `str` | '' | `HOOKS_OBSERVABILITY_ADMIN_METRICS_FILTER` | — |
| `hooks_admin_enabled` | `bool` | True | `HOOKS_ADMIN_ENABLED` | — |

---

## hot — Hot-reload configuration (file watcher, intervals)
<a id="hot"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `hot_reload_enabled` | `bool` | True | `HOT_RELOAD_ENABLED` | — |
| `hot_reload_debounce_ms` | `int` | 200 | `HOT_RELOAD_DEBOUNCE_MS` | ge=0, le=5000 |
| `hot_reload_poll_interval_s` | `float` | 1.0 | `HOT_RELOAD_POLL_INTERVAL_S` | gt=0, le=60.0 |

---

## legacy — Legacy compatibility flags (deprecated, do not use in new code)
<a id="legacy"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `legacy_apis_gone_enabled` | `bool` | False | `LEGACY_APIS_GONE_ENABLED` | — |

---

## llm — LLM provider settings (catalog, defaults)
<a id="llm"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `llm_usage_tracking_enabled` | `bool` | True | `LLM_USAGE_TRACKING_ENABLED` | — |
| `llm_usage_log_path` | `Path` | Path('data/llm_usage.jsonl') | `LLM_USAGE_LOG_PATH` | — |

---

## log — Logging configuration (level, format, sinks)
<a id="log"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `log_level` | `str` | 'INFO' | `LOG_LEVEL` | — |

---

## manual — Manual override and CLI-only flags
<a id="manual"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `manual_compact_max_ms` | `int` | 30000 | `MANUAL_COMPACT_MAX_MS` | ge=1000, le=120000 |

---

## max — Maximum limits (iterations, payload sizes)
<a id="max"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `max_iterations` | `int` | 5 | `MAX_ITERATIONS` | ge=1, le=20 |

---

## minimax — MiniMax (Anthropic-compatible) API key
<a id="minimax"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `minimax_api_key` | `str` | '' | `MINIMAX_API_KEY` | — |

---

## moonshot — Moonshot (Kimi) API key
<a id="moonshot"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `moonshot_api_key` | `str` | '' | `MOONSHOT_API_KEY` | — |

---

## observability — Metrics, traces, and monitoring
<a id="observability"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `observability_enabled` | `bool` | True | `OBSERVABILITY_ENABLED` | — |
| `observability_jsonl_enabled` | `bool` | True | `OBSERVABILITY_JSONL_ENABLED` | — |
| `observability_prometheus_enabled` | `bool` | False | `OBSERVABILITY_PROMETHEUS_ENABLED` | — |
| `observability_otlp_enabled` | `bool` | False | `OBSERVABILITY_OTLP_ENABLED` | — |
| `observability_log_dir` | `Path` | PROJECT_ROOT / 'data' / 'logs' | `OBSERVABILITY_LOG_DIR` | — |
| `observability_log_max_files` | `int` | 30 | `OBSERVABILITY_LOG_MAX_FILES` | ge=1, le=365 |
| `observability_log_max_file_size_mb` | `int` | 100 | `OBSERVABILITY_LOG_MAX_FILE_SIZE_MB` | ge=1, le=1024 |
| `observability_metrics_path` | `str` | '/metrics' | `OBSERVABILITY_METRICS_PATH` | — |
| `observability_metrics_namespace` | `str` | 'harness' | `OBSERVABILITY_METRICS_NAMESPACE` | — |
| `observability_otlp_endpoint` | `str` | '' | `OBSERVABILITY_OTLP_ENDPOINT` | — |
| `observability_otlp_headers` | `str` | '' | `OBSERVABILITY_OTLP_HEADERS` | — |
| `observability_trace_sample_ratio` | `float` | 1.0 | `OBSERVABILITY_TRACE_SAMPLE_RATIO` | ge=0.0, le=1.0 |
| `observability_health_ready_timeout_s` | `float` | 2.0 | `OBSERVABILITY_HEALTH_READY_TIMEOUT_S` | gt=0, le=30.0 |
| `observability_health_deep_timeout_s` | `float` | 5.0 | `OBSERVABILITY_HEALTH_DEEP_TIMEOUT_S` | gt=0, le=60.0 |
| `observability_health_require_qdrant` | `bool` | False | `OBSERVABILITY_HEALTH_REQUIRE_QDRANT` | — |
| `observability_health_require_neo4j` | `bool` | False | `OBSERVABILITY_HEALTH_REQUIRE_NEO4J` | — |
| `observability_cost_enabled` | `bool` | True | `OBSERVABILITY_COST_ENABLED` | — |
| `observability_cost_overrides` | `str` | '' | `OBSERVABILITY_COST_OVERRIDES` | — |
| `observability_log_http_requests` | `bool` | True | `OBSERVABILITY_LOG_HTTP_REQUESTS` | — |
| `observability_log_llm_calls` | `bool` | True | `OBSERVABILITY_LOG_LLM_CALLS` | — |
| `observability_log_tool_calls` | `bool` | True | `OBSERVABILITY_LOG_TOOL_CALLS` | — |
| `observability_log_hook_dispatches` | `bool` | True | `OBSERVABILITY_LOG_HOOK_DISPATCHES` | — |
| `observability_log_compactions` | `bool` | True | `OBSERVABILITY_LOG_COMPACTIONS` | — |
| `observability_log_merge_queue_events` | `bool` | True | `OBSERVABILITY_LOG_MERGE_QUEUE_EVENTS` | — |
| `observability_log_outbound_deliveries` | `bool` | True | `OBSERVABILITY_LOG_OUTBOUND_DELIVERIES` | — |
| `observability_log_privacy_decisions` | `bool` | True | `OBSERVABILITY_LOG_PRIVACY_DECISIONS` | — |

---

## outbound — Outbound notifications (Slack, Teams, webhooks)
<a id="outbound"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `outbound_webhook_urls` | `str` | '' | `OUTBOUND_WEBHOOK_URLS` | — |
| `outbound_webhook_token` | `str` | '' | `OUTBOUND_WEBHOOK_TOKEN` | — |
| `outbound_webhook_timeout_s` | `float` | 5.0 | `OUTBOUND_WEBHOOK_TIMEOUT_S` | ge=0.5, le=60.0 |
| `outbound_webhook_max_retries` | `int` | 3 | `OUTBOUND_WEBHOOK_MAX_RETRIES` | ge=0, le=10 |

---

## plugins — Plugin discovery, dispatch, trust
<a id="plugins"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `plugins_enabled` | `bool` | False | `PLUGINS_ENABLED` | — |
| `plugins_dir` | `Path` | Path('.harness/plugins') | `PLUGINS_DIR` | — |
| `plugins_allowed` | `list[str]` | — | `PLUGINS_ALLOWED` | — |
| `plugins_dispatch_enabled` | `bool` | True | `PLUGINS_DISPATCH_ENABLED` | — |
| `plugins_admin_enabled` | `bool` | True | `PLUGINS_ADMIN_ENABLED` | — |

---

## pr — Pull request automation (strategy, polling, timeout)
<a id="pr"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `pr_default_target_branch` | `str` | 'main' | `PR_DEFAULT_TARGET_BRANCH` | — |
| `pr_poll_interval_s` | `float` | 15.0 | `PR_POLL_INTERVAL_S` | gt=0.0 |
| `pr_wait_timeout_s` | `float` | 300.0 | `PR_WAIT_TIMEOUT_S` | gt=0.0 |
| `pr_strategy` | `str` | 'auto' | `PR_STRATEGY` | — |
| `pr_split_strategy` | `str` | 'auto' | `PR_SPLIT_STRATEGY` | — |
| `pr_split_max_files_per_slice` | `int` | 10 | `PR_SPLIT_MAX_FILES_PER_SLICE` | ge=1, le=1000 |
| `pr_split_min_slices` | `int` | 1 | `PR_SPLIT_MIN_SLICES` | ge=1 |
| `pr_split_max_slices` | `int` | 8 | `PR_SPLIT_MAX_SLICES` | ge=1, le=64 |
| `pr_template_path` | `str` | '' | `PR_TEMPLATE_PATH` | — |
| `pr_issue_link_re` | `str` | '#(\\d+)' | `PR_ISSUE_LINK_RE` | — |
| `pr_review_timeout_s` | `int` | 86400 | `PR_REVIEW_TIMEOUT_S` | ge=60 |
| `pr_review_poll_interval_s` | `int` | 30 | `PR_REVIEW_POLL_INTERVAL_S` | ge=5, le=600 |
| `pr_rate_limit_max_retries` | `int` | 5 | `PR_RATE_LIMIT_MAX_RETRIES` | ge=0, le=20 |
| `pr_rate_limit_initial_backoff_s` | `float` | 2.0 | `PR_RATE_LIMIT_INITIAL_BACKOFF_S` | ge=0.0, le=60.0 |
| `pr_rate_limit_max_backoff_s` | `float` | 60.0 | `PR_RATE_LIMIT_MAX_BACKOFF_S` | ge=1.0, le=600.0 |
| `pr_rate_limit_jitter_s` | `float` | 0.5 | `PR_RATE_LIMIT_JITTER_S` | ge=0.0, le=10.0 |

---

## pre — Pre-execution hooks and checks
<a id="pre"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `pre_compact_enabled` | `bool` | True | `PRE_COMPACT_ENABLED` | — |
| `pre_compact_max_ms` | `int` | 5000 | `PRE_COMPACT_MAX_MS` | ge=1 |
| `pre_compact_save_fields` | `str` | 'messages_last_n,plan_step,hot_l0,metadata' | `PRE_COMPACT_SAVE_FIELDS` | — |

---

## privacy — Privacy zones and redaction
<a id="privacy"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `privacy_zones_enabled` | `bool` | True | `PRIVACY_ZONES_ENABLED` | — |
| `privacy_zone_patterns` | `str` | '' | `PRIVACY_ZONE_PATTERNS` | — |
| `privacy_zone_default_action` | `Literal['block', 'redact', 'skip']` | 'block' | `PRIVACY_ZONE_DEFAULT_ACTION` | — |
| `privacy_zone_per_action` | `str` | '' | `PRIVACY_ZONE_PER_ACTION` | — |
| `privacy_zones_audit_log` | `bool` | False | `PRIVACY_ZONES_AUDIT_LOG` | — |
| `privacy_zones_admin_enabled` | `bool` | False | `PRIVACY_ZONES_ADMIN_ENABLED` | — |

---

## project — Project root paths and resolution
<a id="project"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `project_root` | `Path` | Path('C:/MyAI') | `PROJECT_ROOT` | — |

---

## prompt — Prompt template configuration
<a id="prompt"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `prompt_cache_enabled` | `bool` | True | `PROMPT_CACHE_ENABLED` | — |
| `prompt_cache_strategy` | `Literal['anthropic', 'vllm', 'off']` | 'off' | `PROMPT_CACHE_STRATEGY` | — |

---

## redaction — PII redaction patterns (12 built-in)
<a id="redaction"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `redaction_enabled` | `bool` | True | `REDACTION_ENABLED` | — |
| `redaction_categories` | `str` | '' | `REDACTION_CATEGORIES` | — |
| `redaction_audit_log` | `bool` | False | `REDACTION_AUDIT_LOG` | — |

---

## reflection — Reflection patterns (T1→T2 escalation)
<a id="reflection"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `reflection_enabled` | `bool` | True | `REFLECTION_ENABLED` | — |
| `reflection_max_lessons` | `int` | 5 | `REFLECTION_MAX_LESSONS` | ge=1, le=20 |
| `reflection_max_ms` | `int` | 10000 | `REFLECTION_MAX_MS` | ge=100, le=60000 |
| `reflection_model` | `str` | '' | `REFLECTION_MODEL` | — |
| `reflection_fallback_model` | `str` | '' | `REFLECTION_FALLBACK_MODEL` | — |

---

## scratchpad — Scratchpad / L0 system prompt
<a id="scratchpad"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `scratchpad_enabled` | `bool` | True | `SCRATCHPAD_ENABLED` | — |
| `scratchpad_max_notes_per_session` | `int` | 100 | `SCRATCHPAD_MAX_NOTES_PER_SESSION` | ge=1 |
| `scratchpad_l0_max_bytes` | `int` | 1024 | `SCRATCHPAD_L0_MAX_BYTES` | ge=128 |
| `scratchpad_audit_log` | `bool` | False | `SCRATCHPAD_AUDIT_LOG` | — |
| `scratchpad_inject_l0_to_system_prompt` | `bool` | True | `SCRATCHPAD_INJECT_L0_TO_SYSTEM_PROMPT` | — |
| `scratchpad_l2_qdrant_url` | `str | None` | None | `SCRATCHPAD_L2_QDRANT_URL` | — |
| `scratchpad_l2_qdrant_collection` | `str` | 'scratchpad_l2' | `SCRATCHPAD_L2_QDRANT_COLLECTION` | — |

---

## session — Session storage and metadata
<a id="session"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `session_dir` | `Path` | PROJECT_ROOT / 'data' / 'sessions' | `SESSION_DIR` | — |

---

## subagent — Sub-agent routing and configuration
<a id="subagent"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `subagent_default_model` | `str` | 'MiniMax-M2.7' | `SUBAGENT_DEFAULT_MODEL` | — |
| `subagent_judges` | `int` | 2 | `SUBAGENT_JUDGES` | ge=1, le=5 |
| `subagent_timeout_s` | `float` | 300.0 | `SUBAGENT_TIMEOUT_S` | gt=0 |
| `subagent_t1_model` | `str` | 'qwen3:8b' | `SUBAGENT_T1_MODEL` | — |
| `subagent_t2_model` | `str` | 'glm-4.7' | `SUBAGENT_T2_MODEL` | — |
| `subagent_confidence_high` | `float` | 0.6 | `SUBAGENT_CONFIDENCE_HIGH` | ge=0.0, le=1.0 |
| `subagent_confidence_low` | `float` | 0.3 | `SUBAGENT_CONFIDENCE_LOW` | ge=0.0, le=1.0 |

---

## tier — Tier-based LLM routing (T1/T2/T3)
<a id="tier"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `tier_routing_heuristic_enabled` | `bool` | True | `TIER_ROUTING_HEURISTIC_ENABLED` | — |
| `tier_routing_t1_max_prompt_chars` | `int` | 1000 | `TIER_ROUTING_T1_MAX_PROMPT_CHARS` | ge=1 |
| `tier_routing_t1_max_context_tokens` | `int` | 2000 | `TIER_ROUTING_T1_MAX_CONTEXT_TOKENS` | ge=1 |
| `tier_routing_t3_min_prompt_chars` | `int` | 10000 | `TIER_ROUTING_T3_MIN_PROMPT_CHARS` | ge=1 |
| `tier_routing_t3_min_context_tokens` | `int` | 16000 | `TIER_ROUTING_T3_MIN_CONTEXT_TOKENS` | ge=1 |
| `tier_routing_complexity_keywords` | `list[str]` | — | `TIER_ROUTING_COMPLEXITY_KEYWORDS` | — |

---

## tool — Tool execution and sandboxing
<a id="tool"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `tool_offload_enabled` | `bool` | True | `TOOL_OFFLOAD_ENABLED` | — |
| `tool_offload_threshold_bytes` | `int` | 25600 | `TOOL_OFFLOAD_THRESHOLD_BYTES` | ge=1024 |
| `tool_offload_preview_lines` | `int` | 3 | `TOOL_OFFLOAD_PREVIEW_LINES` | ge=1, le=20 |
| `tool_offload_preview_max_chars` | `int` | 600 | `TOOL_OFFLOAD_PREVIEW_MAX_CHARS` | ge=64, le=4096 |
| `tool_offload_read_max_bytes` | `int` | 4096 | `TOOL_OFFLOAD_READ_MAX_BYTES` | ge=256 |
| `tool_offload_max_ms` | `int` | 2000 | `TOOL_OFFLOAD_MAX_MS` | ge=100, le=60000 |

---

## trust — Plugin trust registry (signed manifests)
<a id="trust"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `trust_registry_path` | `Path` | Path('trust-registry.json') | `TRUST_REGISTRY_PATH` | — |
| `trust_registry_hot_reload` | `bool` | True | `TRUST_REGISTRY_HOT_RELOAD` | — |
| `trust_registry_poll_interval` | `int` | 5 | `TRUST_REGISTRY_POLL_INTERVAL` | ge=1, le=60 |

---

## web — Web UI server config (FastAPI mount, static paths)
<a id="web"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `web_ui_enabled` | `bool` | True | `WEB_UI_ENABLED` | — |
| `web_dist_path` | `Path` | Path('web/dist') | `WEB_DIST_PATH` | — |
| `web_ui_route_prefix` | `str` | '/ui' | `WEB_UI_ROUTE_PREFIX` | — |

---

## webhook — Inbound + outbound webhook delivery
<a id="webhook"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `webhook_secret` | `str` | '' | `WEBHOOK_SECRET` | — |
| `webhook_path` | `str` | '/api/v1/agents/webhooks/github' | `WEBHOOK_PATH` | — |
| `webhook_max_payload_kb` | `int` | 256 | `WEBHOOK_MAX_PAYLOAD_KB` | ge=1, le=10240 |

---

## ws — WebSocket transport (backpressure, heartbeat)
<a id="ws"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `ws_metrics_interval_s` | `float` | 1.0 | `WS_METRICS_INTERVAL_S` | gt=0.0, le=60.0 |
| `ws_heartbeat_s` | `float` | 30.0 | `WS_HEARTBEAT_S` | gt=0.0, le=300.0 |
| `ws_max_backlog` | `int` | 100 | `WS_MAX_BACKLOG` | ge=1, le=10000 |

---

## zhipuai — ZhipuAI (GLM models) API key
<a id="zhipuai"></a>

| Setting | Type | Default | Env var | Constraints |
|---------|------|---------|---------|-------------|
| `zhipuai_api_key` | `str` | '' | `ZHIPUAI_API_KEY` | — |

---
