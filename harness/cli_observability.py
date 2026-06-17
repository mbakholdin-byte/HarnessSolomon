"""Phase 4.4 v1.13.0: ``harness observability`` CLI subcommand.

Public surface:

  - :func:`_cmd_observability_log`     — tail the JSONL log.
  - :func:`_cmd_observability_metrics`  — scrape /metrics (raw text).
  - :func:`_cmd_observability_health`   — GET /health/{level}.
  - :func:`_cmd_observability_stats`    — parse the in-process
    PrometheusMetrics singleton via ``snapshot()`` (Phase 4.4 v1.13.0).

HTTP reads use ``urllib.request`` (stdlib) — no new deps. Exit codes:

  - 0 on success.
  - 1 on ``degraded`` health / connection error.
  - 2 on ``unhealthy`` health / HTTP 4xx-5xx / invalid args.

Trust boundary: imports only from ``harness.observability.*``,
``harness.config``, and stdlib. No ``harness.agents`` or
``harness.server`` imports (test enforces this).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _http_get(url: str, *, timeout_s: float = 5.0) -> tuple[int, bytes]:
    """Tiny urllib wrapper.

    Returns ``(status_code, body)``. Raises ``OSError`` on connection
    failure (caller maps to exit code 1).
    """
    req = urllib.request.Request(url, headers={"Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return resp.status, resp.read()


def _resolve_log_path(log_dir: Path, date_str: str | None) -> Path:
    """Return the path to the harness-YYYY-MM-DD.jsonl file.

    Date is interpreted as UTC to match ``JsonlLogger._path_for``
    (which uses ``datetime.now(timezone.utc)``).
    """
    if date_str is None:
        d = datetime.now(timezone.utc)
    else:
        d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return log_dir / f"harness-{d.strftime('%Y-%m-%d')}.jsonl"


def _cmd_observability_log(args: argparse.Namespace) -> int:
    """``harness observability log`` — read JSONL log file locally.

    Local file read (no server). Tail last N lines (default 20),
    optionally filter by top-level ``event`` field, optionally
    read a specific date (UTC, format YYYY-MM-DD).
    """
    from harness.config import settings
    from harness.observability.events import LogEvent  # noqa: F401  (ensures schema)

    log_dir: Path = settings.observability_log_dir
    path = _resolve_log_path(log_dir, args.date)

    if not path.exists():
        if args.json:
            print(json.dumps({"entries": [], "path": str(path), "note": "file not found"}))
        else:
            print(f"(no log file at {path})", file=sys.stderr)
        return 0

    try:
        # Cap at ~max_bytes to avoid reading multi-MB files.
        max_bytes = max(64 * 1024, int(getattr(args, "max_bytes", 0)) or 0)
        with path.open("rb") as f:
            if max_bytes > 0:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", errors="replace")
    except OSError as exc:
        print(f"[harness] observability log: cannot read {path}: {exc}", file=sys.stderr)
        return 2

    lines = [ln for ln in data.splitlines() if ln.strip()]
    if args.tail and args.tail > 0:
        lines = lines[-args.tail :]

    entries: list[dict[str, Any]] = []
    for ln in lines:
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        entries.append(obj)

    if args.event:
        wanted = {e.strip() for e in args.event.split(",") if e.strip()}
        if wanted:
            entries = [e for e in entries if e.get("event") in wanted]

    if args.json:
        print(
            json.dumps(
                {
                    "path": str(path),
                    "count": len(entries),
                    "entries": entries,
                },
                ensure_ascii=False, indent=2,
            )
        )
        return 0

    if not entries:
        print(f"(no entries in {path.name})", file=sys.stderr)
        return 0

    # Pretty table: timestamp | event | request_id | one-line payload summary
    print(f"path: {path}")
    for e in entries:
        ts = e.get("timestamp", "")
        ev = e.get("event", "?")
        rid = e.get("request_id", "")
        payload = e.get("payload", {})
        if isinstance(payload, dict):
            # Pick a few useful keys for at-a-glance display.
            notable = []
            for k in ("tool_name", "model", "event", "status", "decision",
                      "kind", "hook_name", "severity", "channel"):
                if k in payload:
                    notable.append(f"{k}={payload[k]}")
            summary = " ".join(notable)
        else:
            summary = repr(payload)[:80]
        print(f"  {ts}  {ev:24s}  rid={rid:18s}  {summary}")
    return 0


def _filter_metrics(text: str, pattern: str | None) -> str:
    """Apply ``--filter`` regex to Prometheus text.

    Strategy:
      - Track the most recent ``# HELP <name>`` and ``# TYPE <name>``
        lines.
      - Keep a metric line iff its NAME (the part before ``{`` or
        end of token) matches the regex.
      - Emit the most recent HELP/TYPE pair when the metric matches.
      - Comment-only lines (``#`` not followed by HELP/TYPE) are kept
        only if a following metric matches.

    If ``pattern`` is None or empty, return the input unchanged.
    """
    if not pattern:
        return text
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        print(f"[harness] observability metrics: invalid --filter regex: {exc}",
              file=sys.stderr)
        return text

    out_lines: list[str] = []
    pending_help: str | None = None
    pending_type: str | None = None
    pending_help_name: str | None = None
    pending_type_name: str | None = None

    def _flush_pending() -> None:
        nonlocal pending_help, pending_type, pending_help_name, pending_type_name
        # Emit only the most recent HELP/TYPE for the metric we just
        # matched. (HELP and TYPE names should match; in practice they do.)
        if pending_help and pending_help_name and (
            pending_type_name is None or pending_type_name == pending_help_name
        ):
            out_lines.append(pending_help)
        if pending_type and pending_type_name and (
            pending_help_name is None or pending_help_name == pending_type_name
        ):
            out_lines.append(pending_type)
        pending_help = None
        pending_type = None
        pending_help_name = None
        pending_type_name = None

    for raw in text.splitlines():
        if not raw:
            _flush_pending()
            out_lines.append(raw)
            continue
        if raw.startswith("# HELP "):
            # ``# HELP <name> <description>`` — split into
            # ``["#", "HELP", "<name> <description>"]`` when
            # ``maxsplit=2``. We need the name (first whitespace-
            # delimited token after "HELP"), not the description.
            # Re-split the tail after "# HELP " to get the name.
            tail = raw[len("# HELP "):]
            name = tail.split(None, 1)[0] if tail.strip() else ""
            pending_help = raw
            pending_help_name = name
            continue
        if raw.startswith("# TYPE "):
            # ``# TYPE <name> <type>`` — same as HELP, name is the
            # first whitespace-delimited token after "TYPE".
            tail = raw[len("# TYPE "):]
            name = tail.split(None, 1)[0] if tail.strip() else ""
            pending_type = raw
            pending_type_name = name
            continue
        if raw.startswith("#"):
            # Other comment; emit verbatim (preserves context).
            out_lines.append(raw)
            continue
        # Metric line: ``name{labels} value`` or ``name value``.
        # Extract the name (first whitespace, ``{``, or end-of-line).
        m = re.match(r"^([A-Za-z_:][A-Za-z0-9_:]*)", raw)
        name = m.group(1) if m else ""
        if name and regex.search(name):
            _flush_pending()
            out_lines.append(raw)
    # End of input — drop any pending HELP/TYPE that no metric ever
    # claimed (they belong to a metric that was filtered out).
    return "\n".join(out_lines) + ("\n" if text.endswith("\n") else "")
        # else: drop (no match)
    return "\n".join(out_lines) + ("\n" if text.endswith("\n") else "")


def _cmd_observability_metrics(args: argparse.Namespace) -> int:
    """``harness observability metrics`` — scrape ``GET /metrics``.

    Optional ``--filter`` regex narrows output (HELP/TYPE lines
    for matched metrics are kept; see :func:`_filter_metrics`).

    We do NOT support ``--json`` for this subcommand: the wire
    format is Prometheus text, not JSON (see Plan review #18).
    """
    base = (args.base_url or "http://127.0.0.1:8765").rstrip("/")
    url = base + "/metrics"
    try:
        status, body = _http_get(url, timeout_s=float(args.timeout_s or 5.0))
    except (urllib.error.URLError, OSError) as exc:
        print(
            f"[harness] observability metrics: cannot reach {url} "
            f"({type(exc).__name__}: {exc}). Is `harness serve` running?",
            file=sys.stderr,
        )
        return 1
    if status != 200:
        print(
            f"[harness] observability metrics: HTTP {status} from {url}",
            file=sys.stderr,
        )
        return 2
    text = body.decode("utf-8", errors="replace")
    if getattr(args, "filter", None):
        text = _filter_metrics(text, args.filter)
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")
    return 0


def _cmd_observability_health(args: argparse.Namespace) -> int:
    """``harness observability health`` — GET /health/{level}.

    Exit codes:
      - 0 status=ok
      - 1 status=degraded
      - 2 status=unhealthy OR HTTP 4xx-5xx OR connection error
    """
    base = (args.base_url or "http://127.0.0.1:8765").rstrip("/")
    level = args.level or "deep"
    if level not in ("live", "ready", "deep"):
        print(
            f"[harness] observability health: invalid level {level!r}; "
            f"expected live|ready|deep",
            file=sys.stderr,
        )
        return 2
    url = f"{base}/health/{level}"
    try:
        status, body = _http_get(url, timeout_s=float(args.timeout_s or 5.0))
    except (urllib.error.URLError, OSError) as exc:
        print(
            f"[harness] observability health: cannot reach {url} "
            f"({type(exc).__name__}: {exc}). Is `harness serve` running?",
            file=sys.stderr,
        )
        return 2
    if status >= 400:
        print(
            f"[harness] observability health: HTTP {status} from {url}",
            file=sys.stderr,
        )
        return 2
    try:
        report = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        report = {"raw": body.decode("utf-8", errors="replace")}

    if args.json:
        print(json.dumps({"level": level, "http_status": status, "report": report},
                         ensure_ascii=False, indent=2))
    else:
        rep_status = report.get("status", "?") if isinstance(report, dict) else "?"
        print(f"level       : {level}")
        print(f"http_status : {status}")
        print(f"status      : {rep_status}")
        if isinstance(report, dict):
            if "version" in report:
                print(f"version     : {report.get('version')}")
            if "project_root" in report:
                print(f"project_root: {report.get('project_root')}")
            checks = report.get("checks")
            if isinstance(checks, dict) and checks:
                print("checks      :")
                for name, payload in checks.items():
                    if not isinstance(payload, dict):
                        print(f"  {name:18s}  {payload}")
                        continue
                    pstatus = payload.get("status", "?")
                    extras = ", ".join(
                        f"{k}={v}" for k, v in payload.items()
                        if k not in ("status",)
                    )
                    line = f"  {name:18s}  {pstatus}"
                    if extras:
                        line += f"  ({extras})"
                    print(line)

    rep_status = report.get("status", "ok") if isinstance(report, dict) else "ok"
    if rep_status == "ok":
        return 0
    if rep_status == "degraded":
        return 1
    return 2  # unhealthy or unknown


def _cmd_observability_stats(args: argparse.Namespace) -> int:
    """``harness observability stats`` — in-process counter snapshot.

    Reads the CLI process's own ``PrometheusMetrics`` singleton
    via :func:`harness.observability.get_observability` and
    :meth:`PrometheusMetrics.snapshot`.

    Caveat (documented in the help text): the CLI process is
    fresh, so the snapshot will be empty unless counters have
    been incremented in this very process. For live server
    counters use ``harness observability metrics`` instead.
    """
    from harness.observability import get_observability

    obs = get_observability()
    snapshot = obs.metrics.snapshot()

    if args.json:
        # Convert the inner-tuple keys to lists for JSON.
        out: dict[str, dict[str, float]] = {}
        for name, labelmap in snapshot.items():
            sub: dict[str, float] = {}
            for labels, value in labelmap.items():
                if labels:
                    key = ",".join(f"{k}={v}" for k, v in labels)
                else:
                    key = "(no labels)"
                sub[key] = value
            out[name] = sub
        print(
            json.dumps(
                {
                    "metrics": out,
                    "count": len(out),
                    "note": (
                        "in-process snapshot (CLI starts fresh — "
                        "counters are 0 unless incremented in this process). "
                        "Use `harness observability metrics` for live "
                        "server values."
                    ),
                },
                ensure_ascii=False, indent=2,
            )
        )
        return 0

    if not snapshot:
        print(
            "(no metrics — prometheus_client is not installed OR counters are 0).",
            file=sys.stderr,
        )
        print(
            "Hint: use `harness observability metrics` for the live server.",
            file=sys.stderr,
        )
        return 0

    print(f"{'metric':40s}  {'labels':40s}  value")
    print("-" * 100)
    for name in sorted(snapshot):
        for labels, value in sorted(snapshot[name].items()):
            label_str = ",".join(f"{k}={v}" for k, v in labels) if labels else ""
            if len(label_str) > 40:
                label_str = label_str[:37] + "..."
            print(f"{name:40s}  {label_str:40s}  {value:g}")
    return 0


__all__ = [
    "_cmd_observability_log",
    "_cmd_observability_metrics",
    "_cmd_observability_health",
    "_cmd_observability_stats",
]
