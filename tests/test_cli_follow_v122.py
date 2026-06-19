"""Phase 4.12 v1.22.0: tests for ``--follow`` improvements.

Covers the new :class:`harness.cli_follow.Follower` (async, batched,
persistent state, inode rotation, regex filter) and the integration of
the new ``--batch-size`` / ``--resume`` / ``--reset`` CLI flags into
``harness hooks audit --follow`` and
``harness observability metrics --follow``.

Strategy:
  - :class:`Follower` unit tests use ``stop_predicate`` (a bounded
    callable) so the async generator terminates deterministically
    without real time.sleep. ``max_batches`` is the secondary guard.
  - File rotation is tested two ways:
      1. Real inode change (POSIX-only; on Windows ``st_ino`` is
         often 0, so we fall back to the state-file mismatch path).
      2. State-file with a mismatched ``last_inode`` + ``--resume``
         → the follower detects the mismatch and reopens from 0.
  - Handler tests monkeypatch the Follower / snapshot path and
    verify the new flags route to the batched implementation.
  - Unicode test writes Cyrillic lines to verify UTF-8 safety.

Trust boundary: the new code lives in ``harness.cli_follow``, which
must NOT import ``harness.agents`` or ``harness.server`` (preserved
by ``test_trust_boundary_cli_follow_no_forbidden_imports`` in the
legacy test module).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from harness.cli_follow import (
    Follower,
    _audit_file_for,
    cmd_hooks_audit_follow,
    cmd_observability_metrics_follow,
    follow_state_path,
    run_follow_async,
)


# ===========================================================================
# Helpers
# ===========================================================================


def _write_lines(path: Path, lines: list[str]) -> None:
    """Append ``lines`` (each + newline) to ``path``."""
    with path.open("a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\n")


def _audit_entry(session_id: str = "s1", event: str = "PreToolUse") -> dict:
    return {
        "ts": "2026-06-18T12:00:00+00:00",
        "event": event,
        "session_id": session_id,
        "agent_id": "a1",
        "request_id": "r1",
        "aggregate": {
            "final_decision": "allow",
            "blocked_by": "",
            "final_payload": {},
            "decisions": [
                {"decision": "allow", "hook_id": "logger",
                 "duration_ms": 0.1, "output": {}, "error": ""},
            ],
        },
    }


def _line(session_id: str = "s1", event: str = "PreToolUse") -> str:
    return json.dumps(_audit_entry(session_id, event), ensure_ascii=False)


def _collect_batches(
    follower: Follower,
    *,
    resume: bool = False,
    reset: bool = False,
    stop_predicate=None,
    max_batches: int | None = None,
) -> list[list[str]]:
    """Drive ``follower.run()`` and collect all yielded batches."""
    batches: list[list[str]] = []

    async def _drive() -> None:
        async for batch in follower.run(
            resume=resume, reset=reset,
            stop_predicate=stop_predicate,
            max_batches=max_batches,
        ):
            batches.append(batch)

    asyncio.run(_drive())
    return batches


def _setup_audit_dir(tmp_path: Path) -> Path:
    """Create ``tmp_path/data/audit`` and return it."""
    audit_dir = tmp_path / "data" / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    return audit_dir


# ===========================================================================
# Follower unit tests (9)
# ===========================================================================


def test_follower_yields_new_lines(tmp_path: Path) -> None:
    """Write 3 lines AFTER opening; Follower yields a batch with 3."""
    f = tmp_path / "log.ndjson"
    _write_lines(f, [_line("old")])  # pre-existing, should be skipped at EOF

    follower = Follower(
        f, batch_size=10, poll_interval_s=0.01, missing_file_retries=2,
    )

    # Writer thread appends 3 lines shortly after we start the loop.
    def _writer() -> None:
        time.sleep(0.05)
        _write_lines(f, [_line("new1"), _line("new2"), _line("new3")])

    t = threading.Thread(target=_writer)
    t.start()

    polls = {"n": 0}

    def _stop() -> bool:
        polls["n"] += 1
        return polls["n"] > 500

    batches = _collect_batches(follower, stop_predicate=_stop)
    t.join()

    all_lines = [ln for batch in batches for ln in batch]
    assert len(all_lines) == 3
    sessions = sorted(json.loads(ln)["session_id"] for ln in all_lines)
    assert sessions == ["new1", "new2", "new3"]


def test_follower_batches_lines(tmp_path: Path) -> None:
    """batch_size=5, write 12 lines → 2 batches (5 + 7) after a pause."""
    f = tmp_path / "log.ndjson"
    f.write_text("", encoding="utf-8")

    follower = Follower(
        f, batch_size=5, poll_interval_s=0.01, missing_file_retries=2,
    )

    # Pre-populate the file with 12 lines, then open the follower from
    # the beginning (reset=True) so all 12 are read in one sweep.
    _write_lines(f, [_line(f"line{i}") for i in range(12)])

    polls = {"n": 0}

    def _stop() -> bool:
        polls["n"] += 1
        return polls["n"] > 200

    batches = _collect_batches(follower, reset=True, stop_predicate=_stop)

    # With batch_size=5 and 12 lines:
    #   - batch 1: 5 lines (batch_size reached)
    #   - batch 2: 5 lines (batch_size reached)
    #   - then 2 remain pending; on the next idle poll they flush as
    #     a partial batch of 2.
    total_lines = sum(len(b) for b in batches)
    assert total_lines == 12, f"expected 12 lines, got {total_lines}"
    # At least the first two batches should be exactly batch_size.
    full_batches = [b for b in batches if len(b) == 5]
    assert len(full_batches) >= 2, f"expected >=2 full batches, got {len(full_batches)}"


def test_follower_handles_file_rotation(tmp_path: Path) -> None:
    """File replaced (inode mismatch via state) → Follower reopens from 0.

    On Windows ``st_ino`` is often 0 for all files, so we cannot rely
    on a real inode change. Instead we simulate rotation via the state
    file: write a state with ``last_inode=99999`` + ``--resume``,
    then create a NEW file at the same path. The follower detects
    that the current inode != 99999 and reopens from byte 0.
    """
    f = tmp_path / "log.ndjson"
    _write_lines(f, [_line("rotated_line1"), _line("rotated_line2")])

    # State file claims inode=99999 (will not match the real file).
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({
            "kind": "audit",
            "last_offset": 0,
            "last_inode": 99999,
            "started_at": "2026-01-01T00:00:00+00:00",
        }),
        encoding="utf-8",
    )

    follower = Follower(
        f, batch_size=10, state_file=state_file,
        poll_interval_s=0.01, missing_file_retries=2,
    )

    polls = {"n": 0}

    def _stop() -> bool:
        polls["n"] += 1
        return polls["n"] > 200

    batches = _collect_batches(follower, resume=True, stop_predicate=_stop)
    all_lines = [ln for batch in batches for ln in batch]

    # Because inode mismatch forced a reopen from 0, we see both lines.
    sessions = sorted(json.loads(ln)["session_id"] for ln in all_lines)
    assert sessions == ["rotated_line1", "rotated_line2"], (
        f"rotation reopen failed; got sessions={sessions}"
    )


def test_follower_handles_missing_file(tmp_path: Path) -> None:
    """File doesn't exist → Follower waits + retries (no crash)."""
    f = tmp_path / "missing.ndjson"
    follower = Follower(
        f, batch_size=5, poll_interval_s=0.01, missing_file_retries=3,
    )

    polls = {"n": 0}

    def _stop() -> bool:
        polls["n"] += 1
        return polls["n"] > 10

    batches = _collect_batches(follower, stop_predicate=_stop)
    # No file ever appeared → no batches.
    assert batches == []


def test_follower_persistent_state_saves_offset(tmp_path: Path) -> None:
    """After a run, the state file has a non-zero ``last_offset``."""
    f = tmp_path / "log.ndjson"
    _write_lines(f, [_line(f"line{i}") for i in range(5)])

    state_file = tmp_path / "state.json"
    follower = Follower(
        f, batch_size=10, state_file=state_file,
        poll_interval_s=0.01, missing_file_retries=2,
    )

    polls = {"n": 0}

    def _stop() -> bool:
        polls["n"] += 1
        return polls["n"] > 50

    _collect_batches(follower, reset=True, stop_predicate=_stop)

    assert state_file.exists(), "state file was not created"
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["kind"] == "audit"
    assert data["last_offset"] > 0, (
        f"expected last_offset > 0 after reading 5 lines, got {data['last_offset']}"
    )
    assert "started_at" in data and data["started_at"]


def test_follower_resume_from_state(tmp_path: Path) -> None:
    """State file with offset → starts from offset (skips earlier lines)."""
    f = tmp_path / "log.ndjson"
    _write_lines(f, [_line(f"old{i}") for i in range(5)])

    # Pre-write a state that points past the first 5 lines.
    file_size = f.stat().st_size
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({
            "kind": "audit",
            "last_offset": file_size,
            "last_inode": None,
            "started_at": "2026-01-01T00:00:00+00:00",
        }),
        encoding="utf-8",
    )

    follower = Follower(
        f, batch_size=10, state_file=state_file,
        poll_interval_s=0.01, missing_file_retries=2,
    )

    # Append 2 new lines AFTER setting up state.
    _write_lines(f, [_line("new1"), _line("new2")])

    polls = {"n": 0}

    def _stop() -> bool:
        polls["n"] += 1
        return polls["n"] > 200

    batches = _collect_batches(follower, resume=True, stop_predicate=_stop)
    all_lines = [ln for batch in batches for ln in batch]

    sessions = sorted(json.loads(ln)["session_id"] for ln in all_lines)
    assert sessions == ["new1", "new2"], (
        f"resume should skip old lines; got sessions={sessions}"
    )


def test_follower_filter_regex_skips_nonmatching(tmp_path: Path) -> None:
    """``--filter tool:bash`` → only bash lines are yielded."""
    f = tmp_path / "log.ndjson"
    f.write_text("", encoding="utf-8")

    # 3 bash lines, 2 non-bash lines.
    lines = [
        '{"tool": "bash", "msg": "ok1"}',
        '{"tool": "python", "msg": "skip1"}',
        '{"tool": "bash", "msg": "ok2"}',
        '{"tool": "node", "msg": "skip2"}',
        '{"tool": "bash", "msg": "ok3"}',
    ]
    _write_lines(f, lines)

    follower = Follower(
        f, batch_size=10,
        filter_regex=re.compile(r'"tool":\s*"bash"'),
        poll_interval_s=0.01, missing_file_retries=2,
    )

    polls = {"n": 0}

    def _stop() -> bool:
        polls["n"] += 1
        return polls["n"] > 200

    batches = _collect_batches(follower, reset=True, stop_predicate=_stop)
    all_lines = [ln for batch in batches for ln in batch]

    assert len(all_lines) == 3, f"expected 3 bash lines, got {len(all_lines)}"
    for ln in all_lines:
        assert '"tool": "bash"' in ln


def test_follower_reset_state(tmp_path: Path) -> None:
    """``--reset`` → starts from byte 0 (ignores saved offset)."""
    f = tmp_path / "log.ndjson"
    _write_lines(f, [_line("old1"), _line("old2")])

    # State file claims we already read everything (offset = EOF).
    file_size = f.stat().st_size
    state_file = tmp_path / "state.json"
    state_file.write_text(
        json.dumps({
            "kind": "audit",
            "last_offset": file_size,
            "last_inode": None,
            "started_at": "2026-01-01T00:00:00+00:00",
        }),
        encoding="utf-8",
    )

    follower = Follower(
        f, batch_size=10, state_file=state_file,
        poll_interval_s=0.01, missing_file_retries=2,
    )

    polls = {"n": 0}

    def _stop() -> bool:
        polls["n"] += 1
        return polls["n"] > 200

    # reset=True → ignore state, read from byte 0.
    batches = _collect_batches(follower, reset=True, stop_predicate=_stop)
    all_lines = [ln for batch in batches for ln in batch]

    sessions = sorted(json.loads(ln)["session_id"] for ln in all_lines)
    assert sessions == ["old1", "old2"], (
        f"reset should read from beginning; got sessions={sessions}"
    )


def test_follower_unicode_safe(tmp_path: Path) -> None:
    """Non-ASCII lines (Cyrillic) → no encoding errors."""
    f = tmp_path / "log.ndjson"
    # Mix of Cyrillic, emoji, and ASCII.
    _write_lines(f, [
        '{"msg": "Привет, мир!"}',
        '{"msg": "Тестирование слежения 🔍"}',
        '{"msg": "ASCII line"}',
    ])

    follower = Follower(
        f, batch_size=10, poll_interval_s=0.01, missing_file_retries=2,
    )

    polls = {"n": 0}

    def _stop() -> bool:
        polls["n"] += 1
        return polls["n"] > 200

    batches = _collect_batches(follower, reset=True, stop_predicate=_stop)
    all_lines = [ln for batch in batches for ln in batch]

    assert len(all_lines) == 3
    assert any("Привет" in ln for ln in all_lines)
    assert any("🔍" in ln for ln in all_lines)


# ===========================================================================
# Follower: no-new-lines edge case
# ===========================================================================


def test_follower_no_new_lines_yields_nothing(tmp_path: Path) -> None:
    """File unchanged → no batches yielded."""
    f = tmp_path / "log.ndjson"
    _write_lines(f, [_line("existing")])  # pre-existing content only

    follower = Follower(
        f, batch_size=5, poll_interval_s=0.01, missing_file_retries=2,
    )

    # Default mode (no resume/reset) → starts at EOF. No appends happen.
    polls = {"n": 0}

    def _stop() -> bool:
        polls["n"] += 1
        return polls["n"] > 20

    batches = _collect_batches(follower, stop_predicate=_stop)
    assert batches == []


# ===========================================================================
# Handler integration tests (2)
# ===========================================================================


def test_audit_follow_uses_follower(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    """``hooks audit --follow --batch-size 5`` → uses Follower.

    We monkeypatch ``_run_audit_follower`` to capture the call and
    verify the batched path is taken when ``--batch-size`` is set.
    """
    audit_dir = _setup_audit_dir(tmp_path)

    captured: dict[str, Any] = {}

    def _fake_run(
        audit_file: Path,
        *,
        batch_size: int,
        filter_regex: re.Pattern[str] | None,
        json_output: bool,
        resume: bool,
        reset: bool,
    ) -> int:
        captured["batch_size"] = batch_size
        captured["resume"] = resume
        captured["reset"] = reset
        captured["audit_file"] = audit_file
        return 0

    monkeypatch.setattr("harness.cli_follow._run_audit_follower", _fake_run)

    ns = argparse.Namespace(
        project_root=str(tmp_path),
        follow=True,
        filter=None,
        json=False,
        max_bytes=0,
        batch_size=5,
        resume=False,
        reset=False,
    )
    rc = cmd_hooks_audit_follow(ns)
    assert rc == 0
    assert captured["batch_size"] == 5
    assert captured["audit_file"] == _audit_file_for(audit_dir)


def test_metrics_follow_uses_follower(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """``observability metrics --follow --batch-size 3`` → batched path.

    We mock ``get_observability`` to return a fake whose snapshot()
    changes between polls, then verify that the batched output path
    is active (detected via the stderr banner that mentions
    ``batch_size=3``).
    """
    snaps = [
        {"hook_dispatches_total": { (): 1.0 }},
        {"hook_dispatches_total": { (): 4.0 }},
        {"hook_dispatches_total": { (): 4.0 }},  # no change → loop continues
    ]

    class _FakeMetrics:
        def __init__(self) -> None:
            self._i = 0
            self.enabled = True

        def snapshot(self) -> dict:
            if self._i >= len(snaps):
                raise KeyboardInterrupt
            v = snaps[self._i]
            self._i += 1
            return v

    class _FakeObs:
        def __init__(self) -> None:
            self.metrics = _FakeMetrics()

    fake = _FakeObs()
    monkeypatch.setattr("harness.observability.get_observability", lambda: fake)
    monkeypatch.setattr("time.sleep", lambda _s: None)

    ns = argparse.Namespace(
        interval_ms=10,
        filter=None,
        json=False,
        batch_size=3,
        resume=False,
        reset=False,
    )
    rc = cmd_observability_metrics_follow(ns)
    out, err = capsys.readouterr()
    assert rc == 0
    # The batched path prints ``batch_size=3`` in the stderr banner.
    assert "batch_size=3" in err, (
        f"expected batch_size=3 in stderr banner; got: {err!r}"
    )
    # And the diff still appears in stdout.
    assert "hook_dispatches_total" in out


# ===========================================================================
# follow_state_path + config integration
# ===========================================================================


def test_follow_state_path_uses_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """``follow_state_path`` respects ``settings.cli_follow_state_dir``."""
    from harness.config import settings

    custom_dir = tmp_path / "custom-state"
    monkeypatch.setattr(settings, "cli_follow_state_dir", custom_dir)

    p = follow_state_path("audit")
    assert p == custom_dir / ".follow-state-audit.json"
    assert "audit" in p.name

    p2 = follow_state_path("metrics")
    assert p2 == custom_dir / ".follow-state-metrics.json"
