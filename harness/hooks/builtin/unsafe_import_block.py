"""Phase 4.10 Task B: Builtin UnsafeImportBlockHook — block dangerous imports.

PreToolUse defence-in-depth for ``write_file`` / ``edit_file`` on ``*.py``
content. Scans the proposed file content for import / usage patterns that
are well-known security risks:

    * ``os.system(...)``         — shell escape, use ``subprocess`` instead.
    * ``pickle.load`` / ``pickle.loads`` — arbitrary code execution via
      crafted byte streams. Use ``json`` or signed serialisation.
    * ``eval(...)`` / ``exec(...)`` — arbitrary code execution. Almost
      always replaceable with ``ast.literal_eval`` or a real parser.
    * ``yaml.load(...)`` *without* ``Loader=yaml.SafeLoader`` — RCE via
      YAML tags. Use ``yaml.safe_load`` or pass ``Loader=SafeLoader``.
    * ``subprocess.**(shell=True)`` — shell injection. Pass a list and
      leave ``shell`` at its default (``False``).
    * ``requests.post(...)`` *without* ``timeout=`` — unbounded hang on
      a slow / dead server. Always pass ``timeout=``.

The denylist is configurable via ``Settings.hooks_unsafe_imports_blocklist``
(a comma-separated string). The default matches the OWASP Python cheat
sheet. Each entry names a *module* or *module.method*; the hook scans
for occurrences of that token followed by a ``(`` (call) or ``=``
(keyword argument) — this avoids false positives on ``import os.system``
literally written in a docstring or comment about the blocklist itself.

Trust boundary: stdlib + ``re`` + ``logging`` + ``harness.config``
(read-only). No ``harness.agents`` / ``harness.server`` imports.
"""
from __future__ import annotations

import logging
import re

from harness.hooks.context import HookContext, HookDecision


logger = logging.getLogger("harness.hooks.builtin.unsafe_import_block")


# Default blocklist (used when Settings is unreadable, e.g. in a minimal
# test environment without pydantic-settings). Must mirror the default
# value of ``Settings.hooks_unsafe_imports_blocklist`` so behaviour is
# identical whether the hook reads the settings object or falls back.
_DEFAULT_BLOCKLIST: tuple[str, ...] = (
    "os.system",
    "subprocess",
    "eval",
    "exec",
    "pickle",
    "yaml.load",
    "requests.post",
)


def _get_blocklist() -> tuple[str, ...]:
    """Return the configured unsafe-imports blocklist.

    Lazy import of ``harness.config.settings`` keeps the module importable
    in environments where pydantic-settings is absent (tests). On any
    error we fall back to ``_DEFAULT_BLOCKLIST`` — fail-safe, never
    fail-open (the hook still blocks the OWASP defaults).
    """
    try:
        from harness.config import settings

        raw = settings.hooks_unsafe_imports_blocklist
    except Exception:  # noqa: BLE001 — config unavailable in minimal envs
        return _DEFAULT_BLOCKLIST
    if not raw:
        return ()
    items = tuple(s.strip() for s in raw.split(",") if s.strip())
    return items or _DEFAULT_BLOCKLIST


def _scan(content: str, blocklist: tuple[str, ...]) -> str | None:
    """Scan ``content`` for any unsafe pattern.

    Returns the human-readable reason for the first match, or ``None``
    if the content is clean. The logic per entry:

        * ``os.system``   → block on ``os.system(``.
        * ``eval``/``exec`` → block on bare ``eval(`` / ``exec(``.
          Word-boundary anchors prevent matching ``myeval(`` or
          ``re.exec``.
        * ``pickle``      → block on ``pickle.load`` / ``pickle.loads``.
          (Pickling is not inherently dangerous if you control the
          bytes — but in agent-generated code the safer path is to
          forbid it entirely.)
        * ``yaml.load``   → block on ``yaml.load(`` *unless* the call
          site passes ``SafeLoader``. ``yaml.safe_load`` is allowed.
        * ``subprocess``  → block ONLY when paired with ``shell=True``.
          A bare ``import subprocess`` or ``subprocess.run([...])``
          (shell defaults to False) is fine. This keeps the
          ``test_unsafe_import_allows_safe_subprocess`` case green.
        * ``requests.post`` → block on ``requests.post(`` *unless* the
          same call passes ``timeout=`` somewhere on the line.

    The function is deliberately conservative: a single regex per entry,
    no multi-line context, no AST. The goal is to catch the OWASP-class
    patterns cheaply at the PreToolUse sink, not to be a full taint
    analyser.
    """
    for entry in blocklist:
        if entry == "os.system":
            if re.search(r"os\.system\s*\(", content):
                return f"unsafe import: os.system() call"
        elif entry in ("eval", "exec"):
            # Word-boundary so ``myeval`` / ``re.exec`` don't match.
            if re.search(r"\b" + re.escape(entry) + r"\s*\(", content):
                return f"unsafe import: {entry}() call"
        elif entry == "pickle":
            # Block pickle.load(s) — the actual deserialisation calls.
            if re.search(r"pickle\.loads?\s*\(", content):
                return "unsafe import: pickle deserialisation (RCE risk)"
        elif entry == "yaml.load":
            # Allow yaml.safe_load, block yaml.load without SafeLoader.
            for m in re.finditer(r"yaml\.load\s*\(", content):
                tail = content[m.end(): m.end() + 200]
                if "SafeLoader" not in tail and "Loader=yaml.CSafeLoader" not in tail:
                    return "unsafe import: yaml.load() without SafeLoader"
        elif entry == "subprocess":
            # Only block when shell=True is present. A bare import or a
            # shell-less call is the recommended pattern.
            if re.search(r"subprocess\.\w+\([^)]*shell\s*=\s*True", content, re.DOTALL):
                return "unsafe import: subprocess with shell=True (injection risk)"
        elif entry == "requests.post":
            for m in re.finditer(r"requests\.post\s*\(", content):
                tail = content[m.end(): m.end() + 200]
                # Find the matching close paren of this call.
                depth = 1
                end = 0
                for i, ch in enumerate(tail):
                    if ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1
                        if depth == 0:
                            end = i
                            break
                call_text = tail[:end]
                if "timeout" not in call_text:
                    return "unsafe import: requests.post() without timeout"
        else:
            # Generic fallback: literal match of the entry as a call.
            if entry in content:
                return f"unsafe import: {entry}"
    return None


def _content_from_arguments(arguments: object) -> str:
    """Extract the proposed file content from tool arguments.

    Looks for ``content`` (write_file / edit_file convention) and falls
    back to flattening all argument values. Returns "" if nothing useful.
    """
    if isinstance(arguments, dict):
        # write_file / edit_file use ``content``; some tools use ``new_str``.
        for key in ("content", "new_str", "text"):
            v = arguments.get(key)
            if isinstance(v, str) and v:
                return v
        # Fall through to flattening.
    if isinstance(arguments, str):
        return arguments
    if isinstance(arguments, (list, tuple)):
        return " ".join(_content_from_arguments(v) for v in arguments)
    if isinstance(arguments, dict):
        return " ".join(_content_from_arguments(v) for v in arguments.values())
    return str(arguments) if arguments else ""


async def unsafe_import_block_hook(context: HookContext) -> HookDecision:
    """Block PreToolUse if the proposed content contains a dangerous import.

    Only PreToolUse events are inspected. If the payload has no writable
    content (e.g. a ``read_file`` call), the hook allows — there is
    nothing to scan.
    """
    if context.event != "PreToolUse":
        return HookDecision(
            decision="allow", hook_id="user.builtin.unsafe_import_block"
        )
    arguments = context.payload.get("arguments", {})
    content = _content_from_arguments(arguments)
    if not content:
        return HookDecision(
            decision="allow", hook_id="user.builtin.unsafe_import_block"
        )
    blocklist = _get_blocklist()
    if not blocklist:
        return HookDecision(
            decision="allow", hook_id="user.builtin.unsafe_import_block"
        )
    reason = _scan(content, blocklist)
    if reason is not None:
        logger.warning("UnsafeImportBlock: %s", reason)
        return HookDecision(
            decision="block",
            hook_id="user.builtin.unsafe_import_block",
            output={"reason": reason},
        )
    return HookDecision(
        decision="allow", hook_id="user.builtin.unsafe_import_block"
    )


__all__ = ["unsafe_import_block_hook"]
