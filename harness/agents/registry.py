"""Sub-agent registry — load built-ins + project overrides (Phase 2.0, Step 2).

Resolution order for a sub-agent named ``foo``:

    1. ``harness/agents/builtin/foo.md``     — built-in shipped with the package
    2. ``<project_root>/.harness/agents/foo.md``  — user-editable override

If neither exists, :func:`load_agent` raises :class:`FileNotFoundError`. If
both exist, the user override wins and the built-in is shadowed silently
(by design — see ``docs/subagents.md``).

To inspect what's installed, run::

    python -m harness agents list
"""
from __future__ import annotations

import logging
from importlib import resources
from pathlib import Path
from typing import Iterable

from harness.agents.spec import AgentSpec, FrontmatterParseError, parse_agent_md

logger = logging.getLogger(__name__)


# === Path helpers ===

#: Directory inside the package where the built-in ``.md`` files live.
BUILTIN_DIR_RESOURCE: str = "harness.agents.builtin"


def _builtin_file(name: str):
    """Return the ``Traversable`` for the built-in ``<name>.md``, or None.

    We use ``importlib.resources`` (Python 3.9+) for package-data access —
    it works in editable installs, wheels, and zip-imports. The returned
    object is a ``Traversable`` (not a ``Path``) and supports ``read_text``.

    Returns ``None`` if the file does not exist in the package data.
    """
    f = resources.files(BUILTIN_DIR_RESOURCE).joinpath(f"{name}.md")
    if not f.is_file():
        return None
    return f


def _project_override_path(project_root: Path, name: str) -> Path:
    """Path to the user override ``<name>.md`` under ``.harness/agents/``."""
    return project_root / ".harness" / "agents" / f"{name}.md"


def _read_builtin(name: str) -> AgentSpec | None:
    """Parse the built-in ``.md`` for ``name``, or return None if missing."""
    f = _builtin_file(name)
    if f is None or not f.is_file():
        return None
    # ``parse_agent_md`` accepts a Path, but Traversable is duck-compatible
    # (has ``read_text(encoding=, errors=)``). We pre-read the text to keep
    # the contract simple.
    from harness.agents.spec import FrontmatterParseError
    text = f.read_text(encoding="utf-8", errors="replace")
    try:
        return _parse_text(text, source=f"<builtin:{name}>")
    except FrontmatterParseError as e:
        # Re-raise with a clearer source label.
        raise FrontmatterParseError(
            f"built-in agent {name!r} is malformed: {e}"
        ) from e


def _read_override(project_root: Path, name: str) -> AgentSpec | None:
    """Parse the user-override ``.md`` for ``name``, or return None if missing."""
    p = _project_override_path(project_root, name)
    if not p.exists():
        return None
    return parse_agent_md(p)


def _parse_text(text: str, *, source: str) -> "AgentSpec":
    """Parse frontmatter text. ``source`` is used in error messages."""
    from harness.agents.spec import (
        _FRONTMATTER_RE,
        _parse_frontmatter_block,
        AgentSpec,
        DEFAULT_PERMISSIONS,
        FrontmatterParseError,
    )
    from harness.config import settings

    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise FrontmatterParseError(
            f"{source}: missing the required '---...---' frontmatter block"
        )
    front_raw, body = m.group(1), m.group(2)
    fields = _parse_frontmatter_block(front_raw)
    model_id = fields.get("model") or settings.subagent_default_model
    if not model_id:
        raise FrontmatterParseError(
            f"{source}: 'model' is empty and settings.subagent_default_model is also empty"
        )
    try:
        return AgentSpec(
            name=fields.get("name") or "",
            model=model_id,
            tools=list(fields.get("tools") or []),
            permissions=fields.get("permissions") or DEFAULT_PERMISSIONS,
            system_prompt=body.strip(),
            max_iterations=(
                int(fields["max_iterations"])
                if "max_iterations" in fields
                else 5
            ),
            worktree_required=bool(fields.get("worktree_required", True)),
            allowed_paths=list(fields.get("allowed_paths") or []),
        )
    except Exception as e:
        raise FrontmatterParseError(f"{source}: validation failed: {e}") from e


# === Public API ===

def load_agent(name: str, *, project_root: Path) -> AgentSpec:
    """Load a sub-agent by name, preferring the project override.

    Args:
        name: kebab-case sub-agent name (e.g. ``"explore"``).
        project_root: directory under which ``.harness/agents/`` lives.
            Usually :data:`harness.config.settings.project_root`.

    Returns:
        The resolved :class:`AgentSpec` (override if present, else built-in).

    Raises:
        FileNotFoundError: if neither built-in nor override exists.
        FrontmatterParseError: if the resolved file is malformed.
    """
    spec = _read_override(project_root, name)
    if spec is not None:
        return spec
    spec = _read_builtin(name)
    if spec is not None:
        return spec
    raise FileNotFoundError(
        f"no sub-agent named {name!r} (looked in built-ins and "
        f"{project_root / '.harness' / 'agents'})"
    )


def list_agents(*, project_root: Path) -> list[str]:
    """Sorted list of all available sub-agent names.

    Includes built-ins, project overrides, AND any extra ``.md`` files in the
    project override directory that do not shadow a built-in. The result is
    a union, not a deduplication of roles.

    Files that do not match the kebab-case naming convention (e.g. a
    ``README.md`` in the override directory) are ignored — they are
    documentation, not agent specs.
    """
    names: set[str] = set()

    # Built-ins: scan the package resource directory for ``.md`` files.
    for entry in resources.files(BUILTIN_DIR_RESOURCE).iterdir():
        n = entry.name
        if n.endswith(".md") and not n.startswith("."):
            stem = n[: -len(".md")]
            if _is_kebab_case(stem):
                names.add(stem)

    # Project overrides: scan the .harness/agents/ directory.
    override_dir = project_root / ".harness" / "agents"
    if override_dir.is_dir():
        for p in override_dir.iterdir():
            if p.suffix == ".md" and not p.name.startswith(".") and _is_kebab_case(p.stem):
                names.add(p.stem)

    return sorted(names)


def all_specs(*, project_root: Path) -> dict[str, AgentSpec]:
    """All sub-agent specs keyed by name, with override semantics applied.

    For each name in :func:`list_agents`, returns the resolved spec via
    :func:`load_agent` (so built-ins shadowed by overrides are NOT
    included under their built-in values).
    """
    out: dict[str, AgentSpec] = {}
    for name in list_agents(project_root=project_root):
        try:
            out[name] = load_agent(name, project_root=project_root)
        except (FileNotFoundError, FrontmatterParseError) as e:
            # A malformed override must not poison the whole registry —
            # log and skip. The malformed file is the user's problem to
            # fix; we do not silently substitute the built-in.
            logger.error("skipping sub-agent %r: %s", name, e)
    return out


def builtin_only() -> Iterable[str]:
    """Names of built-in sub-agents (no project lookup)."""
    for entry in resources.files(BUILTIN_DIR_RESOURCE).iterdir():
        n = entry.name
        if n.endswith(".md") and not n.startswith("."):
            stem = n[: -len(".md")]
            if _is_kebab_case(stem):
                yield stem


def has_override(name: str, *, project_root: Path) -> bool:
    """True iff a user override exists for ``name`` under the project root."""
    return _project_override_path(project_root, name).exists()


# === Kebab-case filter (re-used by list_agents / builtin_only) ===

_KEBAB_CASE_RE = __import__("re").compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")


def _is_kebab_case(name: str) -> bool:
    """True iff ``name`` is a valid kebab-case identifier (matches AgentSpec rule)."""
    return bool(_KEBAB_CASE_RE.match(name))
