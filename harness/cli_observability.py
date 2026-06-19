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

    Phase 4.7 v1.17.0: ``--diff BEFORE.json AFTER.json`` mode
    compares two JSON snapshots (each produced by
    ``observability stats --json``) and prints the per-metric
    delta. Exit codes in diff mode:
        0 — no changes (identical BEFORE / AFTER).
        1 — file-not-found OR invalid JSON.
        2 — at least one metric differs (counter changed,
            new metric appeared, metric removed).
    """
    diff_files = getattr(args, "diff", None)
    if diff_files:
        return _run_stats_diff(
            Path(diff_files[0]), Path(diff_files[1]),
            json_output=bool(getattr(args, "json", False)),
        )

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


def _load_snapshot(path: Path) -> dict[str, dict[str, float]]:
    """Load a metrics snapshot from a JSON file.

    Accepts two formats:

      1. ``{"metrics": {name: {labels_key: value}}}`` — the layout
         produced by ``harness observability stats --json`` (we
         ignore the surrounding ``count`` / ``note`` fields).
      2. ``{name: {labels_key: value}}`` — a bare metrics dict
         (useful when snapshots are produced by another tool).

    Raises ``OSError`` if the file is missing, ``json.JSONDecodeError``
    if the file is not valid JSON, ``ValueError`` if the structure
    does not match either shape. The caller maps these to exit 1.
    """
    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(
            f"expected a JSON object at top level, got {type(data).__name__}"
        )
    if "metrics" in data and isinstance(data["metrics"], dict):
        metrics = data["metrics"]
    else:
        metrics = data
    # Normalise: every value must be a dict[str, float/int].
    out: dict[str, dict[str, float]] = {}
    for name, labelmap in metrics.items():
        if not isinstance(labelmap, dict):
            raise ValueError(
                f"metric {name!r}: expected a label->value map, "
                f"got {type(labelmap).__name__}"
            )
        sub: dict[str, float] = {}
        for label_key, value in labelmap.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(
                    f"metric {name!r} label {label_key!r}: expected a "
                    f"number, got {type(value).__name__}"
                )
            sub[str(label_key)] = float(value)
        out[str(name)] = sub
    return out


def _run_stats_diff(
    before_path: Path, after_path: Path, *, json_output: bool,
) -> int:
    """Phase 4.7 v1.17.0: compute per-metric deltas.

    Reads two snapshots (each from ``observability stats --json``
    or a compatible bare-metrics JSON file), and for each
    (metric, label-set) pair computes ``Δ = AFTER − BEFORE``.

    - Pairs present in both: ``Δ = after − before``.
    - Pairs only in AFTER: marked ``NEW`` with ``+after``.
    - Pairs only in BEFORE: marked ``REMOVED`` with ``-before``.

    Returns:
        0 — no differences.
        1 — file-not-found or invalid JSON.
        2 — at least one delta (changed / new / removed).
    """
    # --- Load both snapshots (exit 1 on any read/parse failure). ---
    try:
        before = _load_snapshot(before_path)
    except FileNotFoundError:
        print(
            f"[harness] observability stats --diff: BEFORE file not found: "
            f"{before_path}",
            file=sys.stderr,
        )
        return 1
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(
            f"[harness] observability stats --diff: cannot parse BEFORE "
            f"({before_path}): {exc}",
            file=sys.stderr,
        )
        return 1
    try:
        after = _load_snapshot(after_path)
    except FileNotFoundError:
        print(
            f"[harness] observability stats --diff: AFTER file not found: "
            f"{after_path}",
            file=sys.stderr,
        )
        return 1
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(
            f"[harness] observability stats --diff: cannot parse AFTER "
            f"({after_path}): {exc}",
            file=sys.stderr,
        )
        return 1

    # --- Compute deltas. ---
    # Key = (metric_name, label_key). We build a single flat dict
    # per side so new/removed pairs surface regardless of which
    # metric dict they live under.
    before_flat: dict[tuple[str, str], float] = {}
    for name, labelmap in before.items():
        for lk, value in labelmap.items():
            before_flat[(name, lk)] = value
    after_flat: dict[tuple[str, str], float] = {}
    for name, labelmap in after.items():
        for lk, value in labelmap.items():
            after_flat[(name, lk)] = value

    all_keys = sorted(set(before_flat) | set(after_flat))
    rows: list[dict[str, Any]] = []
    for key in all_keys:
        name, label_key = key
        in_before = key in before_flat
        in_after = key in after_flat
        if in_before and in_after:
            b = before_flat[key]
            a = after_flat[key]
            delta = a - b
            if delta == 0:
                continue  # no change — skip
            rows.append({
                "metric": name,
                "labels": label_key,
                "before": b,
                "after": a,
                "delta": delta,
                "status": "CHANGED",
            })
        elif in_after:
            a = after_flat[key]
            rows.append({
                "metric": name,
                "labels": label_key,
                "before": None,
                "after": a,
                "delta": a,
                "status": "NEW",
            })
        else:  # in_before only
            b = before_flat[key]
            rows.append({
                "metric": name,
                "labels": label_key,
                "before": b,
                "after": None,
                "delta": -b,
                "status": "REMOVED",
            })

    has_delta = bool(rows)

    if json_output:
        # NDJSON: one record per delta row (makes streaming parses
        # trivial for downstream tools). An extra summary record
        # is NOT appended to keep the stream uniform.
        for r in rows:
            print(json.dumps(r, ensure_ascii=False))
    else:
        if not rows:
            print("(no metric changes)")
        else:
            print(
                f"{'metric':36s}  {'labels':28s}  "
                f"{'status':8s}  {'delta':>14s}"
            )
            print("-" * 94)
            for r in rows:
                delta_s: str
                if r["status"] == "NEW":
                    delta_s = f"+{r['delta']:g}"
                elif r["status"] == "REMOVED":
                    delta_s = f"{r['delta']:g}"
                else:
                    d = r["delta"]
                    delta_s = f"{d:+g}"
                labels_short = r["labels"]
                if len(labels_short) > 28:
                    labels_short = labels_short[:25] + "..."
                name_short = r["metric"]
                if len(name_short) > 36:
                    name_short = name_short[:33] + "..."
                print(
                    f"{name_short:36s}  {labels_short:28s}  "
                    f"{r['status']:8s}  {delta_s:>14s}"
                )

    return 2 if has_delta else 0


# === webhooks dlq (Phase 4.13B Drift 2) ===============================
#
# Subcommand surface: ``harness observability webhooks dlq [list|replay <id>]``.
# Reads from the admin endpoint
# ``GET /api/v1/observability/webhooks/dlq`` (list) and
# ``POST /api/v1/observability/webhooks/dlq/{id}/replay`` (replay).
# Reuses the existing ``_http_get`` urllib wrapper for list; replay
# needs a POST so we add a tiny ``_http_post`` helper (also stdlib).


def _http_post(
    url: str,
    *,
    timeout_s: float = 10.0,
    token: str = "",
) -> tuple[int, bytes]:
    """Tiny urllib POST wrapper (no body — replay takes no params).

    Returns ``(status_code, body)``. Raises ``OSError`` on connection
    failure (caller maps to exit code 1).
    """
    headers = {"Accept": "*/*"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url, data=b"", headers=headers, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return resp.status, resp.read()


def _cmd_webhooks_dlq(args: argparse.Namespace) -> int:
    """``harness observability webhooks dlq`` — list or replay DLQ.

    Sub-actions:

      * ``list`` (default): GET /api/v1/observability/webhooks/dlq
        → prints recent unreplayed DLQ entries.
      * ``replay <id>``: POST /api/v1/observability/webhooks/dlq/<id>/replay
        → re-sends the entry's payload with the current secret.

    The ``--base-url`` flag is inherited from the parent observability
    namespace (default ``http://127.0.0.1:8765``). ``--token`` is
    optional (open dev mode); pass the admin API token for scope-
    gated deployments.
    """
    action = getattr(args, "dlq_action", "list") or "list"
    base_url = getattr(args, "base_url", "http://127.0.0.1:8765")
    token = getattr(args, "token", "") or ""
    json_output = bool(getattr(args, "json", False))

    if action == "list":
        limit = int(getattr(args, "limit", 100) or 100)
        include_replayed = bool(
            getattr(args, "include_replayed", False)
        )
        url = (
            f"{base_url.rstrip('/')}/api/v1/observability/webhooks/dlq"
            f"?limit={limit}&include_replayed="
            f"{'true' if include_replayed else 'false'}"
        )
        try:
            if token:
                # Inject Authorization into _http_get via a wrapper.
                def _authed_get(u: str, *, timeout_s: float = 5.0) -> tuple[int, bytes]:
                    req = urllib.request.Request(
                        u, headers={
                            "Accept": "*/*",
                            "Authorization": f"Bearer {token}",
                        },
                    )
                    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                        return resp.status, resp.read()
                status, body = _authed_get(url)
            else:
                status, body = _http_get(url)
        except OSError as exc:
            print(
                f"[harness] webhooks dlq: cannot reach {base_url}: {exc}",
                file=sys.stderr,
            )
            return 1
        if status >= 400:
            print(
                f"[harness] webhooks dlq: HTTP {status}: "
                f"{body.decode('utf-8', errors='replace')[:200]}",
                file=sys.stderr,
            )
            return 2
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            print(
                f"[harness] webhooks dlq: invalid JSON response",
                file=sys.stderr,
            )
            return 2
        entries = payload.get("entries", []) if isinstance(payload, dict) else []
        if json_output:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            if not entries:
                print("(no DLQ entries)")
            else:
                print(
                    f"{'id':>6s}  {'kind':20s}  {'url':40s}  "
                    f"{'attempts':>8s}  {'failed_at':24s}"
                )
                print("-" * 108)
                for e in entries:
                    kind = str(e.get("event_kind", ""))[:20]
                    url_s = str(e.get("url", ""))[:40]
                    attempts = e.get("attempts", 0)
                    failed = str(e.get("failed_at", ""))[:24]
                    print(
                        f"{e.get('id', '?'):>6}  {kind:20s}  "
                        f"{url_s:40s}  {attempts:>8}  {failed:24s}"
                    )
                print(f"\n({len(entries)} entries)")
        return 0

    if action == "replay":
        dlq_id = getattr(args, "dlq_id", None)
        if dlq_id is None:
            print(
                "[harness] webhooks dlq replay: missing <id> argument",
                file=sys.stderr,
            )
            return 2
        url = (
            f"{base_url.rstrip('/')}/api/v1/observability/webhooks/dlq/"
            f"{int(dlq_id)}/replay"
        )
        try:
            status, body = _http_post(url, token=token)
        except OSError as exc:
            print(
                f"[harness] webhooks dlq replay: cannot reach "
                f"{base_url}: {exc}",
                file=sys.stderr,
            )
            return 1
        if status >= 400:
            print(
                f"[harness] webhooks dlq replay: HTTP {status}: "
                f"{body.decode('utf-8', errors='replace')[:200]}",
                file=sys.stderr,
            )
            return 2
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {"raw": body.decode("utf-8", errors="replace")}
        if json_output:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            replayed = payload.get("replayed", False)
            status_code = payload.get("status_code", "?")
            verdict = "REPLAYED" if replayed else "NOT REPLAYED"
            print(
                f"dlq id={dlq_id} → HTTP {status_code} ({verdict})"
            )
        return 0

    print(
        f"[harness] webhooks dlq: unknown action {action!r} "
        f"(expected 'list' or 'replay')",
        file=sys.stderr,
    )
    return 2


__all__ = [
    "_cmd_observability_log",
    "_cmd_observability_metrics",
    "_cmd_observability_health",
    "_cmd_observability_stats",
    "_run_stats_diff",
    "_cmd_webhooks_dlq",
]
