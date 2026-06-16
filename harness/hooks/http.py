"""Phase 4.0: HTTP transport for hooks.

An HTTP hook is a remote endpoint that:
    1. Receives a JSON ``HookContext`` via POST.
    2. Performs its work.
    3. Returns a JSON ``HookDecision`` in the response body.

We use ``urllib.request`` (stdlib) inside ``asyncio.to_thread`` to
avoid blocking the event loop, wrapped in ``asyncio.wait_for`` for
the hard timeout (Plan B4).

Auth headers are passed verbatim (e.g. ``Authorization: Bearer abc``).

Trust boundary: stdlib only (``urllib``, ``asyncio``, ``json``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

from harness.hooks.context import HookContext, HookDecision


logger = logging.getLogger(__name__)


async def invoke_http_hook(
    url: str,
    context: HookContext,
    *,
    timeout_ms: int,
    headers: dict[str, str] | None = None,
    method: str = "POST",
) -> HookDecision:
    """Run an HTTP hook and return its decision.

    Args:
        url: Full URL of the hook endpoint.
        context: The context to send as JSON body.
        timeout_ms: Hard timeout (connect + read).
        headers: Optional HTTP headers (e.g. ``Authorization``).
        method: HTTP method (default ``POST``).

    Returns:
        ``HookDecision`` with decision, ``hook_id=http.<url>``, and
        ``error`` populated on any failure. Fail-open: timeouts and
        4xx/5xx responses return ``allow``.
    """
    hook_id = f"http.{url}"
    start = time.monotonic()
    payload = {
        "event": context.event,
        "session_id": context.session_id,
        "agent_id": context.agent_id,
        "payload": context.payload,
        "ts": context.ts,
        "request_id": context.request_id,
        "recursion_depth": context.recursion_depth,
        "event_stack": list(context.event_stack),
    }
    body = json.dumps(payload).encode("utf-8")
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)

    def _do_request() -> tuple[int, str]:
        req = urllib.request.Request(
            url, data=body, headers=req_headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_ms / 1000.0) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", errors="replace") if e.fp else ""

    try:
        status_code, response_text = await asyncio.wait_for(
            asyncio.to_thread(_do_request),
            timeout=timeout_ms / 1000.0,
        )
    except asyncio.TimeoutError:
        duration_ms = (time.monotonic() - start) * 1000.0
        logger.warning("HTTP hook %s timed out after %dms", url, timeout_ms)
        return HookDecision(
            decision="allow",
            hook_id=hook_id,
            duration_ms=duration_ms,
            error=f"HTTP timeout after {timeout_ms}ms",
        )
    except Exception as e:  # noqa: BLE001
        duration_ms = (time.monotonic() - start) * 1000.0
        logger.warning(
            "HTTP hook %s raised %s: %s", url, type(e).__name__, e
        )
        return HookDecision(
            decision="allow",
            hook_id=hook_id,
            duration_ms=duration_ms,
            error=f"{type(e).__name__}: {e}",
        )

    duration_ms = (time.monotonic() - start) * 1000.0
    if status_code >= 400:
        # 4xx / 5xx: fail-open with error message.
        return HookDecision(
            decision="allow",
            hook_id=hook_id,
            duration_ms=duration_ms,
            error=f"HTTP {status_code}: {response_text[:200]}",
        )
    if not response_text:
        return HookDecision(
            decision="allow",
            hook_id=hook_id,
            duration_ms=duration_ms,
            error="empty response body",
        )
    try:
        data: dict[str, Any] = json.loads(response_text)
    except json.JSONDecodeError as e:
        return HookDecision(
            decision="allow",
            hook_id=hook_id,
            duration_ms=duration_ms,
            error=f"invalid JSON in response: {e}",
        )
    decision_str = data.get("decision", "allow")
    if decision_str not in ("allow", "block", "modify"):
        decision_str = "allow"
    return HookDecision(
        decision=decision_str,  # type: ignore[arg-type]
        hook_id=hook_id,
        duration_ms=duration_ms,
        output=dict(data.get("output", {})),
        error=str(data.get("error", "")),
    )


__all__ = ["invoke_http_hook"]
