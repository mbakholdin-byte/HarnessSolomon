"""Phase 4.6 v1.16.0: tests for ``harness hooks audit`` CLI subcommand.

Covers:
  - ``read_audit_log`` helper (parsing, filters, tail, malformed lines).
  - ``_cmd_hooks_audit`` CLI handler (no-audit-dir, --tail, --event,
    --decision, --session, --since, --json, pretty table).
  - Trust boundary preservation (cli_hooks.py does not import
    harness.agents / harness.server).

Strategy: invoke the handler / helper directly (no subprocess) to keep
tests fast. We synthesise NDJSON fixtures on disk so we do not depend
on the running server or on ``HookAuditSink.record`` semantics.
"""
from __future__ import annotations

import ast
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from harness.cli_hooks import _cmd_hooks_audit, read_audit_log


# === Fixtures ============================================================


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
) -> dict:
    """Build a single audit-log entry matching HookAuditSink.record format."""
    if decisions is None:
        decisions = [
            {
                "decision": final_decision,
                "hook_id": "builtin.log",
                "duration_ms": 0.5,
                "output": {},
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
            "final_payload": {},
            "decisions": decisions,
        },
    }


def _write_ndjson(path: Path, entries: list[dict]) -> Path:
    """Write entries as NDJSON (one JSON object per line)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    return path


@pytest.fixture
def audit_dir(tmp_path: Path) -> Path:
    """Create and return the ``<tmp>/data/audit`` directory."""
    d = tmp_path / "data" / "audit"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _audit_file(audit_dir: Path, when: datetime | None = None) -> Path:
    """Resolve today's (or given) audit file path under audit_dir."""
    when = when or datetime.now(timezone.utc)
    return audit_dir / f"hooks-{when.strftime('%Y-%m-%d')}.ndjson"


def _ns(
    *,
    tail: int = 50,
    event: str | None = None,
    decision: str | None = None,
    session: str | None = None,
    since: str | None = None,
    project_root: str | None = None,
    json_output: bool = False,
) -> argparse.Namespace:
    """Build an argparse.Namespace mirroring the audit parser."""
    return argparse.Namespace(
        tail=tail,
        event=event,
        decision=decision,
        session=session,
        since=since,
        project_root=project_root,
        json=json_output,
    )


# === read_audit_log: parsing & edge cases ===============================


def test_read_audit_log_missing_file_returns_empty(tmp_path: Path) -> None:
    """A non-existent file yields an empty list (no exception)."""
    missing = tmp_path / "does-not-exist.ndjson"
    assert read_audit_log(missing) == []


def test_read_audit_log_skips_malformed_lines(audit_dir: Path) -> None:
    """Malformed JSON lines are skipped; valid lines are kept."""
    path = _audit_file(audit_dir)
    path.write_text(
        json.dumps(_entry(session_id="ok")) + "\n"
        + "this is not json\n"
        + "\n"  # blank line
        + json.dumps(_entry(session_id="ok2")) + "\n",
        encoding="utf-8",
    )
    result = read_audit_log(path)
    assert len(result) == 2
    assert {r["session_id"] for r in result} == {"ok", "ok2"}


def test_read_audit_log_tail_default(audit_dir: Path) -> None:
    """``tail=50`` keeps the last 50 entries when more are present."""
    entries = [_entry(session_id=f"s{i}") for i in range(80)]
    path = _write_ndjson(_audit_file(audit_dir), entries)
    result = read_audit_log(path, tail=50)
    assert len(result) == 50
    # Last 50 = s30..s79.
    assert result[0]["session_id"] == "s30"
    assert result[-1]["session_id"] == "s79"


def test_read_audit_log_tail_zero_returns_all(audit_dir: Path) -> None:
    """``tail=0`` returns every entry (no truncation)."""
    entries = [_entry(session_id=f"s{i}") for i in range(5)]
    path = _write_ndjson(_audit_file(audit_dir), entries)
    result = read_audit_log(path, tail=0)
    assert len(result) == 5


# === read_audit_log: filters ============================================


def test_read_audit_log_filter_by_event(audit_dir: Path) -> None:
    entries = [
        _entry(event="PreToolUse", session_id="a"),
        _entry(event="PostToolUse", session_id="b"),
        _entry(event="PreToolUse", session_id="c"),
    ]
    path = _write_ndjson(_audit_file(audit_dir), entries)
    result = read_audit_log(path, event="PreToolUse", tail=0)
    assert {r["session_id"] for r in result} == {"a", "c"}


def test_read_audit_log_filter_by_decision(audit_dir: Path) -> None:
    entries = [
        _entry(session_id="a", final_decision="allow"),
        _entry(
            session_id="b", final_decision="block",
            blocked_by="builtin.rm-guard",
            decisions=[
                {"decision": "block", "hook_id": "builtin.rm-guard",
                 "duration_ms": 0.2, "output": {}, "error": ""},
            ],
        ),
        _entry(session_id="c", final_decision="allow"),
    ]
    path = _write_ndjson(_audit_file(audit_dir), entries)
    result = read_audit_log(path, decision="block", tail=0)
    assert len(result) == 1
    assert result[0]["session_id"] == "b"


def test_read_audit_log_filter_by_session(audit_dir: Path) -> None:
    entries = [
        _entry(session_id="s1"),
        _entry(session_id="s2"),
        _entry(session_id="s1"),
    ]
    path = _write_ndjson(_audit_file(audit_dir), entries)
    result = read_audit_log(path, session="s1", tail=0)
    assert len(result) == 2
    assert all(r["session_id"] == "s1" for r in result)


def test_read_audit_log_filter_by_since(audit_dir: Path) -> None:
    """``--since`` keeps entries with ts >= since."""
    base = datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc)
    entries = [
        _entry(ts=(base - timedelta(minutes=10)).isoformat(), session_id="old"),
        _entry(ts=base.isoformat(), session_id="now"),
        _entry(ts=(base + timedelta(minutes=10)).isoformat(), session_id="new"),
    ]
    path = _write_ndjson(_audit_file(audit_dir), entries)
    # Boundary: ts >= base should include "now" and "new".
    result = read_audit_log(
        path, since=base.isoformat(), tail=0,
    )
    assert {r["session_id"] for r in result} == {"now", "new"}


def test_read_audit_log_since_with_z_suffix(audit_dir: Path) -> None:
    """A trailing ``Z`` in --since is accepted."""
    base = datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc)
    entries = [
        _entry(ts=(base - timedelta(minutes=5)).isoformat(), session_id="old"),
        _entry(ts=base.isoformat(), session_id="keep"),
    ]
    path = _write_ndjson(_audit_file(audit_dir), entries)
    result = read_audit_log(path, since="2026-06-17T12:00:00Z", tail=0)
    assert [r["session_id"] for r in result] == ["keep"]


# === _cmd_hooks_audit: CLI handler ======================================


def test_cmd_audit_no_audit_dir_prints_no_audit_log(
    capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """When the audit directory does not exist, print ``(no audit log)``."""
    rc = _cmd_hooks_audit(_ns(project_root=str(tmp_path)))
    out, err = capsys.readouterr()
    assert rc == 0, f"expected exit 0, got {rc}; stderr={err}"
    assert "(no audit log)" in out


def test_cmd_audit_no_file_today_prints_no_audit_log(
    capsys: pytest.CaptureFixture, audit_dir: Path,
) -> None:
    """Audit dir exists but today's file is missing → same message."""
    rc = _cmd_hooks_audit(_ns(project_root=str(audit_dir.parent.parent)))
    out, err = capsys.readouterr()
    assert rc == 0
    assert "(no audit log)" in out


def test_cmd_audit_tail_default(
    capsys: pytest.CaptureFixture, audit_dir: Path,
) -> None:
    """Default --tail 50 returns the last 50 entries."""
    entries = [_entry(session_id=f"s{i:03d}") for i in range(80)]
    _write_ndjson(_audit_file(audit_dir), entries)
    rc = _cmd_hooks_audit(_ns(project_root=str(audit_dir.parent.parent)))
    out, err = capsys.readouterr()
    assert rc == 0, f"stderr={err}"
    # The table header + 50 data rows.
    assert "s030" in out  # first of the last 50
    assert "s079" in out  # last
    assert "s029" not in out  # truncated


def test_cmd_audit_tail_custom_n(
    capsys: pytest.CaptureFixture, audit_dir: Path,
) -> None:
    entries = [_entry(session_id=f"s{i}") for i in range(20)]
    _write_ndjson(_audit_file(audit_dir), entries)
    rc = _cmd_hooks_audit(
        _ns(tail=3, project_root=str(audit_dir.parent.parent)),
    )
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "s17" in out
    assert "s19" in out
    assert "s16" not in out


def test_cmd_audit_filter_by_event(
    capsys: pytest.CaptureFixture, audit_dir: Path,
) -> None:
    entries = [
        _entry(event="PreToolUse", session_id="sess-alpha"),
        _entry(event="PostToolUse", session_id="sess-beta"),
        _entry(event="PreToolUse", session_id="sess-gamma"),
    ]
    _write_ndjson(_audit_file(audit_dir), entries)
    rc = _cmd_hooks_audit(
        _ns(event="PreToolUse", project_root=str(audit_dir.parent.parent)),
    )
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "sess-alpha" in out and "sess-gamma" in out
    assert "sess-beta" not in out


def test_cmd_audit_filter_by_decision(
    capsys: pytest.CaptureFixture, audit_dir: Path,
) -> None:
    entries = [
        _entry(session_id="allow1", final_decision="allow"),
        _entry(session_id="block1", final_decision="block"),
    ]
    _write_ndjson(_audit_file(audit_dir), entries)
    rc = _cmd_hooks_audit(
        _ns(decision="block", project_root=str(audit_dir.parent.parent)),
    )
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "block1" in out
    assert "allow1" not in out


def test_cmd_audit_invalid_decision_exits_2(
    capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """An invalid --decision value (bypassing argparse choices) exits 2."""
    rc = _cmd_hooks_audit(
        _ns(decision="bogus", project_root=str(tmp_path)),
    )
    out, err = capsys.readouterr()
    assert rc == 2
    assert "allow|block|modify" in err


def test_cmd_audit_filter_by_session(
    capsys: pytest.CaptureFixture, audit_dir: Path,
) -> None:
    entries = [
        _entry(session_id="alpha"),
        _entry(session_id="beta"),
        _entry(session_id="alpha"),
    ]
    _write_ndjson(_audit_file(audit_dir), entries)
    rc = _cmd_hooks_audit(
        _ns(session="alpha", project_root=str(audit_dir.parent.parent)),
    )
    out, _ = capsys.readouterr()
    assert rc == 0
    # Two alpha rows.
    assert out.count("alpha") >= 2
    assert "beta" not in out


def test_cmd_audit_since_filter(
    capsys: pytest.CaptureFixture, audit_dir: Path,
) -> None:
    base = datetime(2026, 6, 17, 12, 0, 0, tzinfo=timezone.utc)
    entries = [
        _entry(
            ts=(base - timedelta(minutes=5)).isoformat(),
            session_id="before",
        ),
        _entry(ts=base.isoformat(), session_id="at"),
        _entry(
            ts=(base + timedelta(minutes=5)).isoformat(),
            session_id="after",
        ),
    ]
    _write_ndjson(_audit_file(audit_dir), entries)
    rc = _cmd_hooks_audit(
        _ns(since=base.isoformat(), project_root=str(audit_dir.parent.parent)),
    )
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "at" in out and "after" in out
    assert "before" not in out


def test_cmd_audit_json_output(
    capsys: pytest.CaptureFixture, audit_dir: Path,
) -> None:
    """``--json`` emits a JSON object with entries/count/file."""
    entries = [
        _entry(session_id="x", final_decision="allow"),
        _entry(session_id="y", final_decision="block"),
    ]
    _write_ndjson(_audit_file(audit_dir), entries)
    rc = _cmd_hooks_audit(
        _ns(json_output=True, project_root=str(audit_dir.parent.parent)),
    )
    out, _ = capsys.readouterr()
    assert rc == 0
    payload = json.loads(out)
    assert payload["count"] == 2
    assert isinstance(payload["entries"], list)
    assert len(payload["entries"]) == 2
    assert "file" in payload
    assert payload["file"].endswith(".ndjson")


def test_cmd_audit_json_output_no_file(
    capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """``--json`` with no audit file emits an empty entries array."""
    rc = _cmd_hooks_audit(
        _ns(json_output=True, project_root=str(tmp_path)),
    )
    out, _ = capsys.readouterr()
    assert rc == 0
    payload = json.loads(out)
    assert payload["count"] == 0
    assert payload["entries"] == []


def test_cmd_audit_pretty_table_header(
    capsys: pytest.CaptureFixture, audit_dir: Path,
) -> None:
    """The pretty table prints the documented columns."""
    entries = [
        _entry(
            ts="2026-06-17T12:00:00+00:00",
            event="PreToolUse",
            session_id="sess-A",
            final_decision="block",
            blocked_by="builtin.rm-guard",
            decisions=[
                {"decision": "block", "hook_id": "builtin.rm-guard",
                 "duration_ms": 1.2, "output": {"reason": "destructive"},
                 "error": ""},
            ],
        ),
    ]
    _write_ndjson(_audit_file(audit_dir), entries)
    rc = _cmd_hooks_audit(_ns(project_root=str(audit_dir.parent.parent)))
    out, _ = capsys.readouterr()
    assert rc == 0
    # Title row mentions the file.
    assert "Hook audit log (" in out
    # Column headers.
    for col in ("timestamp", "event", "session", "hook_id", "decision", "duration_ms"):
        assert col in out
    # Data: blocked_by hook_id surfaces (preferred over first decision).
    assert "builtin.rm-guard" in out
    assert "block" in out
    # Duration is the sum of per-hook durations.
    assert "1.2" in out


def test_cmd_audit_pretty_table_no_entries(
    capsys: pytest.CaptureFixture, audit_dir: Path,
) -> None:
    """An existing-but-empty audit file prints ``no entries``."""
    _write_ndjson(_audit_file(audit_dir), [])
    rc = _cmd_hooks_audit(_ns(project_root=str(audit_dir.parent.parent)))
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "(no entries)" in out


def test_cmd_audit_invalid_project_root_exits_2(
    capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """A non-existent project_root exits 2."""
    bogus = tmp_path / "does-not-exist"
    rc = _cmd_hooks_audit(_ns(project_root=str(bogus)))
    out, err = capsys.readouterr()
    assert rc == 2
    assert "is not a directory" in err


# === Trust boundary preservation ========================================


_CLI_HOOKS_PATH = (
    Path(__file__).resolve().parent.parent / "harness" / "cli_hooks.py"
)
_FORBIDDEN_PREFIXES: tuple[str, ...] = ("harness.agents", "harness.server")


def test_trust_boundary_cli_hooks_no_forbidden_imports() -> None:
    """AST-scan cli_hooks.py: it must not import harness.agents/server.

    This mirrors ``tests/test_hooks_trust_boundary.py`` but targets
    the CLI module (which is allowed to import from harness.hooks.*
    and harness.config, but must stay decoupled from the server /
    agents runtime so the CLI can run in a minimal environment).
    """
    assert _CLI_HOOKS_PATH.is_file(), f"cli_hooks.py missing at {_CLI_HOOKS_PATH}"
    source = _CLI_HOOKS_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_CLI_HOOKS_PATH))

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _check_module(alias.name, node.lineno, violations)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue  # relative import — cannot reach forbidden prefixes
            if node.module:
                _check_module(node.module, node.lineno, violations)

    assert not violations, (
        "Trust boundary violations in harness/cli_hooks.py:\n  "
        + "\n  ".join(violations)
    )


def _check_module(module: str, lineno: int, violations: list[str]) -> None:
    for prefix in _FORBIDDEN_PREFIXES:
        if module == prefix or module.startswith(prefix + "."):
            violations.append(
                f"harness/cli_hooks.py:{lineno}: forbidden import "
                f"{module!r} (prefix {prefix!r} not allowed)"
            )
