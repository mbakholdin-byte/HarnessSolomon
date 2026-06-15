"""Phase 3 v1.5.0 Step 2: PrivacyZoneFilter.

Path-based privacy filter. Given a file path, returns the action to
take (``block`` / ``redact`` / ``skip`` / ``allow``) and the matched
pattern (or ``None`` if no match).

Design:
- Pure function (``check``) — no I/O, no side effects.
- Audit integration is OPTIONAL — caller passes ``audit`` which we
  call on every hit (best-effort, never raises).
- ``enabled=False`` short-circuits to ``("allow", None)`` so the
  filter is a no-op without code changes upstream.

Integration points (Step 3):
- ``ToolRuntime._read_file`` (block / redact / skip on read)
- ``ToolRuntime._grep`` and ``ToolRuntime._glob`` (block on search)
- ``scratchpad.write_note`` Tier 2 (audit-only, not content filter)

Why path-based (vs content-based):
- Content redaction (12 patterns) already lives in
  :mod:`harness.redaction` and is applied at the LLM context
  boundary (Phase 3 v1.0.0). Path zones add a *second* defence
  layer: "don't even read this file in the first place".
- Privacy zones are operator-configured (Settings) vs content
  redaction which is automatic (regex). Operators may want to
  extend their gitignore / .dockerignore mental model.

Out of scope (Tier 3, deferred to v1.6.0+):
- WebSocketChat broadcasts
- OutboundWebhookDispatcher payloads
- Embedder path metadata
"""
from __future__ import annotations

import logging
from typing import Any, Final, Literal

from harness.privacy.path_match import match_glob
from harness.privacy.zone_config import ZoneAction, ZoneRule

__all__ = ["PrivacyZoneFilter", "ZoneDecision"]

logger = logging.getLogger(__name__)

# Action returned by ``check()``. ``"allow"`` is the "no rule matched"
# case; the other three come from ``ZoneAction`` literal.
ZoneDecision = Literal["allow", "block", "redact", "skip"]


class PrivacyZoneFilter:
    """Path-based privacy filter with optional audit integration.

    Args:
        rules:   Ordered list of :class:`ZoneRule`. First match wins
                 (subsequent rules NOT evaluated for the same path).
        audit:   Optional audit sink. Called on every non-``allow``
                 decision with ``(event="privacy_zone_blocked"|..., payload)``.
                 Must be defensive — PrivacyZoneFilter never raises on
                 audit errors.
        enabled: Master switch. ``False`` → :meth:`check` always
                 returns ``("allow", None)``.

    Example:
        >>> from harness.privacy.zone_config import parse_zones
        >>> rules = parse_zones("", "", "block")
        >>> f = PrivacyZoneFilter(rules)
        >>> f.check("private/.env")
        ('block', 'private/**')
        >>> f.check("src/main.py")
        ('allow', None)
    """

    def __init__(
        self,
        rules: list[ZoneRule],
        *,
        audit: Any | None = None,
        enabled: bool = True,
    ) -> None:
        self._rules = list(rules)  # copy to avoid caller mutation
        self._audit = audit
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def rules(self) -> list[ZoneRule]:
        """Read-only view of the configured rules (for tests / introspection)."""
        return list(self._rules)

    def check(self, path: str) -> tuple[ZoneDecision, str | None]:
        """Determine the action to take for a given file path.

        Iterates rules in order; first match wins. Returns
        ``("allow", None)`` if no rule matches (or filter is
        disabled). Emits an audit event on every non-``allow``
        decision (best-effort, never raises).

        Args:
            path: Repo-relative POSIX path. Examples:
                  ``"private/.env"``, ``"src/main.py"``,
                  ``"home/user/.ssh/id_rsa"``.

        Returns:
            Tuple of ``(action, matched_pattern)``:
            - action = ``"allow"`` (no match) or one of
              ``"block"``, ``"redact"``, ``"skip"`` (rule matched).
            - matched_pattern = the pattern that matched, or ``None``
              when action is ``"allow"``.
        """
        if not self._enabled:
            return ("allow", None)

        for rule in self._rules:
            if match_glob(path, rule.pattern):
                self._safe_audit(rule.action, path, rule.pattern)
                return (rule.action, rule.pattern)

        return ("allow", None)

    def should_exclude(self, path: str) -> bool:
        """Convenience: ``True`` if the path should be excluded entirely.

        "Exclude" = ``block`` (most common case). For ``redact`` and
        ``skip``, callers usually want to handle the result themselves
        (e.g. emit placeholder content), so this helper is a
        shortcut for the common "don't even read" case.
        """
        action, _ = self.check(path)
        return action == "block"

    def _safe_audit(
        self,
        action: ZoneAction,
        path: str,
        pattern: str,
    ) -> None:
        """Emit audit event, never raise (fail-open).

        Event name format: ``privacy_zone_{action}ed`` (past participle)
        for grammatical consistency. So actions ``block`` / ``redact`` /
        ``skip`` map to events ``privacy_zone_blocked`` / ``_redacted``
        / ``_skipped`` respectively. This matches the
        ``_occurred`` / ``_skipped`` convention used elsewhere in the
        harness (e.g. ``compact_failed``, ``reflection_extracted``).
        """
        if self._audit is None:
            return
        event_map: Final[dict[ZoneAction, str]] = {
            "block": "privacy_zone_blocked",
            "redact": "privacy_zone_redacted",
            "skip": "privacy_zone_skipped",
        }
        event = event_map[action]
        payload: dict[str, Any] = {
            "action": action,
            "path": path,
            "pattern": pattern,
        }
        try:
            # Mirror v1.3.1 / v1.4.0 ScratchpadAudit.record signature.
            # Accept either ``record(event, payload)`` or ``record(event=..., **kw)``.
            record = getattr(self._audit, "record", None)
            if record is None:
                return
            try:
                record(event, payload)
            except TypeError:
                # Some audit sinks take only event name, no payload.
                record(event=event, **payload)
        except Exception as exc:  # noqa: BLE001 — audit MUST fail-open
            logger.warning("PrivacyZoneFilter audit failed: %s", exc)
