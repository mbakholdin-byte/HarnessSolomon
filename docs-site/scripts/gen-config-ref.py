#!/usr/bin/env python3
"""
gen-config-ref.py — generate Configuration Reference from Pydantic Settings.

Usage:
  python gen-config-ref.py [config_path] [output_path]
  python gen-config-ref.py ../harness/config.py ./docs/configuration/reference.md

Output: Markdown table with all settings grouped by section.
"""
import ast
import sys
from pathlib import Path
from datetime import datetime
import re

DEFAULT_CONFIG = Path(__file__).parent.parent.parent / "harness" / "config.py"
DEFAULT_OUTPUT = Path(__file__).parent.parent / "docs" / "configuration" / "reference.md"


def extract_settings(config_path: Path) -> list[dict]:
    """Parse Pydantic Settings class and extract all Field definitions."""
    source = config_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    settings = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        # Find class inheriting from BaseSettings or BaseModel
        is_settings = any(
            (isinstance(base, ast.Name) and "Settings" in base.id) or
            (isinstance(base, ast.Attribute) and "Settings" in base.attr)
            for base in node.bases
        )
        if not is_settings:
            continue

        class_name = node.name

        for item in node.body:
            if not isinstance(item, ast.AnnAssign) or not isinstance(item.target, ast.Name):
                continue

            field_name = item.target.id

            # Get type annotation as string
            type_str = ast.unparse(item.annotation) if item.annotation else "Any"

            # Get default value, description, env alias, constraints
            default_str = ""
            description_str = ""
            env_alias = ""
            constraints = []
            deprecated = False

            if item.value and isinstance(item.value, ast.Call):
                # Field(default=..., description=..., alias=..., ge=..., le=..., gt=..., deprecated=...)
                call = item.value
                if isinstance(call.func, ast.Name) and call.func.id == "Field":
                    for kw in call.keywords:
                        if kw.arg == "default":
                            default_str = ast.unparse(kw.value)
                        elif kw.arg == "alias":
                            env_alias = ast.unparse(kw.value).strip("\"'")
                        elif kw.arg == "description":
                            description_str = ast.unparse(kw.value).strip("\"'")
                        elif kw.arg in ("ge", "le", "gt", "lt"):
                            constraints.append(f"{kw.arg}={ast.unparse(kw.value)}")
                        elif kw.arg == "deprecated" and isinstance(kw.value, ast.Constant) and kw.value.value:
                            deprecated = True

            settings.append({
                "class": class_name,
                "name": field_name,
                "type": type_str,
                "default": default_str,
                "description": description_str,
                "env_alias": env_alias,
                "constraints": constraints,
                "deprecated": deprecated,
            })

    return settings


def group_by_section(settings: list[dict]) -> dict[str, list[dict]]:
    """Group settings by FIRST underscore-separated word (e.g., `tier_*` → tier section)."""
    sections = {}
    for s in settings:
        name = s["name"]
        parts = name.split("_")
        if len(parts) == 1 or "_" not in name:
            section = "general"
        else:
            section = parts[0]

        sections.setdefault(section, []).append(s)

    return sections


def section_description(section: str) -> str:
    """Human-readable description for section."""
    descriptions = {
        # === Core server / paths ===
        "general": "General settings (host, port, log_level, project_root)",
        "host": "HTTP server bind host and port",
        "log": "Logging configuration (level, format, sinks)",
        "session": "Session storage and metadata",
        "db": "Database paths (sessions, scope tokens)",
        "project": "Project root paths and resolution",
        "cors": "CORS allowed origins",
        "max": "Maximum limits (iterations, payload sizes)",
        "context": "Context window and message handling",
        "manual": "Manual override and CLI-only flags",

        # === LLM providers ===
        "llm": "LLM provider settings (catalog, defaults)",
        "minimax": "MiniMax (Anthropic-compatible) API key",
        "zhipuai": "ZhipuAI (GLM models) API key",
        "moonshot": "Moonshot (Kimi) API key",
        "embedding": "Embedding model configuration (model id, dimensions)",
        "embeddings": "Embedding variant / secondary model",
        "prompt": "Prompt template configuration",

        # === Sub-agents and routing ===
        "subagent": "Sub-agent routing and configuration",
        "agents": "Sub-agent directory location",
        "tier": "Tier-based LLM routing (T1/T2/T3)",
        "cli": "CLI subcommands and defaults",
        "auto": "Auto-merge and PR automation defaults",

        # === Plugins and hooks ===
        "plugin": "Plugin system configuration",
        "plugins": "Plugin discovery, dispatch, trust",
        "hook": "Hook system framework (14 events, 4 transports)",
        "hooks": "Hook patterns, filters, audit",
        "trust": "Plugin trust registry (signed manifests)",
        "hot": "Hot-reload configuration (file watcher, intervals)",

        # === Privacy and redaction ===
        "privacy": "Privacy zones and redaction",
        "redaction": "PII redaction patterns (12 built-in)",

        # === API and auth ===
        "auth": "Authentication and authorization (Bearer tokens, scopes)",
        "scope": "Scope definitions and RBAC",

        # === Observability ===
        "observability": "Metrics, traces, and monitoring",
        "metric": "Metrics export and aggregation",
        "audit": "Audit logging (JSONL, SQLite)",
        "log_level": "Log level overrides per module",
        "health": "Health checks and deep probes",

        # === Memory and persistence ===
        "memory": "Memory and persistence (L0/L1/L2, RRF)",
        "scratchpad": "Scratchpad / L0 system prompt",

        # === Tool execution ===
        "tool": "Tool execution and sandboxing",
        "tools": "Tool registry and permission gates",

        # === GitHub integration ===
        "github": "GitHub integration (token, repo)",
        "pr": "Pull request automation (strategy, polling, timeout)",
        "webhook": "Inbound + outbound webhook delivery",

        # === Compaction and reflection ===
        "compaction": "Context compaction (pre, thresholds, force)",
        "pre": "Pre-execution hooks and checks",
        "reflection": "Reflection patterns (T1→T2 escalation)",

        # === Outbound and notifications ===
        "outbound": "Outbound notifications (Slack, Teams, webhooks)",

        # === Rate limiting / circuit breaker ===
        "ratelimit": "Rate limiting (token bucket, per-route)",
        "circuit": "Circuit breaker (failure threshold, recovery)",

        # === Elicitation ===
        "elicit": "Elicitation framework (permission prompts, broker)",

        # === Web UI / transport ===
        "web": "Web UI server config (FastAPI mount, static paths)",
        "ws": "WebSocket transport (backpressure, heartbeat)",

        # === Eval / calibration ===
        "eval": "Evaluation and calibration harness",

        # === Legacy ===
        "legacy": "Legacy compatibility flags (deprecated, do not use in new code)",
    }
    return descriptions.get(section, "Configuration")


def render_markdown(settings: list[dict], config_path: Path) -> str:
    """Render settings as Markdown."""
    sections = group_by_section(settings)
    total = len(settings)

    md = []
    md.append("# Configuration Reference")
    md.append("")
    md.append(f"> **Auto-generated** from `{config_path.name}` on {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    md.append(f"> Total: **{total}** settings in **{len(sections)}** sections")
    md.append("")
    md.append("All settings can be overridden via environment variables. By default, the env var name is the setting name uppercased (e.g. `subagent_judges` → `SUBAGENT_JUDGES`). If `alias=` is set on the field, the explicit alias is used instead.")
    md.append("")
    md.append("**Constraints:** `ge=`/`le=`/`gt=`/`lt=` are Pydantic field constraints applied at validation time.")
    md.append("")

    # Table of contents
    md.append("## Table of Contents")
    md.append("")
    for section in sorted(sections.keys()):
        anchor = section.lower().replace("_", "-")
        count = len(sections[section])
        md.append(f"- [{section_description(section)}](#{anchor}) ({count} settings)")
    md.append("")
    md.append("---")
    md.append("")

    # Each section
    for section in sorted(sections.keys()):
        anchor = section.lower().replace("_", "-")
        md.append(f"## {section} — {section_description(section)}")
        md.append(f"<a id=\"{anchor}\"></a>")
        md.append("")

        items = sections[section]
        md.append("| Setting | Type | Default | Env var | Constraints |")
        md.append("|---------|------|---------|---------|-------------|")

        for s in items:
            name = s["name"]
            type_str = s["type"]
            default = s["default"] or "—"
            # Escape pipes
            default = default.replace("|", "\\|")
            # Env var: explicit alias if set, else uppercased name
            env_var = s["env_alias"] if s["env_alias"] else name.upper()
            constraints = ", ".join(s["constraints"]) if s["constraints"] else "—"
            # Add deprecated marker
            deprecated_marker = " ⚠️ **deprecated**" if s["deprecated"] else ""
            md.append(f"| `{name}`{deprecated_marker} | `{type_str}` | {default} | `{env_var}` | {constraints} |")

        md.append("")
        md.append("---")
        md.append("")

    return "\n".join(md)


def main():
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CONFIG
    output_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUTPUT

    if not config_path.exists():
        print(f"Error: config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    print(f"[gen-config-ref] parsing: {config_path}")
    settings = extract_settings(config_path)
    print(f"[gen-config-ref] found {len(settings)} settings")

    if not settings:
        print("Warning: no settings found. Check that config.py has Field() definitions.", file=sys.stderr)
        sys.exit(1)

    md = render_markdown(settings, config_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(md, encoding="utf-8")

    print(f"[gen-config-ref] wrote: {output_path} ({len(md):,} bytes, {md.count(chr(10))+1} lines)")


if __name__ == "__main__":
    main()
