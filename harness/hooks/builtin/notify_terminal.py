"""Phase 4.3+ → v1.18.0: Builtin NotifyTerminalHook — multi-channel
Notification fanout with per-channel retry + deadletter queue.

Default ON. Listens to ``Notification`` events and dispatches the
payload to one or more channels:

    - ``stdout``  → write ``[severity] message`` to stderr (the
      canonical side-channel; never collides with agent output).
    - ``webhook`` → HTTP POST to ``settings.hooks_notify_webhook_url``
      with HMAC-SHA256 signature header.
    - ``desktop`` → platform-specific toast/notification command
      (Windows: PowerShell BurntToast or ``msg *`` fallback,
      macOS: ``osascript``, Linux: ``notify-send``). Opt-in via
      ``settings.hooks_notify_desktop_enabled``.
    - ``slack``   → POST a Slack incoming-webhook payload to
      ``settings.hooks_notify_slack_webhook_url``. Severity maps to
      an attachment color (info=green, warn=yellow, error=red). No
      HMAC — the webhook URL itself is the secret (Phase 4.6 v1.16.0).
    - ``teams``   → POST a Microsoft Teams MessageCard to
      ``settings.hooks_notify_teams_webhook_url``. Severity maps to
      ``themeColor`` (info=blue, warn=orange, error=red). No HMAC
      (Phase 4.6 v1.16.0).

Payload contract::

    {
        "severity": "info" | "warn" | "error",  # default: "info"
        "message":  "Compaction completed in 1.2s",  # required, non-empty
        "channels": ["stdout", "webhook", "desktop", "slack", "teams"],  # default: ["stdout"]
    }

Each channel is a separate handler dispatched concurrently via
``asyncio.gather`` (per-channel isolation). Since v1.18.0 each
channel is retried with exponential backoff on transient errors
(HTTP 5xx, timeout, OSError); on exhaustion or a permanent error
(HTTP 4xx, ValueError) the payload is persisted to a SQLite
deadletter queue (``notify_dlq`` table in ``agent-jobs.db``) so an
operator can inspect / replay lost notifications.

Webhook URLs (Slack/Teams/generic) are NEVER logged. Error messages
reference the env var NAME, never the URL value — mirrors the
``webhook_secret`` policy at ``config.py``.

Trust boundary: this module imports stdlib (asyncio, hashlib, hmac,
json, logging, sqlite3, sys, urllib, time) + ``harness.config`` +
``harness.observability.emit`` + ``aiosqlite``. No
``harness.agents`` / ``harness.server``.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Literal

import aiosqlite

from harness.config import Settings
from harness.hooks.context import HookContext, HookDecision
from harness.observability.emit import emit_notify_dlq, emit_notification_dispatched


logger = logging.getLogger("harness.hooks.builtin.notify_terminal")


# === Severity → prefix mapping ===

def _severity_to_prefix(severity: str) -> str:
    sev = severity.lower()
    if sev == "error":
        return "ERROR"
    if sev == "warn":
        return "WARN"
    return "INFO"


# === Channel error classification (Phase 4.8 v1.18.0) ===
#
# Two categories govern the retry / DLQ behaviour:
#
#   - ``transient``: the failure is likely to resolve on retry
#     (HTTP 5xx, network timeout, OSError). Retried up to
#     ``hooks_notify_max_retries`` times with exponential backoff.
#   - ``permanent``: the failure will not resolve on retry (HTTP 4xx,
#     ValueError). Skips retry and goes straight to the DLQ.
#
# Any unclassified exception (``Exception`` subclasses not explicitly
# mapped below) is treated as transient — a conservative default that
# prefers retry-and-record over silent loss.

ErrorCategory = Literal["transient", "permanent"]


@dataclass(frozen=True)
class ChannelError(Exception):
    """Structured error raised by a ``_deliver_*`` channel function.

    Attributes
    ----------
    category:
        ``transient`` (retryable) or ``permanent`` (not retryable).
    channel:
        Channel name (``stdout`` / ``webhook`` / ...) — used for DLQ
        labelling.
    status:
        HTTP status code when available (0 if unknown / non-HTTP).
    cause:
        Underlying exception type name (for logging / DLQ ``last_error``).
    message:
        Human-readable description of the failure.
    """

    category: ErrorCategory
    channel: str
    status: int
    cause: str
    message: str

    def __post_init__(self) -> None:
        # ``Exception`` is not cooperative with frozen dataclass by
        # default — call the Exception initialiser explicitly so
        # ``str(err)`` / traceback pickling work as callers expect.
        super().__init__(self.message)

    def __str__(self) -> str:  # noqa: D401 — readable repr
        parts = [f"[{self.category}]"]
        if self.status:
            parts.append(f"HTTP {self.status}")
        parts.append(f"channel={self.channel}")
        if self.cause:
            parts.append(f"({self.cause})")
        parts.append(self.message)
        return " ".join(parts)


def _classify_exception(exc: BaseException, channel: str) -> ChannelError:
    """Map an arbitrary exception to a :class:`ChannelError`.

    Mapping rules:

      - ``urllib.error.HTTPError`` with ``code >= 500`` → transient.
      - ``urllib.error.HTTPError`` with ``code in [400, 500)`` → permanent.
      - ``asyncio.TimeoutError`` / ``TimeoutError`` → transient.
      - ``OSError`` (socket errors, DNS, connection reset) → transient.
      - ``ValueError`` (malformed payload / config) → permanent.
      - Everything else → transient (conservative — retry + record).
    """
    import urllib.error as _ue

    if isinstance(exc, _ue.HTTPError):
        status = int(exc.code)
        category: ErrorCategory = "transient" if status >= 500 else "permanent"
        return ChannelError(
            category=category,
            channel=channel,
            status=status,
            cause=type(exc).__name__,
            message=f"{exc.reason} (HTTP {status})",
        )
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return ChannelError(
            category="transient",
            channel=channel,
            status=0,
            cause=type(exc).__name__,
            message="operation timed out",
        )
    if isinstance(exc, OSError):
        return ChannelError(
            category="transient",
            channel=channel,
            status=0,
            cause=type(exc).__name__,
            message=str(exc) or "OSError",
        )
    if isinstance(exc, ValueError):
        return ChannelError(
            category="permanent",
            channel=channel,
            status=0,
            cause=type(exc).__name__,
            message=str(exc) or "ValueError",
        )
    # Unclassified → transient (retry + record). This is the safe
    # default: an unknown error category should not silently drop the
    # notification without at least one retry attempt + a DLQ entry.
    return ChannelError(
        category="transient",
        channel=channel,
        status=0,
        cause=type(exc).__name__,
        message=str(exc) or "unknown error",
    )


# === Channel deliver functions (raw — raise ChannelError on failure) ===
#
# ``_deliver_*`` functions are the retry-aware transport layer. Unlike
# the legacy ``_handle_*`` wrappers (which log + swallow failures),
# these raise :class:`ChannelError` so the dispatcher can classify and
# retry. Each ``_deliver_*`` is paired with a ``_handle_*`` thin
# wrapper that preserves the v1.16.0 fail-open API for backward
# compatibility with existing callers and tests.


async def _deliver_stdout(payload: dict[str, Any], _settings: Any) -> None:
    """Write ``[severity] message`` to stderr. Never raises."""
    severity = payload.get("severity", "info")
    message = payload.get("message", "")
    if not message:
        return
    prefix = _severity_to_prefix(severity)
    print(f"[{prefix}] {message}", file=sys.stderr, flush=True)


def _do_urlopen_post(
    url: str, body: bytes, headers: dict[str, str], timeout_s: float
) -> int:
    """Synchronous POST helper. Returns HTTP status. Raises on error."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return int(resp.status)


async def _deliver_http_like(
    payload: dict[str, Any],
    settings: Any,
    *,
    channel: str,
    url_attr: str,
    timeout_attr: str,
    build_body: "Any",
    secret_attr: str | None = None,
) -> None:
    """Shared POST-with-retry transport for webhook / slack / teams.

    Raises :class:`ChannelError` on any failure. The caller (dispatcher)
    decides whether to retry.

    Parameters
    ----------
    url_attr:
        Name of the settings attribute that holds the target URL.
    timeout_attr:
        Name of the settings attribute that holds the per-request
        timeout (seconds).
    build_body:
        Callable ``(payload, settings) -> tuple[bytes, dict[str,str]]``
        returning ``(body, headers)`` for the POST.
    secret_attr:
        Optional name of the settings attribute holding the HMAC
        secret. When set, an ``X-Harness-Signature`` header is added.
    """
    url = getattr(settings, url_attr, "")
    if not url:
        logger.debug("NotifyTerminal: %s channel skipped (no URL configured)", channel)
        return
    timeout_s = float(getattr(settings, timeout_attr, 5.0))
    body, headers = build_body(payload, settings)
    if secret_attr is not None:
        secret = getattr(settings, secret_attr, "")
        if secret:
            sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
            headers["X-Harness-Signature"] = f"sha256={sig}"

    try:
        status = await asyncio.to_thread(_do_urlopen_post, url, body, headers, timeout_s)
    except Exception as exc:  # noqa: BLE001 — classified below
        raise _classify_exception(exc, channel) from exc

    if status >= 400:
        # Treat non-2xx as a transport failure so the dispatcher can
        # retry (5xx) or DLQ (4xx). The classification is done by
        # raising a synthetic HTTPError and re-classifying — keeps the
        # single mapping path in ``_classify_exception``.
        reason = "Server Error" if status >= 500 else "Client Error"
        synth = urllib.error.HTTPError(url, status, reason, {}, None)  # type: ignore[arg-type]
        raise _classify_exception(synth, channel) from synth


def _build_webhook_body(payload: dict[str, Any], _settings: Any) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-Harness-Event": "Notification"}
    return body, headers


async def _deliver_webhook(payload: dict[str, Any], settings: Any) -> None:
    """POST payload to ``settings.hooks_notify_webhook_url`` (HMAC-signed).

    Raises :class:`ChannelError` on failure.
    """
    await _deliver_http_like(
        payload,
        settings,
        channel="webhook",
        url_attr="hooks_notify_webhook_url",
        timeout_attr="hooks_notify_webhook_timeout_s",
        build_body=_build_webhook_body,
        secret_attr="hooks_notify_webhook_secret",
    )


# === Severity → color mapping (Slack + Teams) ===
#
# Slack attachment colors:
#   - "good"    → green (info)
#   - "warning" → yellow (warn)
#   - "danger"  → red (error)
# Teams themeColor is a hex string (no leading #):
#   - "0078D4" → blue (info — Teams default accent)
#   - "FFA500" → orange (warn)
#   - "FF0000" → red (error)

_SLACK_COLOR_MAP: dict[str, str] = {
    "info": "good",
    "warn": "warning",
    "error": "danger",
}

_TEAMS_COLOR_MAP: dict[str, str] = {
    "info": "0078D4",
    "warn": "FFA500",
    "error": "FF0000",
}


def _severity_to_slack_color(severity: str) -> str:
    """Map severity to Slack attachment color (good/warning/danger)."""
    return _SLACK_COLOR_MAP.get(severity.lower(), "good")


def _severity_to_teams_color(severity: str) -> str:
    """Map severity to Teams themeColor hex (no leading #)."""
    return _TEAMS_COLOR_MAP.get(severity.lower(), "0078D4")


def _redact_webhook_url(url: str) -> str:
    """Return a redacted form of a webhook URL for safe logging.

    Keeps scheme + host, replaces the path/query/fragment with ``***``.
    Example::

        >>> _redact_webhook_url("https://hooks.slack.com/services/T0/B1/secret")
        'https://hooks.slack.com/***'

        >>> _redact_webhook_url("https://outlook.office.com/webhook/abc/def")
        'https://outlook.office.com/***'

    Empty input returns ``"<unset>"`` for clarity in log lines.
    """
    if not url:
        return "<unset>"
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return "***"
    return f"{parsed.scheme}://{parsed.netloc}/***"


def _build_slack_body(payload: dict[str, Any], settings: Any) -> tuple[bytes, dict[str, str]]:
    severity = payload.get("severity", "info")
    message = payload.get("message", "")
    channel = getattr(settings, "hooks_notify_slack_channel", "")
    username = getattr(settings, "hooks_notify_slack_username", "Solomon Harness")
    color = _severity_to_slack_color(severity)
    slack_payload: dict[str, Any] = {
        "username": username,
        "text": message,
        "attachments": [
            {
                "color": color,
                "fields": [
                    {"title": "Event", "value": "Notification"},
                    {"title": "Severity", "value": severity},
                ],
            }
        ],
    }
    if channel:
        slack_payload["channel"] = channel
    body = json.dumps(slack_payload, sort_keys=True).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-Harness-Event": "Notification"}
    return body, headers


async def _deliver_slack(payload: dict[str, Any], settings: Any) -> None:
    """POST a Slack incoming-webhook payload.

    Raises :class:`ChannelError` on failure.
    """
    await _deliver_http_like(
        payload,
        settings,
        channel="slack",
        url_attr="hooks_notify_slack_webhook_url",
        timeout_attr="hooks_notify_slack_timeout_s",
        build_body=_build_slack_body,
    )


def _build_teams_body(payload: dict[str, Any], _settings: Any) -> tuple[bytes, dict[str, str]]:
    severity = payload.get("severity", "info")
    message = payload.get("message", "")
    theme_color = _severity_to_teams_color(severity)
    teams_payload: dict[str, Any] = {
        "@type": "MessageCard",
        "@context": "https://schema.org/extensions",
        "themeColor": theme_color,
        "summary": "Harness notification",
        "sections": [
            {
                "activityTitle": "Harness Alert",
                "text": f"Event: Notification  Severity: {severity}\n{message}",
            }
        ],
    }
    body = json.dumps(teams_payload, sort_keys=True).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-Harness-Event": "Notification"}
    return body, headers


async def _deliver_teams(payload: dict[str, Any], settings: Any) -> None:
    """POST a Microsoft Teams MessageCard payload.

    Raises :class:`ChannelError` on failure.
    """
    await _deliver_http_like(
        payload,
        settings,
        channel="teams",
        url_attr="hooks_notify_teams_webhook_url",
        timeout_attr="hooks_notify_teams_timeout_s",
        build_body=_build_teams_body,
    )


async def _deliver_desktop(payload: dict[str, Any], settings: Any) -> None:
    """Platform-specific desktop notification.

    Windows → PowerShell BurntToast (with ``msg *`` fallback).
    macOS   → ``osascript -e 'display notification ...'``.
    Linux   → ``notify-send``.

    Raises :class:`ChannelError` on failure (subprocess timeout or
    non-zero exit code classified as transient — the command may
    succeed on retry).
    """
    if not getattr(settings, "hooks_notify_desktop_enabled", False):
        return
    message = payload.get("message", "")
    severity = payload.get("severity", "info")
    if not message:
        return
    title = "Harness"
    platform = sys.platform
    cmd: list[str]
    if platform == "win32":
        cmd = ["msg", "*", f"[{_severity_to_prefix(severity)}] {message}"]
    elif platform == "darwin":
        safe_msg = message.replace('"', '\\"')
        safe_title = title.replace('"', '\\"')
        cmd = [
            "osascript",
            "-e",
            f'display notification "{safe_msg}" with title "{safe_title}"',
        ]
    else:
        safe_msg = message.replace('"', '\\"')
        cmd = ["notify-send", "-a", title, f"[{_severity_to_prefix(severity)}] {safe_msg}"]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ChannelError(
            category="permanent",
            channel="desktop",
            status=0,
            cause=type(exc).__name__,
            message=f"command not found: {cmd[0] if cmd else '?'}",
        ) from exc
    except Exception as exc:  # noqa: BLE001 — classified below
        raise _classify_exception(exc, "desktop") from exc

    try:
        await asyncio.wait_for(proc.communicate(), timeout=3.0)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise ChannelError(
            category="transient",
            channel="desktop",
            status=0,
            cause="TimeoutError",
            message=f"desktop command timed out: {cmd[0]}",
        ) from exc
    except Exception as exc:  # noqa: BLE001 — classified below
        raise _classify_exception(exc, "desktop") from exc

    if proc.returncode != 0:
        raise ChannelError(
            category="transient",
            channel="desktop",
            status=0,
            cause="SubprocessError",
            message=f"desktop command exit {proc.returncode}: {cmd[0]}",
        )


# === Legacy fail-open handlers (v1.16.0 API, kept for back-compat) ===
#
# These wrap the ``_deliver_*`` transports and swallow any
# :class:`ChannelError` (logging the redacted message). Existing
# callers and tests that expect ``_handle_*`` to never raise continue
# to work unchanged. The retry-aware path is ``_dispatch_to_channel``.


async def _handle_stdout(payload: dict[str, Any], _settings: Any) -> None:
    """Write ``[severity] message`` to stderr (fail-open)."""
    await _deliver_stdout(payload, _settings)


async def _handle_webhook(payload: dict[str, Any], settings: Any) -> None:
    """POST payload to ``settings.hooks_notify_webhook_url`` (fail-open).

    Logs a redacted warning on failure; never raises.
    """
    try:
        await _deliver_webhook(payload, settings)
    except ChannelError as e:
        url = getattr(settings, "hooks_notify_webhook_url", "")
        logger.warning(
            "NotifyTerminal: webhook %s (%s)",
            _redact_webhook_url(url),
            e,
        )


async def _handle_slack(payload: dict[str, Any], settings: Any) -> None:
    """POST a Slack incoming-webhook payload (fail-open)."""
    try:
        await _deliver_slack(payload, settings)
    except ChannelError as e:
        url = getattr(settings, "hooks_notify_slack_webhook_url", "")
        logger.warning(
            "NotifyTerminal: slack %s (%s)",
            _redact_webhook_url(url),
            e,
        )


async def _handle_teams(payload: dict[str, Any], settings: Any) -> None:
    """POST a Microsoft Teams MessageCard payload (fail-open)."""
    try:
        await _deliver_teams(payload, settings)
    except ChannelError as e:
        url = getattr(settings, "hooks_notify_teams_webhook_url", "")
        logger.warning(
            "NotifyTerminal: teams %s (%s)",
            _redact_webhook_url(url),
            e,
        )


async def _handle_desktop(payload: dict[str, Any], settings: Any) -> None:
    """Platform-specific desktop notification (fail-open)."""
    try:
        await _deliver_desktop(payload, settings)
    except ChannelError as e:
        logger.warning("NotifyTerminal: desktop %s", e)


# Channel dispatch table — legacy fail-open handlers.
_HANDLERS: dict[str, Any] = {
    "stdout": _handle_stdout,
    "webhook": _handle_webhook,
    "desktop": _handle_desktop,
    "slack": _handle_slack,
    "teams": _handle_teams,
}

# Retry-aware transports keyed by channel name.
_DELIVERERS: dict[str, Any] = {
    "stdout": _deliver_stdout,
    "webhook": _deliver_webhook,
    "desktop": _deliver_desktop,
    "slack": _deliver_slack,
    "teams": _deliver_teams,
}


# === Deadletter queue (SQLite) ===

_DLQ_SCHEMA = """
CREATE TABLE IF NOT EXISTS notify_dlq (
    dlq_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    session_id TEXT,
    severity TEXT NOT NULL,
    channel TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    last_error TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    terminal INTEGER NOT NULL
)
"""

_DLQ_INDEX_TS = (
    "CREATE INDEX IF NOT EXISTS idx_notify_dlq_ts ON notify_dlq(ts DESC)"
)


class NotifyDLQStore:
    """Persistent deadletter queue for failed notifications.

    Stores payloads that exhausted all retries (``terminal=True``) or
    hit a permanent error (``terminal=False``) so an operator can
    inspect / replay them. The store lives in the existing
    ``agent-jobs.db`` SQLite file (sibling of ``harness.db``), sharing
    its WAL / connection lifecycle.

    All public methods are async (the dispatcher is async). The store
    is **not** thread-safe by itself — aiosqlite serialises access per
    connection, and concurrent writers use WAL + ``busy_timeout``.

    Parameters
    ----------
    db_path:
        Path to the SQLite file. Defaults to
        ``settings.db_path.parent / "agent-jobs.db"`` (same file as
        ``ScratchpadStore`` / ``CompactStore`` / ``JobStore``).
    """

    def __init__(self, db_path: Any) -> None:
        self._db_path = db_path
        self._initialized = False

    async def init(self) -> None:
        """Create the ``notify_dlq`` table + index if missing. Idempotent."""
        if self._initialized:
            return
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA busy_timeout=5000")
            await db.execute("PRAGMA synchronous=NORMAL")
            await db.execute(_DLQ_SCHEMA)
            await db.execute(_DLQ_INDEX_TS)
            await db.commit()
        self._initialized = True
        logger.info("NotifyDLQStore ready at %s", self._db_path)

    async def record_failure(
        self,
        *,
        session_id: str,
        severity: str,
        channel: str,
        payload: dict[str, Any],
        last_error: str,
        attempts: int,
        terminal: bool,
    ) -> int:
        """Persist a failed notification. Returns the assigned ``dlq_id``.

        ``terminal=True`` means the payload exhausted all retries;
        ``terminal=False`` means a permanent error short-circuited to
        the DLQ without any retry.
        """
        if not self._initialized:
            await self.init()
        payload_json = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        async with aiosqlite.connect(self._db_path) as db:
            cur = await db.execute(
                "INSERT INTO notify_dlq "
                "(ts, session_id, severity, channel, payload_json, "
                " last_error, attempts, terminal) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    time.time(),
                    session_id or "",
                    severity,
                    channel,
                    payload_json,
                    last_error,
                    int(attempts),
                    1 if terminal else 0,
                ),
            )
            await db.commit()
            dlq_id = int(cur.lastrowid) if cur.lastrowid is not None else 0
        return dlq_id

    async def query_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent ``limit`` DLQ entries (newest first).

        Each entry is a dict mirroring the table columns. ``terminal``
        is converted back to a bool for caller convenience.
        """
        if not self._initialized:
            await self.init()
        if limit < 1:
            limit = 1
        if limit > 1000:
            limit = 1000
        out: list[dict[str, Any]] = []
        async with aiosqlite.connect(self._db_path) as db:
            db.row_factory = sqlite3.Row
            async with db.execute(
                "SELECT * FROM notify_dlq ORDER BY ts DESC LIMIT ?",
                (int(limit),),
            ) as cur:
                rows = await cur.fetchall()
        for row in rows:
            d = dict(row)
            d["terminal"] = bool(d.get("terminal", 0))
            out.append(d)
        return out


# === Dispatcher with per-channel retry + DLQ ===


async def _dispatch_to_channel(
    channel: str,
    payload: dict[str, Any],
    settings: Any,
    *,
    dlq_store: NotifyDLQStore | None,
    session_id: str = "",
    sleep: Any = asyncio.sleep,
) -> bool:
    """Dispatch a notification to one channel with retry + DLQ.

    Returns ``True`` on success, ``False`` if the payload was moved to
    the DLQ (or dropped when DLQ is disabled).

    Parameters
    ----------
    channel:
        Channel name (must be a key in ``_DELIVERERS``).
    payload:
        Notification payload dict.
    settings:
        Settings-like object with the retry/DLQ attributes.
    dlq_store:
        DLQ store instance, or ``None`` to disable persistence (the
        observability counter is still emitted).
    session_id:
        Session id to record in the DLQ entry.
    sleep:
        Async sleep callable (overridable for tests). Defaults to
        :func:`asyncio.sleep`.
    """
    deliverer = _DELIVERERS.get(channel)
    if deliverer is None:
        logger.debug("NotifyTerminal: unknown channel %r (skipped)", channel)
        return True

    severity = str(payload.get("severity", "info"))
    max_retries = int(getattr(settings, "hooks_notify_max_retries", 3))
    initial_delay_ms = int(getattr(settings, "hooks_notify_retry_initial_delay_ms", 100))
    max_delay_ms = int(getattr(settings, "hooks_notify_retry_max_delay_ms", 5000))
    dlq_enabled = bool(getattr(settings, "hooks_notify_dlq_enabled", True))

    attempt = 0
    delay_ms = initial_delay_ms if initial_delay_ms > 0 else 0
    last_error_str = ""
    terminal = False

    while True:
        attempt += 1
        try:
            await deliverer(payload, settings)
            return True
        except ChannelError as e:
            last_error_str = str(e)
            terminal = False
            if e.category == "permanent":
                # Permanent error: no retry, straight to DLQ.
                logger.warning(
                    "NotifyTerminal: channel %r permanent failure (attempt %d): %s",
                    channel, attempt, e,
                )
                break
            # Transient error: retry if budget allows.
            if attempt > max_retries:
                terminal = True
                logger.warning(
                    "NotifyTerminal: channel %r exhausted %d retries: %s",
                    channel, max_retries, e,
                )
                break
            # Sleep before next attempt. delay_ms capped at max_delay_ms.
            sleep_ms = min(delay_ms, max_delay_ms) if delay_ms > 0 else 0
            if sleep_ms > 0:
                await sleep(sleep_ms / 1000.0)
            # Exponential backoff: double the delay for the next round.
            delay_ms = delay_ms * 2 if delay_ms > 0 else 0
        except Exception as e:  # noqa: BLE001 — defensive catch-all
            # A deliverer raised a non-ChannelError (shouldn't happen —
            # _deliver_* wrap everything). Classify conservatively as
            # transient and retry if budget allows.
            synthetic = _classify_exception(e, channel)
            last_error_str = str(synthetic)
            terminal = False
            if synthetic.category == "permanent" or attempt > max_retries:
                terminal = attempt > max_retries
                logger.warning(
                    "NotifyTerminal: channel %r unclassified failure after %d "
                    "attempt(s): %s",
                    channel, attempt, e,
                )
                break
            sleep_ms = min(delay_ms, max_delay_ms) if delay_ms > 0 else 0
            if sleep_ms > 0:
                await sleep(sleep_ms / 1000.0)
            delay_ms = delay_ms * 2 if delay_ms > 0 else 0

    # Exhausted retries / permanent error → DLQ.
    emit_notify_dlq(
        severity=severity,
        channel=channel,
        terminal=terminal,
        attempts=attempt,
        last_error=last_error_str,
    )
    if dlq_enabled and dlq_store is not None:
        try:
            await dlq_store.record_failure(
                session_id=session_id,
                severity=severity,
                channel=channel,
                payload=payload,
                last_error=last_error_str,
                attempts=attempt,
                terminal=terminal,
            )
        except Exception as e:  # noqa: BLE001 — fail-open DLQ
            logger.warning(
                "NotifyTerminal: DLQ persist failed for channel %r (%s): %s",
                channel, type(e).__name__, e,
            )
    return False


# === Public hook entry point ===

async def notify_terminal_hook(context: HookContext) -> HookDecision:
    """Forward Notification events to configured channels.

    Phase 4.3+ v1.11.0: dispatches to ``stdout`` / ``webhook`` /
    ``desktop`` channels.

    Phase 4.6 v1.16.0: added ``slack`` and ``teams`` channels.

    Phase 4.8 v1.18.0: per-channel retry with exponential backoff +
    SQLite deadletter queue. Channels are dispatched concurrently via
    ``asyncio.gather`` — a slow / failing channel does NOT block
    another. Failed notifications are persisted to ``notify_dlq``
    (in ``agent-jobs.db``) so an operator can inspect / replay them.
    """
    if context.event != "Notification":
        return HookDecision(decision="allow", hook_id="builtin.notify_terminal")
    payload = context.payload
    message = payload.get("message", "")
    if not message:
        logger.debug("NotifyTerminal: empty message (skipped)")
        return HookDecision(decision="allow", hook_id="builtin.notify_terminal")

    channels: list[Any] = payload.get("channels") or ["stdout"]
    settings = Settings()

    # Resolve DLQ store once and reuse across channels.
    dlq_store: NotifyDLQStore | None = None
    if getattr(settings, "hooks_notify_dlq_enabled", True):
        dlq_store = NotifyDLQStore(settings.db_path.parent / "agent-jobs.db")
        try:
            await dlq_store.init()
        except Exception as e:  # noqa: BLE001 — fail-open DLQ
            logger.warning(
                "NotifyTerminal: DLQ init failed (%s): %s",
                type(e).__name__, e,
            )
            dlq_store = None

    # Dispatch every channel concurrently — per-channel isolation.
    # ``return_exceptions=True`` ensures one channel's failure does not
    # cancel the gather (the dispatcher already records failures to DLQ).
    coros = [
        _dispatch_to_channel(
            str(ch),
            payload,
            settings,
            dlq_store=dlq_store,
            session_id=context.session_id,
        )
        for ch in channels
        if _DELIVERERS.get(ch) is not None
    ]
    if coros:
        await asyncio.gather(*coros, return_exceptions=True)

    # Emit per-channel dispatch counters (fire-and-forget metric; the
    # dispatcher already emitted DLQ counters above on failures).
    severity = str(payload.get("severity", "info"))
    for ch in channels:
        if _DELIVERERS.get(ch) is None:
            continue
        emit_notification_dispatched(
            severity=severity,
            channel=str(ch),
            message=str(message),
            hook_name="builtin.notify_terminal",
        )

    return HookDecision(decision="allow", hook_id="builtin.notify_terminal")
