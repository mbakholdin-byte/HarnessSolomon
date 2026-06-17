"""Phase 4.7 v1.17.0: tests for ``harness hooks audit --filter REGEX``.

Covers the snapshot-mode ``--filter`` flag (the follow-mode path is
tested separately in ``test_cli_follow.py``):

  - ``read_audit_log(filter_pattern=...)`` — regex matching on the
    JSON-serialised entry, AND-combined with structured filters.
  - ``_cmd_hooks_audit`` — ``--filter`` plumbing, invalid-regex
    exit 1, malformed-line skipping with a warning.
  - Trust boundary preservation (cli_hooks.py still does not import
    harness.agents / harness.server — re-checked in
    ``test_cli_hooks_audit.py``; we don't duplicate it here).

Strategy: invoke the handler / helper directly (no subprocess) with
synthesised NDJSON fixtures on disk.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness.cli_hooks import _cmd_hooks_audit, read_audit_log


# === Helpers =============================================================


def _entry(
    *,
    ts: str = "2026-06-17T12:00:00+00:00",
    event: str = "PreToolUse",
    session_id: str = "sess-1",
    agent_id: str = "agent-1",
    request_id: str = "req-1",
    final_decision: str = "allow",
    blocked_by: str = "",
    decisions: list[dict] | None = None,
    payload: dict | None = None,
) -> dict:
    """Build a single audit-log entry matching HookAuditSink.record format."""
    if decisions is None:
        decisions = [
            {
                "decision": final_decision,
                "hook_id": "builtin.log",
                "duration_ms": 0.5,
                "output": payload or {},
                "error": "",
            }
        ]
    return {
        "ts": ts,
        "event": event,
        "session_id": session_id,
        "agent_id": agent_id,
        "request_id": request_id,
        "aggregate": {
            "final_decision": final_decision,
            "blocked_by": blocked_by,
            "final_payload": payload or {},
            "decisions": decisions,
        },
    }


def _write_ndjson(path: Path, entries: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return path


@pytest.fixture
def audit_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data" / "audit"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _audit_file(audit_dir: Path, when: datetime | None = None) -> Path:
    when = when or datetime.now(timezone.utc)
    return audit_dir / f"hooks-{when.strftime('%Y-%m-%d')}.ndjson"


def _ns(
    *,
    tail: int = 50,
    event: str | None = None,
    decision: str | None = None,
    session: str | None = None,
    since: str | None = None,
    filter_regex: str | None = None,
    project_root: str | None = None,
    json_output: bool = False,
    follow: bool = False,
    max_bytes: int = 0,
) -> argparse.Namespace:
    """Build an argparse.Namespace mirroring the audit parser."""
    return argparse.Namespace(
        tail=tail,
        event=event,
        decision=decision,
        session=session,
        since=since,
        filter=filter_regex,
        project_root=project_root,
        json=json_output,
        follow=follow,
        max_bytes=max_bytes,
    )


# === read_audit_log: filter_pattern ======================================


def test_read_audit_log_filter_matches_substring(audit_dir: Path) -> None:
    """filter_pattern="confirm_dangerous" keeps entries mentioning it."""
    entries = [
        _entry(
            session_id="dangerous",
            decisions=[
                {
                    "decision": "block",
                    "hook_id": "builtin.confirm_dangerous",
                    "duration_ms": 1.0,
                    "output": {"reason": "destructive"},
                    "error": "",
                },
            ],
            final_decision="block",
            blocked_by="builtin.confirm_dangerous",
        ),
        _entry(session_id="safe"),
    ]
    path = _write_ndjson(_audit_file(audit_dir), entries)
    result = read_audit_log(
        path, filter_pattern="confirm_dangerous", tail=0,
    )
    assert len(result) == 1
    assert result[0]["session_id"] == "dangerous"


def test_read_audit_log_filter_no_match_returns_empty(audit_dir: Path) -> None:
    """A regex that matches nothing → empty list."""
    entries = [_entry(session_id="a"), _entry(session_id="b")]
    path = _write_ndjson(_audit_file(audit_dir), entries)
    result = read_audit_log(
        path, filter_pattern="zzz_nonexistent_zzz", tail=0,
    )
    assert result == []


def test_read_audit_log_filter_combined_with_event(audit_dir: Path) -> None:
    """--event + --filter → AND semantics."""
    entries = [
        _entry(event="PreToolUse", session_id="match", payload={"tool_name": "bash"}),
        _entry(
            event="PreToolUse", session_id="no-match",
            payload={"tool_name": "read_file"},
        ),
        _entry(
            event="PostToolUse", session_id="wrong-event",
            payload={"tool_name": "bash"},
        ),
    ]
    path = _write_ndjson(_audit_file(audit_dir), entries)
    # Only the entry that is BOTH PreToolUse AND mentions "bash" survives.
    result = read_audit_log(
        path, event="PreToolUse", filter_pattern="bash", tail=0,
    )
    assert len(result) == 1
    assert result[0]["session_id"] == "match"


def test_read_audit_log_filter_combined_with_decision(audit_dir: Path) -> None:
    """--decision + --filter → AND semantics."""
    entries = [
        _entry(
            session_id="blocked-rm",
            final_decision="block",
            blocked_by="builtin.rm-guard",
            decisions=[
                {"decision": "block", "hook_id": "builtin.rm-guard",
                 "duration_ms": 0.1, "output": {}, "error": ""},
            ],
        ),
        _entry(
            session_id="blocked-other",
            final_decision="block",
            blocked_by="builtin.confirm_dangerous",
            decisions=[
                {"decision": "block", "hook_id": "builtin.confirm_dangerous",
                 "duration_ms": 0.1, "output": {}, "error": ""},
            ],
        ),
    ]
    path = _write_ndjson(_audit_file(audit_dir), entries)
    result = read_audit_log(
        path, decision="block", filter_pattern="rm-guard", tail=0,
    )
    assert len(result) == 1
    assert result[0]["session_id"] == "blocked-rm"


def test_read_audit_log_filter_regex_anchors(audit_dir: Path) -> None:
    """Regex metacharacters (e.g. ``^``) work as expected."""
    entries = [
        _entry(session_id="alpha"),
        _entry(session_id="beta-alpha"),
    ]
    path = _write_ndjson(_audit_file(audit_dir), entries)
    # ``session_id":"alpha`` matches only the first (exact key), not
    # the second (``beta-alpha``).
    result = read_audit_log(
        path,
        filter_pattern=r'"session_id":\s*"alpha"',
        tail=0,
    )
    assert len(result) == 1
    assert result[0]["session_id"] == "alpha"


def test_read_audit_log_invalid_regex_raises(audit_dir: Path) -> None:
    """An invalid regex propagates as ``re.error`` from read_audit_log.

    The CLI handler catches this and maps it to exit 1; the helper
    itself lets the exception bubble up so callers can decide.
    """
    import re as _re

    entries = [_entry(session_id="a")]
    path = _write_ndjson(_audit_file(audit_dir), entries)
    with pytest.raises(_re.error):
        read_audit_log(path, filter_pattern="(unclosed", tail=0)


def test_read_audit_log_filter_skips_malformed_lines(
    audit_dir: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Malformed JSON lines are skipped; the filter still applies to valid ones."""
    path = _audit_file(audit_dir)
    path.write_text(
        json.dumps(_entry(session_id="keep", payload={"cmd": "rm -rf"})) + "\n"
        + "this is not json\n"
        + json.dumps(_entry(session_id="drop", payload={"cmd": "ls"})) + "\n",
        encoding="utf-8",
    )
    result = read_audit_log(path, filter_pattern="rm -rf", tail=0)
    assert len(result) == 1
    assert result[0]["session_id"] == "keep"


# === _cmd_hooks_audit: --filter plumbing =================================


def test_cmd_audit_filter_match(
    capsys: pytest.CaptureFixture, audit_dir: Path,
) -> None:
    """``--filter confirm_dangerous`` surfaces matching entries."""
    entries = [
        _entry(
            session_id="hit",
            final_decision="block",
            blocked_by="builtin.confirm_dangerous",
            decisions=[
                {"decision": "block", "hook_id": "builtin.confirm_dangerous",
                 "duration_ms": 0.3, "output": {}, "error": ""},
            ],
        ),
        _entry(session_id="miss"),
    ]
    _write_ndjson(_audit_file(audit_dir), entries)
    rc = _cmd_hooks_audit(
        _ns(
            filter_regex="confirm_dangerous",
            project_root=str(audit_dir.parent.parent),
        ),
    )
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "hit" in out
    assert "miss" not in out


def test_cmd_audit_filter_combined_with_event(
    capsys: pytest.CaptureFixture, audit_dir: Path,
) -> None:
    """``--event PreToolUse --filter "rm -rf"`` → AND logic."""
    entries = [
        _entry(
            event="PreToolUse", session_id="match",
            payload={"tool_name": "bash", "cmd": "rm -rf /tmp"},
        ),
        _entry(
            event="PreToolUse", session_id="other",
            payload={"tool_name": "bash", "cmd": "ls"},
        ),
        _entry(
            event="PostToolUse", session_id="wrong-event",
            payload={"tool_name": "bash", "cmd": "rm -rf /tmp"},
        ),
    ]
    _write_ndjson(_audit_file(audit_dir), entries)
    rc = _cmd_hooks_audit(
        _ns(
            event="PreToolUse",
            filter_regex="rm -rf",
            project_root=str(audit_dir.parent.parent),
        ),
    )
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "match" in out
    assert "other" not in out
    assert "wrong-event" not in out


def test_cmd_audit_filter_no_match(
    capsys: pytest.CaptureFixture, audit_dir: Path,
) -> None:
    """A filter that matches nothing → empty table, exit 0."""
    entries = [_entry(session_id="a"), _entry(session_id="b")]
    _write_ndjson(_audit_file(audit_dir), entries)
    rc = _cmd_hooks_audit(
        _ns(
            filter_regex="nonexistent_pattern_xyz",
            project_root=str(audit_dir.parent.parent),
        ),
    )
    out, _ = capsys.readouterr()
    assert rc == 0
    # Pretty table prints "(no entries)" when the filtered list is empty.
    assert "(no entries)" in out


def test_cmd_audit_filter_invalid_regex_exits_1(
    capsys: pytest.CaptureFixture, audit_dir: Path,
) -> None:
    """An invalid regex exits 1 BEFORE reading the audit file."""
    # Even write a valid file to prove we don't reach it.
    _write_ndjson(_audit_file(audit_dir), [_entry(session_id="x")])
    rc = _cmd_hooks_audit(
        _ns(
            filter_regex="(unclosed[",
            project_root=str(audit_dir.parent.parent),
        ),
    )
    out, err = capsys.readouterr()
    assert rc == 1
    assert "invalid --filter regex" in err


def test_cmd_audit_filter_skips_malformed_lines(
    capsys: pytest.CaptureFixture, audit_dir: Path,
) -> None:
    """Malformed lines in the file are skipped; valid matching lines survive."""
    path = _audit_file(audit_dir)
    path.write_text(
        json.dumps(_entry(session_id="keep", payload={"cmd": "rm -rf"})) + "\n"
        + "this is not json\n"
        + json.dumps(_entry(session_id="drop", payload={"cmd": "ls"})) + "\n",
        encoding="utf-8",
    )
    rc = _cmd_hooks_audit(
        _ns(
            filter_regex="rm -rf",
            project_root=str(audit_dir.parent.parent),
        ),
    )
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "keep" in out
    assert "drop" not in out


def test_cmd_audit_filter_json_output(
    capsys: pytest.CaptureFixture, audit_dir: Path,
) -> None:
    """``--json`` + ``--filter`` → JSON with only matching entries."""
    entries = [
        _entry(session_id="hit", payload={"cmd": "rm -rf"}),
        _entry(session_id="miss", payload={"cmd": "ls"}),
    ]
    _write_ndjson(_audit_file(audit_dir), entries)
    rc = _cmd_hooks_audit(
        _ns(
            filter_regex="rm -rf",
            json_output=True,
            project_root=str(audit_dir.parent.parent),
        ),
    )
    out, _ = capsys.readouterr()
    assert rc == 0
    payload = json.loads(out)
    assert payload["count"] == 1
    assert payload["entries"][0]["session_id"] == "hit"
