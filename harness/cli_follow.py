"""Phase 4.7 v1.17.0: ``--follow`` live tail for audit + metrics.

Public surface (sync; the CLI runs in a fresh process):

  - :func:`cmd_hooks_audit_follow`   — tail ``hooks-YYYY-MM-DD.ndjson``
    from EOF onward, printing each new entry as it is appended.
  - :func:`cmd_observability_metrics_follow` — poll
    :meth:`PrometheusMetrics.snapshot` at a configurable interval and
    print only the diffs (changed counters/gauges) since the last poll.

Both commands:

  - Use a 250 ms (audit) / N ms (metrics) polling loop — no
    ``watchdog`` dependency. Polling is portable (Windows has no
    inotify; watchdog is optional).
  - Trap ``KeyboardInterrupt`` (Ctrl+C / SIGINT) and exit 0 cleanly.
  - Emit a hint after 30 s of inactivity (audit only) so the operator
    knows the tail is still alive.
  - Respect ``--filter`` (regex on the whole raw line for audit, or
    on the metric name for metrics).
  - ``--json`` switches the output to NDJSON (one object per line).

Trust boundary: this module imports only from ``harness.hooks.audit``,
``harness.observability.metrics``, ``harness.config``, and stdlib. It
does NOT import ``harness.agents`` or ``harness.server`` (enforced by
the test suite via AST scan).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)


# Defaults (centralised so tests + help stay in sync).
_AUDIT_POLL_INTERVAL_S: float = 0.25
_AUDIT_INACTIVITY_HINT_S: float = 30.0
_METRICS_DEFAULT_INTERVAL_MS: int = 1000


# ===========================================================================
# Audit --follow
# ===========================================================================

def _audit_file_for(audit_dir: Path, when: datetime | None = None) -> Path:
    """Resolve today's (UTC) audit NDJSON path under ``audit_dir``.

    Mirrors ``harness.hooks.audit.HookAuditSink._path_for`` so the
    follow loop reads the same file the sink writes.
    """
    when = when or datetime.now(timezone.utc)
    return audit_dir / f"hooks-{when.strftime('%Y-%m-%d')}.ndjson"


def _rotate_if_needed(path: Path, max_bytes: int) -> None:
    """Rotate ``path`` to ``.1``, ``.2``, ... once it exceeds ``max_bytes``.

    Rotation is best-effort: if a rename fails (file locked, perms),
    we log and continue. ``max_bytes <= 0`` disables rotation.

    Algorithm: find the largest existing suffix ``.N``, then shift
    ``.N -> .(N+1), ..., .1 -> .2``, then ``path -> .1``. Uses
    ``path.with_name`` to avoid ``with_suffix`` quirks with
    multi-dot filenames.
    """
    if max_bytes <= 0:
        return
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size <= max_bytes:
        return
    parent = path.parent
    stem = path.name  # e.g. "hooks-2026-06-17.ndjson"
    # Find the highest existing rotation index.
    n = 0
    while True:
        candidate = parent / f"{stem}.{n + 1}"
        if not candidate.exists():
            break
        n += 1
    # Shift .(n) -> .(n+1), ..., .1 -> .2.
    for i in range(n, 0, -1):
        src = parent / f"{stem}.{i}"
        dst = parent / f"{stem}.{i + 1}"
        try:
            os.replace(src, dst)
        except OSError as exc:
            logger.warning("audit follow: rotate %s -> %s failed: %s", src, dst, exc)
    # Finally, base file -> .1.
    try:
        os.replace(path, parent / f"{stem}.1")
    except OSError as exc:
        logger.warning("audit follow: rotate %s -> .1 failed: %s", path, exc)


def _iter_new_lines(
    path: Path,
    *,
    start_at_end: bool = True,
    poll_interval_s: float = _AUDIT_POLL_INTERVAL_S,
    is_interrupted: Callable[[], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[str]:
    """Yield new lines appended to ``path`` after the start offset.

    - ``start_at_end=True`` → ``seek(0, SEEK_END)`` (skip existing
      contents). This is the default for ``--follow``.
    - ``start_at_end=False`` → read from byte 0 (used by tests that
      need to see pre-existing lines).
    - ``is_interrupted`` is polled each iteration; when it returns
      True the generator stops. The default ``None`` means rely on
      ``KeyboardInterrupt`` (raised by SIGINT in the main thread).
    - ``sleep`` is injected for tests (monotonic fake clock).
    """
    # Wait for the file to exist (audit dir may not be created yet).
    while not path.exists():
        if is_interrupted is not None and is_interrupted():
            return
        sleep(poll_interval_s)
    with path.open("r", encoding="utf-8") as f:
        if start_at_end:
            f.seek(0, os.SEEK_END)
        buf = ""
        while True:
            if is_interrupted is not None and is_interrupted():
                return
            chunk = f.read()
            if not chunk:
                sleep(poll_interval_s)
                continue
            buf += chunk
            # Split on newlines, keeping any trailing partial line in buf.
            *complete, buf = buf.split("\n")
            for line in complete:
                yield line
            # If the file was truncated (rotation), reset to start.
            try:
                cur_pos = f.tell()
                size = path.stat().st_size
            except OSError:
                size = cur_pos = 0
            if size < cur_pos:
                f.seek(0)
                buf = ""


def cmd_hooks_audit_follow(args: argparse.Namespace) -> int:
    """``harness hooks audit --follow`` — live tail of the audit log.

    Opens today's (UTC) audit NDJSON at EOF and prints each new line
    as it is appended. Supports ``--filter`` (regex on the raw line),
    ``--json`` (echo the raw NDJSON line), ``--max-bytes`` (rotate),
    and exits 0 on Ctrl+C.
    """
    project_root = (
        Path(args.project_root).resolve() if args.project_root else Path.cwd()
    )
    if not project_root.is_dir():
        print(
            f"[harness] hooks audit --follow: project_root {project_root} "
            f"is not a directory",
            file=sys.stderr,
        )
        return 2

    audit_dir = project_root / "data" / "audit"
    audit_file = _audit_file_for(audit_dir)

    filter_regex: re.Pattern[str] | None = None
    if getattr(args, "filter", None):
        try:
            filter_regex = re.compile(args.filter)
        except re.error as exc:
            print(
                f"[harness] hooks audit --follow: invalid --filter regex: {exc}",
                file=sys.stderr,
            )
            return 2

    max_bytes = int(getattr(args, "max_bytes", 0) or 0)
    json_output = bool(getattr(args, "json", False))

    # If the audit dir does not exist, print a hint and exit 0 —
    # matching the non-follow audit command's behaviour.
    if not audit_dir.is_dir():
        msg = (
            f"(no audit directory at {audit_dir}; "
            f"set settings.hooks_audit_log=True to enable)"
        )
        if json_output:
            print(json.dumps({"entries": [], "hint": msg}))
        else:
            print(msg, file=sys.stderr)
        return 0

    print(
        f"[harness] following {audit_file} (Ctrl+C to exit)",
        file=sys.stderr,
    )
    sys.stderr.flush()

    last_activity = time.monotonic()
    try:
        for line in _iter_new_lines(audit_file, start_at_end=True):
            # Rotation check (best-effort).
            _rotate_if_needed(audit_file, max_bytes)

            stripped = line.strip()
            if not stripped:
                continue
            if filter_regex is not None and not filter_regex.search(stripped):
                continue
            last_activity = time.monotonic()
            if json_output:
                # The line is already NDJSON; echo verbatim (guarantees
                # each output line is valid JSON).
                sys.stdout.write(stripped + "\n")
            else:
                _print_audit_line_pretty(stripped)
            sys.stdout.flush()

            # Inactivity hint (only when a long gap follows activity).
            # Cheap to compute each iteration.
            _maybe_inactivity_hint(last_activity)
    except KeyboardInterrupt:
        print("\n[harness] audit follow interrupted; exiting.", file=sys.stderr)
        return 0
    return 0


def _maybe_inactivity_hint(last_activity: float) -> None:
    """Emit a one-time hint after 30s of no new lines.

    Uses an attribute on the function object as a one-shot latch to
    avoid spamming the hint every iteration.
    """
    now = time.monotonic()
    if now - last_activity < _AUDIT_INACTIVITY_HINT_S:
        _maybe_inactivity_hint._emitted = False  # type: ignore[attr-defined]
        return
    if getattr(_maybe_inactivity_hint, "_emitted", False):
        return
    print(
        "[harness] no new audit entries; press Ctrl+C to exit",
        file=sys.stderr,
    )
    _maybe_inactivity_hint._emitted = True  # type: ignore[attr-defined]


def _print_audit_line_pretty(raw_line: str) -> None:
    """Pretty-print a single audit NDJSON line as a compact row.

    Columns: ``ts | event | session | decision | hook_id``. Falls
    back to printing the raw line if it is not valid JSON.
    """
    try:
        entry = json.loads(raw_line)
    except json.JSONDecodeError:
        sys.stdout.write(raw_line + "\n")
        return
    ts = str(entry.get("ts", ""))[:26]
    event = str(entry.get("event", ""))[:16]
    session = str(entry.get("session_id", ""))[:12]
    aggregate = entry.get("aggregate") if isinstance(entry.get("aggregate"), dict) else {}
    decision = str(aggregate.get("final_decision", ""))[:8]
    hook_id = str(aggregate.get("blocked_by") or "")[:24]
    sys.stdout.write(
        f"{ts}  {event:<16s}  {session:<12s}  {decision:<8s}  {hook_id}\n"
    )


# ===========================================================================
# Metrics --follow
# ===========================================================================

def _snapshot_diff(
    prev: dict[str, dict[tuple[tuple[str, str], ...], float]],
    curr: dict[str, dict[tuple[tuple[str, str], ...], float]],
    *,
    name_filter: re.Pattern[str] | None = None,
) -> list[tuple[str, tuple[tuple[str, str], ...], float, float]]:
    """Compute (metric_name, labels, prev_value, curr_value) tuples for
    every counter/gauge whose value changed OR is new.

    Metrics whose name does not match ``name_filter`` are skipped.
    A value that was absent in ``prev`` is reported with ``prev_value=0.0``
    for counters (first observation) — this surfaces new label sets.
    """
    out: list[tuple[str, tuple[tuple[str, str], ...], float, float]] = []
    for name, labelmap in curr.items():
        if name_filter is not None and not name_filter.search(name):
            continue
        prev_labelmap = prev.get(name, {})
        for labels, value in labelmap.items():
            old = prev_labelmap.get(labels)
            if old is None:
                # New label set — report as a change from 0.
                out.append((name, labels, 0.0, value))
            elif value != old:
                out.append((name, labels, old, value))
    return out


def cmd_observability_metrics_follow(args: argparse.Namespace) -> int:
    """``harness observability metrics --follow`` — live metrics diff.

    Polls :meth:`PrometheusMetrics.snapshot` every ``--interval-ms``
    milliseconds (default 1000) and prints only the counters/gauges
    whose value changed since the last poll.
    """
    from harness.observability import get_observability

    interval_ms = max(10, int(getattr(args, "interval_ms", _METRICS_DEFAULT_INTERVAL_MS)))
    interval_s = interval_ms / 1000.0
    json_output = bool(getattr(args, "json", False))

    name_filter: re.Pattern[str] | None = None
    if getattr(args, "filter", None):
        try:
            name_filter = re.compile(args.filter)
        except re.error as exc:
            print(
                f"[harness] observability metrics --follow: invalid "
                f"--filter regex: {exc}",
                file=sys.stderr,
            )
            return 2

    obs = get_observability()
    if not obs.metrics.enabled:
        msg = (
            "(prometheus_client not installed — metrics are no-op. "
            "Install with `pip install prometheus-client` to enable.)"
        )
        if json_output:
            print(json.dumps({"error": "prometheus_client not installed"}))
        else:
            print(msg, file=sys.stderr)
        return 0

    print(
        f"[harness] following metrics (interval={interval_ms}ms, Ctrl+C to exit)",
        file=sys.stderr,
    )
    sys.stderr.flush()

    prev = obs.metrics.snapshot()
    try:
        while True:
            time.sleep(interval_s)
            curr = obs.metrics.snapshot()
            diffs = _snapshot_diff(prev, curr, name_filter=name_filter)
            prev = curr
            for name, labels, old_v, new_v in diffs:
                label_str = ",".join(f"{k}={v}" for k, v in labels) if labels else ""
                if json_output:
                    sys.stdout.write(
                        json.dumps(
                            {
                                "metric": name,
                                "labels": label_str,
                                "prev": old_v,
                                "value": new_v,
                                "delta": new_v - old_v,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                else:
                    arrow = "+" if new_v > old_v else ""
                    sys.stdout.write(
                        f"{name:<40s}  {label_str:<40s}  "
                        f"{old_v:g} -> {new_v:g} ({arrow}{new_v - old_v:g})\n"
                    )
            if diffs:
                sys.stdout.flush()
    except KeyboardInterrupt:
        print("\n[harness] metrics follow interrupted; exiting.", file=sys.stderr)
        return 0
    return 0


__all__ = [
    "cmd_hooks_audit_follow",
    "cmd_observability_metrics_follow",
    "_iter_new_lines",
    "_rotate_if_needed",
    "_snapshot_diff",
    "_audit_file_for",
]
