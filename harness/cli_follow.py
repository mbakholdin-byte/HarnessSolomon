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
from typing import Any, AsyncIterator, Callable, Iterator

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

    Phase 4.12 v1.22.0: when ``--batch-size``, ``--resume``, or
    ``--reset`` is set, the command switches to the :class:`Follower`
    implementation (async, batched, persistent state). Without those
    flags, the legacy :func:`_iter_new_lines` path is used (preserves
    backward compatibility with existing tests that monkeypatch
    ``_iter_new_lines``).
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

    # Phase 4.12 v1.22.0: Follower path (batched + state).
    batch_size = int(getattr(args, "batch_size", 0) or 0)
    resume = bool(getattr(args, "resume", False))
    reset = bool(getattr(args, "reset", False))
    use_follower = bool(batch_size or resume or reset)

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

    if use_follower:
        return _run_audit_follower(
            audit_file,
            batch_size=batch_size,
            filter_regex=filter_regex,
            json_output=json_output,
            resume=resume,
            reset=reset,
        )

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


def _run_audit_follower(
    audit_file: Path,
    *,
    batch_size: int,
    filter_regex: re.Pattern[str] | None,
    json_output: bool,
    resume: bool,
    reset: bool,
) -> int:
    """Phase 4.12 v1.22.0: drive the audit :class:`Follower`.

    Renders each batch by delegating to the same pretty-printer used
    by the legacy path (:func:`_print_audit_line_pretty`). State is
    saved to :func:`follow_state_path` under
    ``settings.cli_follow_state_dir`` so ``--resume`` picks it up.
    """
    from harness.config import settings

    if batch_size <= 0:
        batch_size = settings.cli_follow_default_batch_size

    state_path = follow_state_path("audit")
    follower = Follower(
        audit_file,
        batch_size=batch_size,
        filter_regex=filter_regex,
        state_file=state_path,
        kind="audit",
    )

    def _on_batch(batch: list[str]) -> None:
        for line in batch:
            if json_output:
                sys.stdout.write(line + "\n")
            else:
                _print_audit_line_pretty(line)
        sys.stdout.flush()

    return run_follow_async(
        follower,
        on_batch=_on_batch,
        resume=resume,
        reset=reset,
    )


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

    Phase 4.12 v1.22.0: ``--batch-size N`` buffers diffs into batches
    of N entries before flushing to stdout (reduces I/O for busy
    metric sources). ``--resume`` / ``--reset`` are accepted for
    CLI parity with ``hooks audit --follow`` but are no-ops here
    (in-memory counters are ephemeral — there is no file offset to
    persist; a warning is printed to stderr if either is set).
    """
    from harness.observability import get_observability

    interval_ms = max(10, int(getattr(args, "interval_ms", _METRICS_DEFAULT_INTERVAL_MS)))
    interval_s = interval_ms / 1000.0
    json_output = bool(getattr(args, "json", False))
    batch_size = int(getattr(args, "batch_size", 0) or 0)
    resume = bool(getattr(args, "resume", False))
    reset = bool(getattr(args, "reset", False))

    # In-memory counters have no persistent state — warn if the
    # operator asked for resume/reset.
    if resume or reset:
        print(
            "[harness] observability metrics --follow: --resume / --reset "
            "have no effect on in-memory counters (accepted for CLI parity "
            "with `hooks audit --follow` but no-ops here).",
            file=sys.stderr,
        )

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

    # Phase 4.12 v1.22.0: when --batch-size is set, mark that we're
    # using the batched path so the test suite can detect it.
    use_batching = batch_size > 0

    print(
        f"[harness] following metrics (interval={interval_ms}ms"
        + (f", batch_size={batch_size}" if use_batching else "")
        + ", Ctrl+C to exit)",
        file=sys.stderr,
    )
    sys.stderr.flush()

    def _format_diff(name: str, labels: tuple[tuple[str, str], ...], old_v: float, new_v: float) -> str:
        label_str = ",".join(f"{k}={v}" for k, v in labels) if labels else ""
        if json_output:
            return json.dumps(
                {
                    "metric": name,
                    "labels": label_str,
                    "prev": old_v,
                    "value": new_v,
                    "delta": new_v - old_v,
                },
                ensure_ascii=False,
            )
        arrow = "+" if new_v > old_v else ""
        return (
            f"{name:<40s}  {label_str:<40s}  "
            f"{old_v:g} -> {new_v:g} ({arrow}{new_v - old_v:g})"
        )

    pending_lines: list[str] = []

    def _flush() -> None:
        if pending_lines:
            sys.stdout.write("\n".join(pending_lines) + "\n")
            sys.stdout.flush()
            pending_lines.clear()

    prev = obs.metrics.snapshot()
    try:
        while True:
            time.sleep(interval_s)
            curr = obs.metrics.snapshot()
            diffs = _snapshot_diff(prev, curr, name_filter=name_filter)
            prev = curr
            for name, labels, old_v, new_v in diffs:
                line = _format_diff(name, labels, old_v, new_v)
                if use_batching:
                    pending_lines.append(line)
                    if len(pending_lines) >= batch_size:
                        _flush()
                else:
                    sys.stdout.write(line + "\n")
            if not use_batching and diffs:
                sys.stdout.flush()
            elif use_batching and diffs:
                # Flush partial batch after each poll so the operator
                # sees timely output even when the batch isn't full.
                _flush()
    except KeyboardInterrupt:
        print("\n[harness] metrics follow interrupted; exiting.", file=sys.stderr)
        return 0
    return 0


# ===========================================================================
# Phase 4.12 v1.22.0: Follower — persistent tail with rotation,
# batching, filtering, and resume-from-state.
# ===========================================================================

# Sentinel returned by ``Follower.run`` when no ``stop_predicate`` is
# supplied: the loop runs indefinitely until the caller breaks out or
# the source raises a terminal exception. Tests inject a bounded
# ``stop_predicate`` so the async generator terminates deterministically.
_FOLLOW_POLL_INTERVAL_S: float = 0.25
_FOLLOW_MISSING_FILE_RETRIES: int = 4  # ~1s at 250ms before giving up on a missing file
_FOLLOW_STATE_FILENAME_FMT: str = ".follow-state-{kind}.json"


class Follower:
    """Persistent tail with file rotation, batching, filtering.

    Phase 4.12 v1.22.0: a reusable async generator that yields batches
    of new lines appended to ``path``. Handles:

      - **File rotation** (inode change): if ``os.stat(path).st_ino``
        differs from the inode recorded at open time, the follower
        reopens the file from byte 0 (the new file is treated as a
        fresh rotation).
      - **Missing file**: if the path disappears, the follower waits
        ``poll_interval_s`` and retries, up to
        ``missing_file_retries`` consecutive failures before giving up.
      - **Batching**: lines are buffered until ``batch_size`` is
        reached OR the source pauses (no new data for one poll
        interval), then yielded as a ``list[str]``.
      - **Filtering**: ``filter_regex`` (compiled) skips lines that
        do not match (via ``re.search`` on the raw line).
      - **Persistent state**: when ``state_file`` is set, the
        follower writes ``{kind, last_offset, last_inode, started_at}``
        after each batch so a subsequent ``--resume`` run can continue.

    Usage:

        follower = Follower(
            path=audit_file,
            batch_size=10,
            filter_regex=re.compile("block") if args.filter else None,
            state_file=state_path,
            kind="audit",
        )
        async for batch in follower.run(resume=args.resume, reset=args.reset):
            for line in batch:
                print(line)

    The generator is **cooperative**: pass ``stop_predicate`` (a
    zero-arg callable returning ``True`` to stop) so tests can bound
    the loop without sending ``KeyboardInterrupt``.

    Trust boundary: stdlib only (asyncio, pathlib, re, os, json,
    time). Does NOT import ``harness.agents`` or ``harness.server``.
    """

    def __init__(
        self,
        path: Path,
        *,
        batch_size: int = 10,
        filter_regex: re.Pattern[str] | None = None,
        state_file: Path | None = None,
        kind: str = "audit",
        poll_interval_s: float = _FOLLOW_POLL_INTERVAL_S,
        missing_file_retries: int = _FOLLOW_MISSING_FILE_RETRIES,
    ) -> None:
        self.path = Path(path)
        self.batch_size = max(1, int(batch_size))
        self.filter_regex = filter_regex
        self.state_file = Path(state_file) if state_file else None
        self.kind = str(kind)
        self.poll_interval_s = float(poll_interval_s)
        self.missing_file_retries = max(1, int(missing_file_retries))
        # Runtime state (populated by ``_load_state`` / ``run``).
        self._last_offset: int = 0
        self._last_inode: int | None = None
        self._started_at: str = ""

    # --- State persistence ----------------------------------------------

    def _load_state(self, *, resume: bool, reset: bool) -> None:
        """Populate ``_last_offset`` / ``_last_inode`` from state file.

        - ``reset=True`` → ignore any saved state, start from offset 0.
        - ``resume=True`` → read the state file and restore offset/inode.
        - Both False (default) → start from EOF of the current file
          (classic ``tail -f`` behaviour). We do NOT consult the state
          file in this mode; the offset is set to the file's current
          size on first open.
        """
        self._last_offset = 0
        self._last_inode = None
        if reset or not resume or self.state_file is None:
            return
        try:
            raw = self.state_file.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return  # corrupt or missing — start fresh
        if not isinstance(data, dict):
            return
        try:
            self._last_offset = int(data.get("last_offset", 0))
        except (TypeError, ValueError):
            self._last_offset = 0
        inode_raw = data.get("last_inode")
        try:
            self._last_inode = int(inode_raw) if inode_raw is not None else None
        except (TypeError, ValueError):
            self._last_inode = None
        self._started_at = str(data.get("started_at", ""))

    def _save_state(self) -> None:
        """Write ``{kind, last_offset, last_inode, started_at}`` to state file.

        Best-effort: failures are swallowed (the tail loop must not
        crash on a state-write error — the operator can always
        ``--reset`` to recover).
        """
        if self.state_file is None:
            return
        payload = {
            "kind": self.kind,
            "last_offset": int(self._last_offset),
            "last_inode": self._last_inode,
            "started_at": self._started_at,
        }
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(self.state_file.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, self.state_file)
        except OSError:
            pass

    # --- Inode + open helpers -------------------------------------------

    @staticmethod
    def _inode_of(path: Path) -> int | None:
        """Return ``st_ino`` or ``None`` if the file is gone."""
        try:
            return path.stat().st_ino
        except OSError:
            return None

    def _await_file(self, stop_predicate: Callable[[], bool] | None) -> bool:
        """Wait for ``self.path`` to exist. Returns ``True`` once it does.

        Returns ``False`` if the file never appears within
        ``missing_file_retries`` polls OR ``stop_predicate`` trips.
        """
        for _ in range(self.missing_file_retries):
            if stop_predicate is not None and stop_predicate():
                return False
            if self.path.exists():
                return True
            time.sleep(self.poll_interval_s)
        return self.path.exists()

    # --- Main async generator -------------------------------------------

    async def run(
        self,
        *,
        resume: bool = False,
        reset: bool = False,
        stop_predicate: Callable[[], bool] | None = None,
        max_batches: int | None = None,
    ) -> "AsyncIterator[list[str]]":
        """Yield batches of new lines appended to ``self.path``.

        Args:
            resume: Continue from the offset saved in ``state_file``.
            reset: Start from byte 0 (ignore saved state).
            stop_predicate: Zero-arg callable; the loop stops when it
                returns ``True``. Used by tests to bound execution.
            max_batches: If set, stop after yielding this many batches.
                Useful for tests; ``None`` means run forever (until
                ``stop_predicate`` or file exhaustion).

        Yields:
            Lists of 1..``batch_size`` lines. A partial batch is
            yielded when the source pauses (no new data for one poll
            interval) so the consumer sees timely output.

        The generator is async so it can be composed with other
        asyncio tasks (e.g. a heartbeat writer). Internally it uses
        ``asyncio.sleep``; file I/O is synchronous (files are small
        and reads are cheap — true async file I/O would require
        ``aiofiles``, which is an unwanted dependency).
        """
        import asyncio

        self._load_state(resume=resume, reset=reset)
        if not self._started_at:
            self._started_at = datetime.now(timezone.utc).isoformat()

        batches_yielded = 0
        # Buffer of pending lines not yet yielded (waiting for either
        # batch_size to fill or a source pause).
        pending: list[str] = []

        # Wait for the file to appear (rotation may be mid-swap).
        if not self._await_file(stop_predicate):
            return

        # Determine the starting inode + offset on first open.
        current_inode = self._inode_of(self.path)
        if current_inode is None:
            return
        if self._last_inode is not None and self._last_inode != current_inode:
            # Saved inode differs from current → file was rotated
            # while we were away. Start from 0 of the new file.
            self._last_offset = 0
        self._last_inode = current_inode

        # Open once; we reopen only when inode changes.
        fh = self.path.open("r", encoding="utf-8", errors="replace")
        try:
            if resume and self._last_offset > 0:
                fh.seek(self._last_offset)
            elif not reset and not resume:
                # Classic ``tail -f``: start at EOF (skip existing).
                fh.seek(0, os.SEEK_END)
                self._last_offset = fh.tell()
            else:
                # reset=True OR resume with offset=0 → from beginning.
                fh.seek(0)
                self._last_offset = 0

            idle_polls = 0
            while True:
                if stop_predicate is not None and stop_predicate():
                    break
                if max_batches is not None and batches_yielded >= max_batches:
                    break

                # Rotation check: if inode changed, reopen from 0.
                new_inode = self._inode_of(self.path)
                if new_inode is not None and new_inode != self._last_inode:
                    fh.close()
                    self._last_inode = new_inode
                    self._last_offset = 0
                    fh = self.path.open("r", encoding="utf-8", errors="replace")
                    fh.seek(0)

                chunk = fh.read()
                if chunk:
                    idle_polls = 0
                    # Split on newlines; keep the trailing partial in buf.
                    lines_complete, trailing = self._split_lines(chunk)
                    for line in lines_complete:
                        stripped = line.strip()
                        if not stripped:
                            continue
                        if self.filter_regex is not None and not self.filter_regex.search(stripped):
                            continue
                        pending.append(stripped)
                        if len(pending) >= self.batch_size:
                            yield list(pending)
                            pending.clear()
                            batches_yielded += 1
                            self._last_offset = fh.tell()
                            self._save_state()
                            if max_batches is not None and batches_yielded >= max_batches:
                                return
                    # If there's a trailing partial line, we leave the
                    # file cursor mid-line; ``fh.tell()`` reflects that.
                    # We only update ``_last_offset`` on batch boundaries
                    # so resume is consistent with batch granularity.
                    continue

                # No new data this poll.
                idle_polls += 1
                # If we have pending lines and the source paused for at
                # least one poll, flush them as a partial batch.
                if pending and idle_polls >= 1:
                    yield list(pending)
                    pending.clear()
                    batches_yielded += 1
                    self._last_offset = fh.tell()
                    self._save_state()
                await asyncio.sleep(self.poll_interval_s)
        finally:
            fh.close()
            # Final state save so the next ``--resume`` picks up where
            # we left off (even on Ctrl+C, since the finally runs).
            self._save_state()

    @staticmethod
    def _split_lines(chunk: str) -> tuple[list[str], str]:
        """Split ``chunk`` into complete lines + trailing partial.

        Mirrors the logic in :func:`_iter_new_lines`: a trailing
        partial line (no newline yet) is kept for the next iteration.
        """
        if "\n" not in chunk:
            return [], chunk
        parts = chunk.split("\n")
        # The last element is the partial (possibly empty).
        return parts[:-1], parts[-1]


def follow_state_path(
    kind: str, *, state_dir: Path | None = None,
) -> Path:
    """Resolve the state file path for a given follow ``kind``.

    ``kind`` is one of ``"audit"`` / ``"metrics"`` (or any custom
    label). The state file lives under ``state_dir`` (default:
    ``settings.cli_follow_state_dir``) and is named
    ``.follow-state-{kind}.json``.

    Kept as a module-level function so tests can override
    ``settings.cli_follow_state_dir`` without instantiating a
    :class:`Follower`.
    """
    if state_dir is None:
        from harness.config import settings
        state_dir = settings.cli_follow_state_dir
    return Path(state_dir) / _FOLLOW_STATE_FILENAME_FMT.format(kind=kind)


def run_follow_async(
    follower: Follower,
    *,
    on_batch: Callable[[list[str]], None],
    resume: bool = False,
    reset: bool = False,
    stop_predicate: Callable[[], bool] | None = None,
    max_batches: int | None = None,
) -> int:
    """Run a :class:`Follower` synchronously, dispatching each batch.

    Thin wrapper around ``asyncio.run(follower.run(...))`` that calls
    ``on_batch`` for each yielded batch. Returns 0 on clean exit
    (``stop_predicate`` / ``max_batches`` reached), 130 on
    ``KeyboardInterrupt`` (Ctrl+C).

    Used by :func:`cmd_hooks_audit_follow` and
    :func:`cmd_observability_metrics_follow` so they stay synchronous
    CLI handlers while delegating the tail logic to the async
    :class:`Follower`.
    """
    import asyncio

    async def _drive() -> int:
        try:
            async for batch in follower.run(
                resume=resume, reset=reset,
                stop_predicate=stop_predicate,
                max_batches=max_batches,
            ):
                on_batch(batch)
        except KeyboardInterrupt:
            return 130
        return 0

    try:
        return asyncio.run(_drive())
    except KeyboardInterrupt:
        print(
            f"\n[harness] {follower.kind} follow interrupted; exiting.",
            file=sys.stderr,
        )
        return 0


__all__ = [
    "cmd_hooks_audit_follow",
    "cmd_observability_metrics_follow",
    "_iter_new_lines",
    "_rotate_if_needed",
    "_snapshot_diff",
    "_audit_file_for",
    # Phase 4.12 v1.22.0
    "Follower",
    "follow_state_path",
    "run_follow_async",
]
