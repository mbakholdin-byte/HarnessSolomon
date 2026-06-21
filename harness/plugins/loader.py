"""Phase 6.2A v1.27.0 — Plugin loader.

Discovers ``*.py`` files in a plugins directory and loads each via
``importlib.util.spec_from_file_location``. Each plugin module must
expose either:

1. A module-level ``register(registry: PluginRegistry) -> None``
   function (duck-typed), OR
2. A ``Plugin`` subclass instantiated at module level whose
   ``.register(registry)`` is invoked.

The loader calls whichever is present. A module with neither is logged
and skipped (does NOT abort the whole batch).

Trust boundary (CRITICAL):

    Plugin modules are ``exec_module``-d with a custom globals dict
    that contains only the registry-facing surface (``__name__``,
    ``__file__``, ``__builtins__``, and a ``registry`` reference if
    the plugin declares it). The plugins directory is scanned by path;
    no ``harness.agents.*`` or ``harness.server.*`` symbols leak in
    via the loader. An AST-level test
    (``tests/test_plugin_loader_v127.py``) additionally asserts that
    the shipped plugins do not import those subpackages.

Failures (ImportError, SyntaxError, missing ``register``, etc.) are
caught, logged via ``logging``, and the offending plugin is skipped —
the rest of the batch continues to load.
"""
from __future__ import annotations

import ast
import importlib.util
import logging
from pathlib import Path
from typing import Any

from harness.plugins import PluginInfo, PluginManifestV2, PluginRegistry, get_registry

log = logging.getLogger("harness.plugins.loader")

# Subpackage prefixes that plugins MUST NOT import (trust boundary).
# The AST pre-scan rejects a plugin file whose source statically imports
# any of these BEFORE we ever exec it.
_FORBIDDEN_IMPORT_PREFIXES: tuple[str, ...] = (
    "harness.agents",
    "harness.server",
)


def _ast_scan_forbidden_imports(source: str, path: Path) -> list[str]:
    """Return a list of forbidden import targets found in ``source``.

    Uses :mod:`ast` so we never actually execute the plugin while
    validating it. Detects ``import X`` and ``from X import ...``
    forms. Returns the offending dotted module names (empty list = ok).
    """
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        # A syntax error is a separate failure mode — surface it but
        # do NOT treat it as a forbidden import (the caller will catch
        # the ImportError when it tries to exec).
        log.warning("plugins: %s has SyntaxError: %s", path, exc)
        return []

    forbidden: list[str] = []
    for node in ast.walk(tree):
        targets: list[str] = []
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            targets.append(node.module)
        for tgt in targets:
            if any(
                tgt == prefix or tgt.startswith(prefix + ".")
                for prefix in _FORBIDDEN_IMPORT_PREFIXES
            ):
                forbidden.append(tgt)
    return forbidden


def _load_one(
    path: Path,
    registry: PluginRegistry,
    allowed: list[str] | None,
) -> PluginInfo | None:
    """Load a single plugin file. Return its info, or None on skip/failure.

    Args:
        path: ``*.py`` file to load.
        registry: The :class:`PluginRegistry` to register against.
        allowed: Whitelist of plugin names (by stem). ``None`` or empty
            list = all allowed. A plugin whose stem is not in the list
            is silently skipped.
    """
    stem = path.stem
    if allowed and stem not in allowed:
        log.info("plugins: %s skipped (not in whitelist)", stem)
        return None

    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("plugins: %s unreadable: %s", path, exc)
        return None

    forbidden = _ast_scan_forbidden_imports(source, path)
    if forbidden:
        log.warning(
            "plugins: %s rejected — imports forbidden module(s): %s",
            stem,
            ", ".join(forbidden),
        )
        return None

    # Build a restricted globals dict. We populate the minimum the
    # plugin needs: dunder bookkeeping, full builtins (plugins may use
    # logging, dataclasses, etc.), and a ``registry`` slot the plugin
    # can reference. Critically, NO harness.* symbols are injected
    # here — the plugin gets them only if it imports them (and the AST
    # scan above blocks ``harness.agents`` / ``harness.server``).
    module_name = f"harness_plugins.{stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        log.warning("plugins: %s — no import spec (skipping)", path)
        return None

    module = importlib.util.module_from_spec(spec)
    # Restrict globals: only builtins + dunders + registry reference.
    module.__dict__.update({
        "__builtins__": __builtins__,
        "registry": registry,
    })

    try:
        spec.loader.exec_module(module)  # type: ignore[union-attr]
    except Exception as exc:  # noqa: BLE001 — plugin code is untrusted
        log.warning(
            "plugins: %s failed to exec (%s: %s) — skipping",
            stem,
            type(exc).__name__,
            exc,
        )
        return None

    # Resolve the plugin's identity + register entrypoint.
    # Phase 7.4 WI-02: MANIFEST_V2 takes priority over v1 PLUGIN_NAME/PLUGIN_VERSION.
    manifest_v2_dict = getattr(module, "MANIFEST_V2", None)
    if manifest_v2_dict is not None and isinstance(manifest_v2_dict, dict):
        try:
            manifest = PluginManifestV2.from_dict(manifest_v2_dict)
            manifest.validate()
        except (TypeError, ValueError, KeyError) as exc:
            log.warning(
                "plugins: %s has invalid MANIFEST_V2 (%s: %s) — skipping",
                stem,
                type(exc).__name__,
                exc,
            )
            return None
        plugin_name = manifest.name
        plugin_version = manifest.version
        log.info(
            "plugins: %s loaded v2 manifest (min_harness=%s, permissions=%d)",
            stem,
            manifest.min_harness_version or "<none>",
            len(manifest.permissions),
        )
    else:
        # v1 fallback — PLUGIN_NAME / PLUGIN_VERSION module attributes.
        plugin_name = getattr(module, "PLUGIN_NAME", stem)
        plugin_version = getattr(module, "PLUGIN_VERSION", "0.0.0")

    # Snapshot registered surface BEFORE calling register so we can
    # diff afterwards to populate PluginInfo.hooks / tools / scopes.
    hooks_before = {k: list(v) for k, v in registry._hooks.items()}  # noqa: SLF001
    tools_before = set(registry._tools.keys())  # noqa: SLF001
    scopes_before = set(registry._scopes.keys())  # noqa: SLF001

    register_fn: Any = getattr(module, "register", None)
    if callable(register_fn):
        try:
            register_fn(registry)
        except Exception as exc:  # noqa: BLE001 — plugin code is untrusted
            log.warning(
                "plugins: %s register() raised (%s: %s) — skipping",
                stem,
                type(exc).__name__,
                exc,
            )
            return None
    else:
        log.warning(
            "plugins: %s has no register() function — skipping", stem,
        )
        return None

    # Diff to find what THIS plugin registered.
    new_hooks = [
        h
        for h, handlers in registry._hooks.items()  # noqa: SLF001
        if len(handlers) > len(hooks_before.get(h, []))
    ]
    new_tools = list(set(registry._tools.keys()) - tools_before)  # noqa: SLF001
    new_scopes = list(set(registry._scopes.keys()) - scopes_before)  # noqa: SLF001

    info = PluginInfo(
        name=plugin_name,
        version=plugin_version,
        source_path=str(path),
        hooks=new_hooks,
        tools=new_tools,
        scopes=new_scopes,
    )
    registry.register_plugin(info)
    log.info(
        "plugins: loaded %s v%s (hooks=%d tools=%d scopes=%d)",
        info.name,
        info.version,
        len(info.hooks),
        len(info.tools),
        len(info.scopes),
    )
    return info


def load_plugins_from_dir(
    plugins_dir: Path,
    registry: PluginRegistry | None = None,
    allowed: list[str] | None = None,
) -> list[PluginInfo]:
    """Discover and load all ``*.py`` plugins in ``plugins_dir``.

    Args:
        plugins_dir: Directory to scan. Non-existent / empty → ``[]``.
        registry: Target registry (defaults to the process singleton).
        allowed: Optional whitelist of plugin stems. ``None`` or empty
            list = all discovered plugins are allowed.

    Returns:
        List of :class:`PluginInfo` for successfully loaded plugins,
        in lexicographic filename order. Failed / skipped plugins are
        omitted from the list (a warning is logged).
    """
    if registry is None:
        registry = get_registry()

    plugins_dir = Path(plugins_dir)
    if not plugins_dir.is_dir():
        log.info("plugins: dir %s does not exist — skipping", plugins_dir)
        return []

    py_files = sorted(plugins_dir.glob("*.py"))
    # Exclude __init__.py, dunder files, and editor temp files.
    py_files = [
        p for p in py_files
        if not p.name.startswith("_") and not p.name.startswith(".")
    ]

    loaded: list[PluginInfo] = []
    for path in py_files:
        info = _load_one(path, registry, allowed)
        if info is not None:
            loaded.append(info)
    return loaded
