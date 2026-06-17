"""Phase 4.4 v1.13.0: ``harness hooks`` CLI subcommand.

Public surface (all sync; the CLI is a fresh process):

  - :func:`_cmd_hooks_list` — list builtin + project hooks.
  - :func:`_cmd_hooks_show` — show details for one hook by id.
  - :func:`_cmd_hooks_status` — hot-reload status (no server probe).

Exits (per ``harness reload`` precedent):
  - 0 on success (including "no hooks found" / "all error files").
  - 1 if the requested hook_id is not found in ``hooks show``.
  - 2 on invalid arguments (e.g. unknown transport).

Trust boundary: this module imports only from ``harness.hooks.*``,
``harness.config``, and stdlib. It does NOT import ``harness.agents``
or ``harness.server`` (the test suite enforces this).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# Default priority (matches HookSpec default). Used when a parsed
# JSON spec omits ``priority``.
_DEFAULT_PRIORITY: int = 100


def _redact_header_value(value: str) -> str:
    """Redact likely credentials in a header value.

    Used by ``hooks show`` for the ``http`` transport's
    ``Authorization`` field. We keep the scheme prefix (e.g. ``Bearer``)
    and mask the secret.
    """
    if not value:
        return value
    if " " in value:
        scheme, _, rest = value.partition(" ")
        if rest:
            return f"{scheme} ***"
    return "***"


def _spec_to_row(
    spec: Any,
    *,
    source: str,
    file_name: str = "",
    error: str = "",
) -> dict[str, Any]:
    """Serialise a HookSpec (or an error row) to a JSON-safe dict.

    Always includes ``hook_id``, ``event``, ``transport``, ``enabled``,
    ``priority``, ``source``. For builtin specs, ``matcher`` is empty
    and ``callable_name`` is the function name. For project specs,
    ``file`` is the source filename.
    """
    row: dict[str, Any] = {
        "hook_id": spec.hook_id,
        "event": spec.event.value if hasattr(spec.event, "value") else str(spec.event),
        "transport": spec.transport,
        "enabled": bool(spec.enabled),
        "priority": int(getattr(spec, "priority", _DEFAULT_PRIORITY)),
        "matcher": getattr(spec, "matcher", "") or "",
        "source": source,
    }
    if file_name:
        row["file"] = file_name
    if error:
        row["error"] = error
        return row
    # Transport-specific summary.
    transport = spec.transport
    if transport == "builtin":
        cb = getattr(spec, "callable", None)
        row["callable_name"] = getattr(cb, "__name__", "<unknown>")
    elif transport == "subprocess":
        row["script_path"] = getattr(spec, "script_path", "")
    elif transport == "http":
        row["url"] = getattr(spec, "url", "")
        # Redact Authorization header.
        headers = dict(getattr(spec, "headers", {}) or {})
        if "Authorization" in headers:
            headers["Authorization"] = _redact_header_value(headers["Authorization"])
        row["headers"] = headers
    elif transport == "llm":
        row["model"] = getattr(spec, "model", "")
        # ``prompt`` may contain secrets — truncate to 120 chars to
        # avoid accidental leakage in --json output.
        prompt = getattr(spec, "prompt", "") or ""
        if len(prompt) > 120:
            prompt = prompt[:117] + "..."
        row["prompt"] = prompt
    row["timeout_ms"] = getattr(spec, "timeout_ms", None)
    return row


def _parse_project_hooks(project_root: Path) -> tuple[list[Any], list[dict[str, str]]]:
    """Parse ``.harness/hooks/*.json``.

    Returns ``(specs, errors)``. Errors are dicts ``{file, error}`` —
    the caller decides how to display them. Specs are ``HookSpec``.

    NOTE: We re-implement the JSON parse here (not call
    :func:`harness.hooks.hot_reload._parse_hook_file`) because
    that helper only reads the 3 required fields + the simple
    ones (matcher / timeout_ms / enabled / priority). It does
    NOT extract transport-specific fields (``script_path``,
    ``url``, ``headers``, ``model``, ``prompt``) which the CLI
    needs to display in ``hooks show``. We preserve the same
    3-field validation and the same error behaviour.
    """
    from harness.hooks.events import EventType
    from harness.hooks.registry import HookSpec

    hooks_dir = project_root / ".harness" / "hooks"
    specs: list[Any] = []
    errors: list[dict[str, str]] = []
    if not hooks_dir.is_dir():
        return specs, errors
    for path in sorted(hooks_dir.glob("*.json")):
        if path.name.startswith("."):
            continue
        try:
            text = path.read_text(encoding="utf-8")
            data = json.loads(text)
            if isinstance(data, dict):
                items = [data]
            elif isinstance(data, list):
                items = data
            else:
                raise ValueError(
                    f"hook file must be a JSON object or list, got {type(data).__name__}"
                )
            for i, item in enumerate(items):
                if not isinstance(item, dict):
                    raise ValueError(f"hook spec #{i} is not an object")
                for required in ("hook_id", "event", "transport"):
                    if required not in item:
                        raise ValueError(
                            f"hook spec #{i} missing required field {required!r}"
                        )
                try:
                    event = EventType(item["event"])
                except ValueError as exc:
                    raise ValueError(
                        f"hook spec #{i}: unknown event {item['event']!r}"
                    ) from exc
                transport = str(item["transport"])
                # Transport-specific fields (best-effort extraction).
                kwargs: dict[str, Any] = {
                    "matcher": str(item.get("matcher", "")),
                    "timeout_ms": int(item.get("timeout_ms", 3000)),
                    "enabled": bool(item.get("enabled", True)),
                    "priority": int(item.get("priority", _DEFAULT_PRIORITY)),
                }
                if transport == "subprocess":
                    kwargs["script_path"] = str(item.get("script_path", ""))
                elif transport == "http":
                    kwargs["url"] = str(item.get("url", ""))
                    headers_raw = item.get("headers") or {}
                    if not isinstance(headers_raw, dict):
                        raise ValueError(
                            f"hook spec #{i}: 'headers' must be an object"
                        )
                    kwargs["headers"] = {
                        str(k): str(v) for k, v in headers_raw.items()
                    }
                elif transport == "llm":
                    kwargs["model"] = str(item.get("model", ""))
                    kwargs["prompt"] = str(item.get("prompt", ""))
                specs.append(
                    HookSpec(
                        hook_id=str(item["hook_id"]),
                        event=event,
                        transport=transport,  # type: ignore[arg-type]
                        **kwargs,
                    )
                )
        except Exception as exc:  # noqa: BLE001 — show, don't crash
            errors.append({"file": path.name, "error": str(exc)})
    return specs, errors


def _collect_all(
    project_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Collect builtin + project hook rows for the CLI.

    Returns ``(rows, errors)`` where:
      - ``rows`` is a list of dicts (one per HookSpec).
      - ``errors`` is a list of dicts from malformed project files.
    """
    from harness.hooks.registry import get_registry

    registry = get_registry()
    rows: list[dict[str, Any]] = []
    # 1) Builtins.
    for spec in registry.all_specs():
        rows.append(_spec_to_row(spec, source="builtin"))
    # 2) Project overrides (parse .harness/hooks/*.json).
    proj_specs, proj_errors = _parse_project_hooks(project_root)
    # Tag each project spec with its source file. We re-walk the
    # directory to map spec back to file (the parser above doesn't
    # keep the file association).
    hooks_dir = project_root / ".harness" / "hooks"
    file_by_id: dict[str, str] = {}
    if hooks_dir.is_dir():
        for path in sorted(hooks_dir.glob("*.json")):
            if path.name.startswith("."):
                continue
            try:
                text = path.read_text(encoding="utf-8")
                data = json.loads(text)
            except Exception:  # noqa: BLE001
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict) and "hook_id" in item:
                    file_by_id[str(item["hook_id"])] = path.name
    for spec in proj_specs:
        rows.append(
            _spec_to_row(
                spec,
                source="project",
                file_name=file_by_id.get(spec.hook_id, ""),
            )
        )
    return rows, proj_errors


def _apply_filters(
    rows: list[dict[str, Any]],
    *,
    event: str | None,
    transport: str | None,
    enabled: str | None,
) -> list[dict[str, Any]]:
    """Apply ``--event`` / ``--transport`` / ``--enabled|--disabled`` filters."""
    out = list(rows)
    if event:
        events = {e.strip() for e in event.split(",") if e.strip()}
        if events:
            out = [r for r in out if r.get("event") in events]
    if transport:
        transports = {t.strip() for t in transport.split(",") if t.strip()}
        if transports:
            out = [r for r in out if r.get("transport") in transports]
    if enabled == "yes":
        out = [r for r in out if r.get("enabled") is True]
    elif enabled == "no":
        out = [r for r in out if r.get("enabled") is False]
    return out


def _print_table(rows: list[dict[str, Any]], *, title: str) -> None:
    """Print a compact table to stdout (no external deps)."""
    print(title)
    if not rows:
        print("  (no hooks)")
        return
    cols = [
        ("hook_id", 32),
        ("event", 18),
        ("transport", 11),
        ("enabled", 7),
        ("priority", 8),
        ("source", 8),
    ]
    header = "  " + "  ".join(f"{name:<{w}}" for name, w in cols)
    print(header)
    print("  " + "-" * (sum(w for _, w in cols) + 2 * (len(cols) - 1)))
    for r in rows:
        line_parts: list[str] = []
        for name, w in cols:
            v = r.get(name, "")
            s = str(v)
            if len(s) > w:
                s = s[: w - 1] + "…"
            line_parts.append(f"{s:<{w}}")
        print("  " + "  ".join(line_parts))


def _cmd_hooks_list(args: argparse.Namespace) -> int:
    """``harness hooks list`` — list all registered hooks."""
    project_root = Path(args.project_root).resolve() if args.project_root else Path.cwd()
    if not project_root.is_dir():
        print(
            f"[harness] hooks list: project_root {project_root} is not a directory",
            file=sys.stderr,
        )
        return 2

    rows, errors = _collect_all(project_root)
    rows = _apply_filters(
        rows,
        event=getattr(args, "event", None),
        transport=getattr(args, "transport", None),
        enabled=getattr(args, "enabled_flag", None),
    )

    if args.json:
        # Wrap for forward-compat (allows adding fields without
        # breaking parsers).
        payload = {
            "hooks": rows,
            "count": len(rows),
            "errors": errors,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_table(rows, title=f"Registered hooks (project_root: {project_root}):")
        if errors:
            print()
            print("[harness] project files with errors:")
            for err in errors:
                print(f"  ERROR {err['file']}: {err['error']}", file=sys.stderr)
    return 0


def _cmd_hooks_show(args: argparse.Namespace) -> int:
    """``harness hooks show <hook_id>`` — full spec for one hook."""
    hook_id = args.hook_id
    if not hook_id:
        print(
            "[harness] hooks show: hook_id is required",
            file=sys.stderr,
        )
        return 2

    project_root = Path(args.project_root).resolve() if args.project_root else Path.cwd()
    if not project_root.is_dir():
        print(
            f"[harness] hooks show: project_root {project_root} is not a directory",
            file=sys.stderr,
        )
        return 2

    rows, errors = _collect_all(project_root)
    matches = [r for r in rows if r.get("hook_id") == hook_id]
    if not matches:
        if args.json:
            print(
                json.dumps(
                    {"hook_id": hook_id, "found": False, "errors": errors},
                    ensure_ascii=False, indent=2,
                )
            )
        else:
            print(
                f"[harness] hooks show: hook_id {hook_id!r} not found",
                file=sys.stderr,
            )
        return 1

    row = matches[0]
    if args.json:
        print(json.dumps({"hook": row, "found": True}, ensure_ascii=False, indent=2))
    else:
        print(f"hook_id : {row.get('hook_id')}")
        print(f"event   : {row.get('event')}")
        print(f"transport: {row.get('transport')}")
        print(f"enabled : {row.get('enabled')}")
        print(f"priority: {row.get('priority')}")
        print(f"matcher : {row.get('matcher') or '(none)'}")
        print(f"source  : {row.get('source')}")
        if "file" in row:
            print(f"file    : {row.get('file')}")
        if "callable_name" in row:
            print(f"callable: {row.get('callable_name')}")
        if "script_path" in row:
            print(f"script  : {row.get('script_path')}")
        if "url" in row:
            print(f"url     : {row.get('url')}")
            for k, v in (row.get("headers") or {}).items():
                print(f"header  : {k}: {v}")
        if "model" in row:
            print(f"model   : {row.get('model')}")
            print(f"prompt  : {row.get('prompt')}")
        if "timeout_ms" in row and row.get("timeout_ms") is not None:
            print(f"timeout : {row.get('timeout_ms')}ms")
    return 0


def _cmd_hooks_status(args: argparse.Namespace) -> int:
    """``harness hooks status`` — local hot-reload summary.

    Reports whether ``.harness/hooks/`` exists, counts valid +
    invalid files, and the total HookSpec count. Does NOT probe
    the running server (that requires a /hooks/status endpoint
    we haven't shipped yet — deferred).
    """
    project_root = Path(args.project_root).resolve() if args.project_root else Path.cwd()
    if not project_root.is_dir():
        print(
            f"[harness] hooks status: project_root {project_root} is not a directory",
            file=sys.stderr,
        )
        return 2

    hooks_dir = project_root / ".harness" / "hooks"
    rows, errors = _collect_all(project_root)

    builtin_count = sum(1 for r in rows if r.get("source") == "builtin")
    project_count = sum(1 for r in rows if r.get("source") == "project")
    payload = {
        "project_root": str(project_root),
        "hooks_dir": str(hooks_dir),
        "hooks_dir_exists": hooks_dir.is_dir(),
        "total_specs": len(rows),
        "builtin_specs": builtin_count,
        "project_specs": project_count,
        "project_files_with_errors": len(errors),
        "errors": errors,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"project_root  : {payload['project_root']}")
        print(f"hooks_dir     : {payload['hooks_dir']}")
        print(f"exists        : {payload['hooks_dir_exists']}")
        print(f"total_specs   : {payload['total_specs']} "
              f"(builtin={builtin_count}, project={project_count})")
        if errors:
            print(f"files_errored : {len(errors)}")
            for err in errors:
                print(f"  ERROR {err['file']}: {err['error']}", file=sys.stderr)
        else:
            print("files_errored : 0")
    return 0


__all__ = [
    "_cmd_hooks_list",
    "_cmd_hooks_show",
    "_cmd_hooks_status",
]
