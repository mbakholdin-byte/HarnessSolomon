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
from datetime import datetime, timezone
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


def _cmd_hooks_dispatch(args: argparse.Namespace) -> int:
    """Phase 4.5 v1.15.0: ``harness hooks dispatch <event>``.

    Fires a single hook event against the global registry and prints
    the aggregate decision (``allow`` / ``block`` / ``modify``).
    Useful for shell-based testing of hook configurations without
    spinning up the FastAPI server.

    Exit codes:
        0 — event fired (regardless of decision).
        1 — internal error during dispatch.
        2 — invalid arguments (unknown event name, malformed --payload).

    The ``--project-root`` flag is accepted for parity with the
    other ``hooks`` subcommands; it is used to load
    ``.harness/hooks/*.json`` overrides into the global registry
    before firing. When omitted, only the built-in hooks fire.
    """
    import asyncio

    from harness.hooks.events import EventType

    # Validate the event name against the EventType enum.
    event_name = args.event
    valid_event_values = {e.value for e in EventType}
    if event_name not in valid_event_values:
        print(
            f"[harness] hooks dispatch: unknown event {event_name!r}. "
            f"Valid events: {', '.join(sorted(valid_event_values))}",
            file=sys.stderr,
        )
        return 2

    # Parse the payload (default empty dict).
    payload: dict[str, Any] = {}
    if args.payload:
        try:
            parsed = json.loads(args.payload)
        except json.JSONDecodeError as exc:
            print(
                f"[harness] hooks dispatch: --payload is not valid JSON: {exc}",
                file=sys.stderr,
            )
            return 2
        if not isinstance(parsed, dict):
            print(
                "[harness] hooks dispatch: --payload must be a JSON object, "
                f"got {type(parsed).__name__}",
                file=sys.stderr,
            )
            return 2
        payload = parsed

    # Load project overrides into the global registry before firing
    # so the dispatch observes the same hooks production code does.
    project_root = Path(args.project_root).resolve() if args.project_root else Path.cwd()
    if project_root.is_dir():
        specs, parse_errors = _parse_project_hooks(project_root)
        for spec in specs:
            # Register each project spec. We import the registry
            # inside the function to keep the module's import-time
            # surface minimal.
            from harness.hooks.registry import get_registry
            registry = get_registry()
            try:
                asyncio.run(registry.register(spec))
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[harness] hooks dispatch: failed to register "
                    f"project hook {spec.hook_id!r}: {exc}",
                    file=sys.stderr,
                )
        for err in parse_errors:
            print(
                f"  ERROR {err['file']}: {err['error']}",
                file=sys.stderr,
            )

    from harness.hooks.runner import safe_fire

    decision = asyncio.run(
        safe_fire(
            event_name,
            session_id=args.session or "",
            agent_id=args.agent or "",
            payload=payload,
        )
    )

    if args.json:
        print(
            json.dumps(
                {
                    "event": event_name,
                    "decision": decision,
                    "session_id": args.session or "",
                    "agent_id": args.agent or "",
                    "payload": payload,
                },
                ensure_ascii=False, indent=2,
            )
        )
    else:
        print(f"event    : {event_name}")
        print(f"decision : {decision}")
        if args.session:
            print(f"session  : {args.session}")
        if args.agent:
            print(f"agent    : {args.agent}")
        if payload:
            preview = json.dumps(payload, ensure_ascii=False)
            if len(preview) > 200:
                preview = preview[:197] + "..."
            print(f"payload  : {preview}")
    return 0


# === Phase 4.6 v1.16.0: ``harness hooks audit`` ============================
#
# Reads the NDJSON audit log written by ``harness.hooks.audit.HookAuditSink``.
# We do NOT reuse ``HookAuditSink.tail()`` here because:
#   1) ``tail()`` only reads today's file; the CLI should let the
#      operator specify which date's file to read (future flag).
#   2) ``tail()`` does not apply filters; we want one-pass filter+tail.
#   3) The CLI is read-only and a single-function parser is easy to
#      test without instantiating a sink.
#
# The on-disk format (one JSON object per line) is documented in
# ``harness/hooks/audit.py::HookAuditSink.record`` and in
# ``docs/hooks.md`` §8. We re-derive the schema here defensively
# rather than importing the sink's private ``_path_for`` helper.

_AUDIT_DIR_NAME = "data/audit"


def _audit_dir_for(project_root: Path) -> Path:
    """Resolve ``<project_root>/data/audit``.

    Matches the path used by ``HookAuditSink`` (which receives
    ``audit_dir`` directly; production wiring passes
    ``project_root / "data/audit"``). Kept here so the CLI does
    not import from harness.hooks.audit (which would be safe but
    would create an unnecessary module-level dependency).
    """
    return project_root / _AUDIT_DIR_NAME


def _audit_file_for(audit_dir: Path, when: datetime | None = None) -> Path:
    """Return the audit file path for a given day (UTC).

    Mirrors ``HookAuditSink._path_for`` but reads from the CLI
    side. Defaults to today's UTC date so the CLI shows the
    current day by default (matching the sink's rotation policy).
    """
    when = when or datetime.now(timezone.utc)
    return audit_dir / f"hooks-{when.strftime('%Y-%m-%d')}.ndjson"


def read_audit_log(
    path: Path,
    *,
    tail: int = 50,
    event: str | None = None,
    decision: str | None = None,
    session: str | None = None,
    since: str | None = None,
    filter_pattern: str | None = None,
) -> list[dict[str, Any]]:
    """Parse an NDJSON audit log and apply filters.

    Args:
        path: File to read (e.g. ``data/audit/hooks-2026-06-17.ndjson``).
            If the file does not exist, returns ``[]``.
        tail: Maximum number of entries to return. The last ``tail``
            entries (by file order) are returned after filtering.
            Set to ``0`` (or a very large number) to return all.
        event: If set, keep only entries whose ``event`` equals
            this value (case-sensitive, exact match).
        decision: If set (one of ``allow``/``block``/``modify``),
            keep only entries whose
            ``aggregate.final_decision`` matches.
        session: If set, keep only entries whose ``session_id``
            matches.
        since: ISO-8601 timestamp; keep only entries whose ``ts``
            is ``>= since``. Parsed loosely (a trailing ``Z`` is
            accepted; naive datetimes are treated as UTC).
        filter_pattern: Phase 4.7 v1.17.0. If set, a regex applied
            via ``re.search`` to the JSON-serialised entry (the
            same ``json.dumps`` representation used on disk). The
            regex runs AFTER the structured filters above (AND
            semantics). The caller is responsible for validating
            the regex — an invalid pattern raises ``re.error``
            here (which the CLI maps to exit 1 with a clear
            message).

    Returns:
        A list of dicts in file order (oldest first). Each dict is
        the raw JSON object from the file (no reshaping). Lines
        that fail to parse as JSON are skipped silently — the
        audit log is append-only and a partial line should not
        abort the read.
    """
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []

    # Compile the regex filter once (if provided). An invalid
    # pattern propagates as ``re.error`` — the CLI handler
    # catches it and exits 1.
    filter_regex: re.Pattern[str] | None = None
    if filter_pattern:
        filter_regex = re.compile(filter_pattern)

    # Parse the ``since`` filter once (loose ISO-8601).
    since_dt: datetime | None = None
    if since:
        candidate = since.strip()
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        try:
            since_dt = datetime.fromisoformat(candidate)
        except ValueError:
            # If the operator passed garbage, treat it as no filter
            # rather than crashing — the CLI prints a warning.
            since_dt = None
        else:
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=timezone.utc)

    out: list[dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            # Skip malformed lines (append-only log; partial writes
            # are possible if the process was killed mid-line).
            continue
        if not isinstance(entry, dict):
            continue

        if event is not None and entry.get("event") != event:
            continue
        if session is not None and entry.get("session_id") != session:
            continue

        aggregate = entry.get("aggregate")
        if isinstance(aggregate, dict):
            entry_decision = aggregate.get("final_decision")
        else:
            entry_decision = None
        if decision is not None and entry_decision != decision:
            continue

        if since_dt is not None:
            ts_raw = entry.get("ts")
            if isinstance(ts_raw, str):
                ts_candidate = ts_raw.rstrip("Z")
                try:
                    entry_dt = datetime.fromisoformat(ts_candidate)
                except ValueError:
                    # Unparseable timestamp — drop the entry rather
                    # than guessing.
                    continue
                else:
                    if entry_dt.tzinfo is None:
                        entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                    if entry_dt < since_dt:
                        continue
            else:
                # No usable timestamp — drop.
                continue

        # Phase 4.7 v1.17.0: regex filter. ``re.search`` on the
        # JSON-serialised entry (compact, no whitespace changes
        # between runs — ``json.dumps`` is deterministic for our
        # schema). The regex sees the full entry including the
        # ``aggregate`` payload, so operators can match on hook
        # ids, decisions, tool names, etc.
        if filter_regex is not None:
            serialised = json.dumps(entry, ensure_ascii=False, sort_keys=True)
            if not filter_regex.search(serialised):
                continue

        out.append(entry)

    if tail and tail > 0:
        return out[-tail:]
    return out


def _print_audit_table(entries: list[dict[str, Any]], *, source: str) -> None:
    """Pretty-print audit entries as a fixed-width table.

    Columns: ``timestamp | event | session | hook_id | decision | duration_ms``.
    ``hook_id`` comes from the aggregate's ``blocked_by`` (if set)
    or the first decision's ``hook_id``. ``duration_ms`` is the
    sum of all per-hook durations (best-effort proxy for total
    dispatch cost).
    """
    print(f"Hook audit log ({source}):")
    if not entries:
        print("  (no entries)")
        return

    cols = [
        ("timestamp", 26),
        ("event", 16),
        ("session", 12),
        ("hook_id", 24),
        ("decision", 8),
        ("duration_ms", 11),
    ]
    header = "  " + "  ".join(f"{name:<{w}}" for name, w in cols)
    print(header)
    print("  " + "-" * (sum(w for _, w in cols) + 2 * (len(cols) - 1)))

    for e in entries:
        ts = str(e.get("ts", ""))[:26]
        event = str(e.get("event", ""))[:16]
        session = str(e.get("session_id", ""))[:12]
        aggregate = e.get("aggregate") if isinstance(e.get("aggregate"), dict) else {}
        decision = str(aggregate.get("final_decision", ""))[:8]
        # hook_id: prefer blocked_by, else first decision's hook_id.
        hook_id = str(aggregate.get("blocked_by") or "")
        if not hook_id:
            decisions = aggregate.get("decisions")
            if isinstance(decisions, list) and decisions:
                first = decisions[0]
                if isinstance(first, dict):
                    hook_id = str(first.get("hook_id", ""))
        hook_id = hook_id[:24]
        # duration_ms: sum of per-hook durations.
        duration = 0.0
        decisions = aggregate.get("decisions")
        if isinstance(decisions, list):
            for d in decisions:
                if isinstance(d, dict):
                    try:
                        duration += float(d.get("duration_ms", 0.0) or 0.0)
                    except (TypeError, ValueError):
                        pass
        duration_s = f"{duration:.1f}"[:11]

        print(
            "  "
            + "  ".join(
                f"{v:<{w}}"
                for v, (_, w) in zip(
                    (ts, event, session, hook_id, decision, duration_s), cols,
                )
            )
        )


def _cmd_hooks_audit(args: argparse.Namespace) -> int:
    """Phase 4.6 v1.16.0: ``harness hooks audit`` — read the NDJSON audit log.

    Reads ``<project_root>/data/audit/hooks-YYYY-MM-DD.ndjson``
    (today's UTC file by default) and prints the last N entries
    after applying the requested filters.

    Exit codes:
        0 — success (including "no audit log" / "no entries").
        1 — invalid ``--filter`` regex (Phase 4.7 v1.17.0).
        2 — invalid arguments (e.g. unknown --decision value).

    If the audit directory or today's file does not exist, the
    command prints ``(no audit log)`` and exits 0 — this is the
    expected state when ``settings.hooks_audit_log`` is False.
    """
    # Validate --decision against the canonical Decision literal.
    decision = getattr(args, "decision", None)
    if decision is not None and decision not in ("allow", "block", "modify"):
        print(
            f"[harness] hooks audit: --decision must be one of "
            f"allow|block|modify, got {decision!r}",
            file=sys.stderr,
        )
        return 2

    # Phase 4.7 v1.17.0: validate --filter regex BEFORE touching
    # the audit file. An invalid pattern exits 1 with a clear
    # message; this distinguishes "bad CLI input" from "file
    # read error" (which we also map to 1, but via a different
    # path).
    filter_pattern = getattr(args, "filter", None)
    if filter_pattern:
        try:
            re.compile(filter_pattern)
        except re.error as exc:
            print(
                f"[harness] hooks audit: invalid --filter regex: {exc}",
                file=sys.stderr,
            )
            return 1

    project_root = Path(args.project_root).resolve() if args.project_root else Path.cwd()
    if not project_root.is_dir():
        print(
            f"[harness] hooks audit: project_root {project_root} is not a directory",
            file=sys.stderr,
        )
        return 2

    audit_dir = _audit_dir_for(project_root)
    audit_file = _audit_file_for(audit_dir)

    if not audit_file.exists():
        # No audit directory or today's file missing. This is the
        # common case (audit is opt-in via settings.hooks_audit_log).
        if args.json:
            print(json.dumps({"entries": [], "count": 0, "file": str(audit_file)}))
        else:
            print("(no audit log)")
        return 0

    entries = read_audit_log(
        audit_file,
        tail=args.tail,
        event=getattr(args, "event", None),
        decision=decision,
        session=getattr(args, "session", None),
        since=getattr(args, "since", None),
        filter_pattern=filter_pattern,
    )

    if args.json:
        print(
            json.dumps(
                {
                    "entries": entries,
                    "count": len(entries),
                    "file": str(audit_file),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        _print_audit_table(entries, source=str(audit_file))
    return 0


__all__ = [
    "_cmd_hooks_list",
    "_cmd_hooks_show",
    "_cmd_hooks_status",
    "_cmd_hooks_dispatch",
    "_cmd_hooks_audit",
    "read_audit_log",
]
