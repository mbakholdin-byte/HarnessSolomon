"""Sub-agent spec — Pydantic model + .md frontmatter parser (Phase 2.0, Step 1).

A sub-agent is defined by a ``.md`` file with YAML frontmatter:

    ---
    name: explore
    model: MiniMax-M2.7
    tools: [read_file, grep, glob]
    permissions: read-only
    max_iterations: 8
    worktree_required: true
    allowed_paths: []
    ---
    You are the explore sub-agent. ...

The frontmatter is parsed with a tiny hand-rolled reader (mirror of
``harness/memory/adapters/file.py:43``) — full PyYAML would add a dep we
don't need for the simple ``key: value`` and ``key: [a, b, c]`` lines we use.

The parser is intentionally strict: unknown fields raise ``FrontmatterParseError``.
This catches typos (``max_iterrations: 5``) early and makes the agent contract
explicit.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from harness.config import settings
from harness.server.llm.models import get_model

logger = logging.getLogger(__name__)


# === Errors ===

class FrontmatterParseError(ValueError):
    """Raised when an agent ``.md`` file has malformed / unknown frontmatter."""


# === Schema ===

#: Default tool allowlist for new agents (read-only reconnaissance).
DEFAULT_TOOLS: list[str] = ["read_file", "grep", "glob"]

#: Default permissions for new agents (read-only is the safest default).
DEFAULT_PERMISSIONS: Literal["read-only", "scoped-write", "full"] = "read-only"

#: Frontmatter regex: ``---\\nKEY: VAL\\n...\\n---\\nBODY``.
#: Body may be empty.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)

#: Single key: value line. Keys are kebab/snake; values are either a scalar
#: or an inline YAML list ``[a, b, c]``.
_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$")

#: kebab-case validator (lowercase letters, digits, hyphens).
_KEBAB_CASE_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")


class AgentSpec(BaseModel):
    """A sub-agent definition: capabilities + system prompt.

    Loaded from ``.harness/agents/<name>.md`` (or a built-in). The
    ``system_prompt`` field comes from the markdown body (text after
    the closing ``---``).
    """

    model_config = ConfigDict(
        extra="forbid",          # unknown fields raise (not silently ignored)
        frozen=True,             # specs are immutable; rebuild on edit
    )

    name: str = Field(min_length=1, max_length=64)
    model: str = Field(default="")  # empty → substituted by parse_agent_md from settings
    tools: list[str] = Field(default_factory=lambda: list(DEFAULT_TOOLS))
    permissions: Literal["read-only", "scoped-write", "full"] = DEFAULT_PERMISSIONS
    system_prompt: str = Field(default="")
    max_iterations: int = Field(default=5, ge=1, le=20)
    worktree_required: bool = True
    allowed_paths: list[str] = Field(default_factory=list)
    #: Phase 2.1 — per-agent memory namespace. When ``None`` (the
    #: default), the sub-agent shares the parent memory
    #: (``UnifiedMemory(agent_id="solomon")``). When set, the runner
    #: constructs / reuses a per-spec :class:`UnifiedMemory` with
    #: that namespace, giving the agent an isolated store.
    #: Built-ins keep this ``None`` so they share solomon by default.
    memory_namespace: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, v: str) -> str:
        if not _KEBAB_CASE_RE.match(v):
            raise ValueError(
                f"agent name must be kebab-case (lowercase letters, digits, hyphens), got {v!r}"
            )
        return v

    @field_validator("memory_namespace")
    @classmethod
    def _validate_memory_namespace(cls, v: str | None) -> str | None:
        """Reuse the kebab-case rule for the namespace identifier.

        We accept the same shape as agent names (lowercase + digits +
        hyphens) so a sub-agent's ``memory_namespace`` can be derived
        from its own name without a separate registry. ``None`` means
        "share the parent solomon memory" (default).
        """
        if v is None:
            return v
        if not v:
            raise ValueError("memory_namespace must be a non-empty string or None")
        if not _KEBAB_CASE_RE.match(v):
            raise ValueError(
                f"memory_namespace must be kebab-case (lowercase letters, "
                f"digits, hyphens), got {v!r}"
            )
        return v

    @field_validator("model")
    @classmethod
    def _validate_model(cls, v: str) -> str:
        # Allow empty — parse_agent_md substitutes settings.subagent_default_model.
        if v == "":
            return v
        if get_model(v) is None:
            raise ValueError(
                f"unknown model {v!r} — must be one of "
                f"{[e['id'] for e in __import__('harness.server.llm.models', fromlist=['MODELS']).MODELS]}"
            )
        return v

    @field_validator("tools")
    @classmethod
    def _validate_tools(cls, v: list[str]) -> list[str]:
        if any(not t or not isinstance(t, str) for t in v):
            raise ValueError("tools must be a list of non-empty strings")
        # Dedupe while preserving order.
        seen: set[str] = set()
        deduped: list[str] = []
        for t in v:
            if t not in seen:
                seen.add(t)
                deduped.append(t)
        return deduped

    @field_validator("permissions")
    @classmethod
    def _enforce_read_only_deny_list(cls, v: str, info: Any) -> str:
        # `read-only` strips write tools regardless of `tools` list — enforce
        # consistency at the schema level. `full` allows everything; `scoped-write`
        # restricts to `allowed_paths` (enforced at runtime in Step 4).
        tools = info.data.get("tools") or []
        if v == "read-only":
            write_tools = {"write_file", "edit_file"}
            overlap = write_tools & set(tools)
            if overlap:
                raise ValueError(
                    f"permissions=read-only conflicts with tools={sorted(overlap)}; "
                    f"either remove the write tools or switch to permissions=scoped-write/full"
                )
        return v


# === YAML-ish parser ===

def _parse_value(raw: str) -> Any:
    """Parse a single YAML-ish value (scalar or inline list).

    Supported shapes:
        ``42``               → 42 (int)
        ``3.14``             → 3.14 (float)
        ``true`` / ``false`` → bool
        ``"quoted string"``  → str (quotes stripped)
        ``[a, b, c]``        → ["a", "b", "c"] (inline list)
        ``bare``             → "bare"
    """
    s = raw.strip()
    if not s:
        return ""
    # Quoted string
    if (s.startswith('"') and s.endswith('"')) or (
        s.startswith("'") and s.endswith("'")
    ):
        return s[1:-1]
    # Inline list
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_parse_value(part) for part in _split_list(inner)]
    # Booleans
    if s in ("true", "True", "yes"):
        return True
    if s in ("false", "False", "no"):
        return False
    # Numbers
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s  # bare string


def _split_list(s: str) -> list[str]:
    """Split an inline list body on commas, respecting quoted strings."""
    parts: list[str] = []
    cur = []
    in_quote: str | None = None
    for ch in s:
        if in_quote:
            cur.append(ch)
            if ch == in_quote:
                in_quote = None
        elif ch in ('"', "'"):
            in_quote = ch
            cur.append(ch)
        elif ch == ",":
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur).strip())
    return [p for p in parts if p]


def _parse_frontmatter_block(block: str) -> dict[str, Any]:
    """Parse a ``key: value`` block into a dict. Strict: unknown fields raise."""
    out: dict[str, Any] = {}
    for lineno, raw in enumerate(block.splitlines(), start=1):
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue  # blank or comment
        # Pre-check for nested keys: anything before the ``:`` that contains
        # a dot is a nesting marker, which we don't support. Catching this
        # explicitly gives a clearer error than the generic "malformed line".
        if ":" in line and not line.lstrip().startswith("#"):
            key_part = line.split(":", 1)[0].strip()
            if "." in key_part:
                raise FrontmatterParseError(
                    f"frontmatter line {lineno}: nested keys are not supported, got {key_part!r}"
                )
        m = _KV_RE.match(line)
        if not m:
            raise FrontmatterParseError(
                f"malformed frontmatter line {lineno}: {line!r} (expected 'key: value')"
            )
        key, raw_value = m.group(1), m.group(2)
        if key in out:
            raise FrontmatterParseError(
                f"frontmatter line {lineno}: duplicate key {key!r}"
            )
        out[key] = _parse_value(raw_value)
    return out


# === Public API ===

#: Known frontmatter fields. Anything else raises ``FrontmatterParseError``.
_KNOWN_FIELDS: set[str] = {
    "name",
    "model",
    "tools",
    "permissions",
    "system_prompt",
    "max_iterations",
    "worktree_required",
    "allowed_paths",
}


def parse_agent_md(path: Path | str) -> AgentSpec:
    """Parse an agent ``.md`` file into an :class:`AgentSpec`.

    The file must start with a YAML frontmatter block delimited by
    ``---\\n...\\n---\\n``. The text after the closing ``---`` becomes
    the agent's ``system_prompt`` (stripped of leading/trailing whitespace).

    Raises:
        FrontmatterParseError: if the file is missing/has malformed frontmatter,
            contains unknown fields, or has nested keys.
        ValueError:             if the resulting spec fails Pydantic validation
            (unknown model, bad permissions-vs-tools combination, etc.).
        FileNotFoundError:      if ``path`` does not exist.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise FileNotFoundError(f"agent file not readable: {p}") from e

    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise FrontmatterParseError(
            f"agent file {p} is missing the required '---...---' frontmatter block"
        )

    front_raw, body = m.group(1), m.group(2)
    fields = _parse_frontmatter_block(front_raw)

    unknown = set(fields) - _KNOWN_FIELDS
    if unknown:
        raise FrontmatterParseError(
            f"agent file {p} has unknown frontmatter fields: {sorted(unknown)}. "
            f"Known: {sorted(_KNOWN_FIELDS)}"
        )

    # Build spec. Empty `model` → fall back to settings default.
    model_id = fields.get("model") or settings.subagent_default_model
    if not model_id:
        raise FrontmatterParseError(
            f"agent file {p}: 'model' is empty and settings.subagent_default_model is also empty"
        )

    spec_kwargs: dict[str, Any] = {
        "name": fields.get("name") or p.stem,
        "model": model_id,
        "tools": list(fields.get("tools") or []),
        "permissions": fields.get("permissions") or DEFAULT_PERMISSIONS,
        "system_prompt": body.strip(),
        "max_iterations": (
            int(fields["max_iterations"])
            if "max_iterations" in fields
            else 5
        ),
        "worktree_required": bool(fields.get("worktree_required", True)),
        "allowed_paths": list(fields.get("allowed_paths") or []),
    }
    try:
        return AgentSpec(**spec_kwargs)
    except Exception as e:
        # Pydantic ValidationError → re-raise as FrontmatterParseError so callers
        # get a single exception type for "this file is wrong".
        raise FrontmatterParseError(
            f"agent file {p} failed validation: {e}"
        ) from e
