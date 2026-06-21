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

            # Get default value
            default_str = ""
            if item.value and isinstance(item.value, ast.Call):
                # Field(default=..., description=..., alias=...)
                call = item.value
                if isinstance(call.func, ast.Name) and call.func.id == "Field":
                    for kw in call.keywords:
                        if kw.arg == "default":
                            default_str = ast.unparse(kw.value)
                        elif kw.arg == "alias":
                            default_str += f" (env: `{ast.unparse(kw.value)}`)"
                        elif kw.arg == "description":
                            desc = ast.unparse(kw.value).strip("\"'")
                            default_str += f" — {desc}"

            settings.append({
                "class": class_name,
                "name": field_name,
                "type": type_str,
                "default": default_str,
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
        "subagent": "Sub-agent routing and configuration",
        "tier": "Tier-based LLM routing (T1/T2/T3)",
        "plugin": "Plugin system configuration",
        "privacy": "Privacy zones and redaction",
        "llm": "LLM provider settings",
        "memory": "Memory and persistence",
        "tool": "Tool execution and sandboxing",
        "audit": "Audit logging",
        "observability": "Metrics, traces, and monitoring",
        "auth": "Authentication and authorization",
        "log": "Logging configuration",
        "request": "HTTP request handling",
        "webhook": "Outbound webhook delivery",
        "ratelimit": "Rate limiting",
        "circuit": "Circuit breaker",
        "compaction": "Context compaction",
        "elicit": "Elicitation framework",
        "trust": "Plugin trust registry",
        "scratchpad": "Scratchpad / L0 system prompt",
        "health": "Health checks and deep probes",
        "hot": "Hot-reload configuration",
        "capability": "Capability discovery",
        "general": "General settings (single-word or uncategorized)",
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
    md.append("All settings can be overridden via environment variables (uppercase + underscores).")
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
        md.append("| Setting | Type | Default |")
        md.append("|---------|------|---------|")

        for s in items:
            name = s["name"]
            type_str = s["type"]
            default = s["default"] or "—"
            # Escape pipes
            default = default.replace("|", "\\|")
            md.append(f"| `{name}` | `{type_str}` | {default} |")

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
