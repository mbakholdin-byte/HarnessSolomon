"""Safety checks for tool execution (Шаг 4).

Two layers:
  1. Bash denylist (regex) — blocks catastrophic shell commands
  2. Path sandbox — keeps file tools inside project_root

These are deliberately conservative. False positives are preferred to
false negatives for security-critical checks.
"""
from __future__ import annotations

import re
from pathlib import Path

# === Bash deny patterns ===

#: Regex patterns (compiled lazily). A bash command is denied if ANY pattern matches.
#: Anchored loosely; we don't require word boundaries because the commands are
#: distinctive enough ("rm -rf /", "git push --force", etc.) and we want to catch
#: variants like "rm  -rf  /tmp" or "git  reset  --hard".
BASH_DENY_PATTERNS: list[str] = [
    r"rm\s+-rf\s+/",       # rm -rf /, rm -rf /something
    r"del\s+/s",            # Windows: del /s
    r"format\s+",           # format C:, format volume
    r"git\s+push\s+--force",  # force-push
    r"git\s+reset\s+--hard",  # destructive reset
]

# Pre-compile once at import time.
_COMPILED_DENY: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p) for p in BASH_DENY_PATTERNS
)


def is_bash_denied(command: str) -> str | None:
    """Return the matched pattern if command is denied, else None.

    Used by the runtime to short-circuit execution before spawning a process.
    """
    for pat in _COMPILED_DENY:
        if pat.search(command):
            return pat.pattern
    return None


# === Path sandbox ===

def is_safe_path(path: Path, project_root: Path) -> bool:
    """Return True if ``path`` resolves to a location under ``project_root``.

    Allows paths equal to project_root and any descendant. Rejects paths
    that escape via ``..`` or are absolute and outside the root.
    """
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        # Unresolvable path (e.g. invalid drive letter on Windows) → unsafe.
        return False
    try:
        root = project_root.resolve(strict=False)
    except OSError:
        return False

    # Both are resolved, so we can do a lexical descendant check via Path.is_relative_to
    # (Python 3.9+). This is correct on Windows for cross-drive rejections.
    try:
        return resolved.is_relative_to(root)
    except (ValueError, OSError):
        return False


def resolve_safe_path(raw: str | Path, project_root: Path) -> Path | None:
    """Resolve a user-supplied path under project_root.

    Returns the resolved absolute Path if safe, or None if it escapes the root.
    The caller decides what to do with None (typically return an error result).
    """
    raw_path = Path(raw)
    # If the user passed an absolute path, take it as-is. Otherwise, anchor
    # relative paths under project_root.
    if not raw_path.is_absolute():
        candidate = project_root / raw_path
    else:
        candidate = raw_path
    if is_safe_path(candidate, project_root):
        return candidate.resolve(strict=False)
    return None
