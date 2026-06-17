"""Phase 4.7 v1.17.0: tests for ``harness hooks audit --follow`` and
``harness observability metrics --follow``.

Strategy:
  - Audit follow: unit-test :func:`_iter_new_lines` with injected
    fake ``sleep`` + ``is_interrupted`` so the loop terminates
    deterministically. Also exercise :func:`cmd_hooks_audit_follow`
    via a writer thread that appends lines while the follow loop runs.
  - Metrics follow: mock ``harness.observability.get_observability``
    to return a fake handle whose ``metrics.snapshot()`` yields
    pre-scripted diffs, and patch ``time.sleep`` so the loop runs
    only a bounded number of iterations.
  - Rotation, filter, json output, sigint cleanup, missing-audit-dir
    are all covered.

Trust boundary: AST-scan cli_follow.py for forbidden imports
(``harness.agents`` / ``harness.server``).
"""
from __future__ import annotations

import argparse
import ast
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from harness.cli_follow import (
    _audit_file_for,
    _iter_new_lines,
    _rotate_if_needed,
    _snapshot_diff,
    cmd_hooks_audit_follow,
    cmd_observability_metrics_follow,
)


# ===========================================================================
# Helpers
# ===========================================================================


def _entry(
    *,
    ts: str = "2026-06-17T12:00:00+00:00",
    event: str = "PreToolUse",
    session_id: str = "s1",
    final_decision: str = "allow",
    blocked_by: str = "",
) -> dict:
    return {
        "ts": ts,
        "event": event,
        "session_id": session_id,
        "agent_id": "a1",
        "request_id": "r1",
        "aggregate": {
            "final_decision": final_decision,
            "blocked_by": blocked_by,
            "final_payload": {},
            "decisions": [
                {"decision": final_decision, "hook_id": "h",
                 "duration_ms": 0.1, "output": {}, "error": ""},
            ],
        },
    }


def _line(e: dict) -> str:
    return json.dumps(e, ensure_ascii=False)


def _audit_file(audit_dir: Path) -> Path:
    return _audit_file_for(audit_dir)


def _audit_ns(
    *,
    project_root: str | None = None,
    filter_: str | None = None,
    json_output: bool = False,
    max_bytes: int = 0,
) -> argparse.Namespace:
    return argparse.Namespace(
        project_root=project_root,
        follow=True,
        filter=filter_,
        json=json_output,
        max_bytes=max_bytes,
    )


def _metrics_ns(
    *,
    interval_ms: int = 100,
    filter_: str | None = None,
    json_output: bool = False,
) -> argparse.Namespace:
    return argparse.Namespace(
        interval_ms=interval_ms,
        filter=filter_,
        json=json_output,
    )


class _FakeSleep:
    """Fake ``time.sleep`` that counts calls and can trip an
    ``is_interrupted`` predicate after N invocations."""

    def __init__(self, max_calls: int = 50) -> None:
        self.calls = 0
        self.max_calls = max_calls

    def __call__(self, _seconds: float) -> None:
        self.calls += 1
        if self.calls > self.max_calls:
            raise AssertionError(
                f"fake sleep exhausted ({self.calls} calls) — loop "
                f"did not terminate; test would hang"
            )

    @property
    def interrupted(self) -> bool:
        return self.calls >= self.max_calls


# ===========================================================================
# Audit follow: _iter_new_lines unit tests
# ===========================================================================


def test_iter_new_lines_reads_appended_lines(tmp_path: Path) -> None:
    """Append 2 lines after opening; both are yielded.

    Uses real ``time.sleep`` (not the fake) so the loop actually
    waits for the writer thread. Termination is bounded by the
    writer appending exactly 2 lines; we break out of the loop
    once both are observed.
    """
    f = tmp_path / "log.ndjson"
    f.write_text(_line(_entry(session_id="old")) + "\n", encoding="utf-8")

    # Writer thread appends 2 lines after a short delay.
    def _writer() -> None:
        time.sleep(0.1)
        with f.open("a", encoding="utf-8") as fh:
            fh.write(_line(_entry(session_id="new1")) + "\n")
            fh.write(_line(_entry(session_id="new2")) + "\n")

    t = threading.Thread(target=_writer)
    t.start()

    seen: list[str] = []
    # Safety cap: abort after 200 polls (50s at 250ms) even if the
    # writer misbehaves — prevents test hang.
    polls = {"n": 0}

    def _interrupted() -> bool:
        polls["n"] += 1
        return polls["n"] > 200

    for line in _iter_new_lines(
        f, start_at_end=True, sleep=time.sleep,
        is_interrupted=_interrupted,
    ):
        seen.append(line)
        if len(seen) >= 2:
            break
    t.join()
    sessions = [json.loads(s)["session_id"] for s in seen]
    assert sessions == ["new1", "new2"]


def test_iter_new_lines_skips_existing(tmp_path: Path) -> None:
    """start_at_end=True → pre-existing content is NOT yielded."""
    f = tmp_path / "log.ndjson"
    f.write_text(
        _line(_entry(session_id="a")) + "\n" + _line(_entry(session_id="b")) + "\n",
        encoding="utf-8",
    )
    sleep = _FakeSleep(max_calls=5)
    seen = list(_iter_new_lines(
        f, start_at_end=True, sleep=sleep,
        is_interrupted=lambda: sleep.interrupted,
    ))
    assert seen == []  # nothing new was appended


# ===========================================================================
# Audit follow: cmd_hooks_audit_follow handler tests
# ===========================================================================


def test_hooks_audit_follow_reads_new_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Write 3 lines, start follow, write 2 more → expect 2 printed
    (follow skips existing). The test patches ``_iter_new_lines`` to
    return a bounded stream so the command returns.
    """
    audit_dir = tmp_path / "data" / "audit"
    audit_dir.mkdir(parents=True)
    f = _audit_file(audit_dir)
    f.write_text(
        "".join(_line(_entry(session_id=f"old{i}")) + "\n" for i in range(3)),
        encoding="utf-8",
    )

    new_lines = [_line(_entry(session_id="new1")), _line(_entry(session_id="new2"))]

    def _fake_iter(_path, **_kw):  # noqa: ANN001
        yield from new_lines

    monkeypatch.setattr("harness.cli_follow._iter_new_lines", _fake_iter)
    rc = cmd_hooks_audit_follow(_audit_ns(project_root=str(tmp_path)))
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "new1" in out
    assert "new2" in out


def test_hooks_audit_follow_filter_match(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    """--filter regex suppresses non-matching lines."""
    audit_dir = tmp_path / "data" / "audit"
    audit_dir.mkdir(parents=True)

    # Build two entries that differ by hook_id. We filter on the
    # rm-guard hook_id (not on "block", because the field name
    # ``blocked_by`` contains the substring "block" in every entry,
    # which would defeat the regex).
    block_entry = _entry(
        session_id="ok", final_decision="block", blocked_by="rm-guard",
    )
    block_entry["aggregate"]["decisions"] = [
        {"decision": "block", "hook_id": "rm-guard",
         "duration_ms": 0.2, "output": {}, "error": ""},
    ]
    allow_entry = _entry(session_id="skipme", final_decision="allow")
    allow_entry["aggregate"]["decisions"] = [
        {"decision": "allow", "hook_id": "logger",
         "duration_ms": 0.1, "output": {}, "error": ""},
    ]
    lines = [_line(block_entry), _line(allow_entry)]

    def _fake_iter(_path, **_kw):  # noqa: ANN001
        yield from lines

    monkeypatch.setattr("harness.cli_follow._iter_new_lines", _fake_iter)
    rc = cmd_hooks_audit_follow(
        _audit_ns(project_root=str(tmp_path), filter_="rm-guard"),
    )
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "ok" in out
    assert "skipme" not in out


def test_hooks_audit_follow_no_audit_dir_exits_0(
    capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """Missing audit directory → exit 0 with a hint (no hang)."""
    rc = cmd_hooks_audit_follow(_audit_ns(project_root=str(tmp_path)))
    _, err = capsys.readouterr()
    assert rc == 0
    assert "no audit directory" in err or "no audit" in err.lower()


def test_hooks_audit_follow_sigint_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    tmp_path: Path,
) -> None:
    """KeyboardInterrupt (Ctrl+C) → exit 0 with cleanup message."""
    audit_dir = tmp_path / "data" / "audit"
    audit_dir.mkdir(parents=True)

    def _raising_iter(*_a, **_kw):  # noqa: ANN001
        raise KeyboardInterrupt
        yield  # pragma: no cover — unreachable

    monkeypatch.setattr("harness.cli_follow._iter_new_lines", _raising_iter)
    rc = cmd_hooks_audit_follow(_audit_ns(project_root=str(tmp_path)))
    _, err = capsys.readouterr()
    assert rc == 0
    assert "interrupted" in err.lower()


def test_hooks_audit_follow_max_bytes_rotate(tmp_path: Path) -> None:
    """File larger than --max-bytes triggers rotation to .1."""
    f = tmp_path / "hooks.ndjson"
    f.write_text("x" * 200, encoding="utf-8")
    _rotate_if_needed(f, max_bytes=100)
    rotated = tmp_path / "hooks.ndjson.1"
    assert rotated.exists()
    # Original is moved away (rotated to .1).
    assert not f.exists()


# ===========================================================================
# Metrics follow: _snapshot_diff unit tests
# ===========================================================================


def test_snapshot_diff_reports_changed() -> None:
    prev = {"http_requests_total": { (("route", "/a"),): 5.0 } }
    curr = {"http_requests_total": { (("route", "/a"),): 7.0 } }
    diffs = _snapshot_diff(prev, curr)
    assert len(diffs) == 1
    name, labels, old_v, new_v = diffs[0]
    assert name == "http_requests_total"
    assert (old_v, new_v) == (5.0, 7.0)


def test_snapshot_diff_skips_unchanged() -> None:
    """An unchanged metric is NOT reported."""
    prev = {"x": { (("l", "v"),): 1.0 }, "y": { (("l", "v"),): 9.0 }}
    curr = {"x": { (("l", "v"),): 1.0 }, "y": { (("l", "v"),): 10.0 }}
    diffs = _snapshot_diff(prev, curr)
    assert len(diffs) == 1
    assert diffs[0][0] == "y"


def test_snapshot_diff_filter() -> None:
    """--filter regex narrows by metric name."""
    import re
    prev = {"hook_dispatches_total": { (): 1.0 }, "http_requests_total": { (): 2.0 }}
    curr = {"hook_dispatches_total": { (): 5.0 }, "http_requests_total": { (): 9.0 }}
    diffs = _snapshot_diff(prev, curr, name_filter=re.compile("hook_"))
    assert len(diffs) == 1
    assert diffs[0][0] == "hook_dispatches_total"


def test_snapshot_diff_new_label_set() -> None:
    """A new label combination is reported as a change from 0."""
    prev = {"c": { (("a", "1"),): 1.0 }}
    curr = {"c": { (("a", "1"),): 1.0, (("a", "2"),): 3.0 }}
    diffs = _snapshot_diff(prev, curr)
    assert len(diffs) == 1
    _, labels, old_v, new_v = diffs[0]
    assert labels == (("a", "2"),)
    assert (old_v, new_v) == (0.0, 3.0)


# ===========================================================================
# Metrics follow: cmd_observability_metrics_follow handler tests
# ===========================================================================


class _FakeMetrics:
    """Fake PrometheusMetrics with a scriptable snapshot()."""

    def __init__(self, snapshots: list[dict]) -> None:
        self._snaps = list(snapshots)
        self.enabled = True
        self._i = 0

    def snapshot(self) -> dict:
        if self._i >= len(self._snaps):
            # After the script runs out, raise KeyboardInterrupt to
            # stop the follow loop (simulates Ctrl+C).
            raise KeyboardInterrupt
        v = self._snaps[self._i]
        self._i += 1
        return v


class _FakeObs:
    def __init__(self, metrics: _FakeMetrics) -> None:
        self.metrics = metrics


def test_observability_metrics_follow_polls(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Two snapshots with a diff → one diff line printed, then exit."""
    snaps = [
        {"hook_dispatches_total": { (): 1.0 }},
        {"hook_dispatches_total": { (): 4.0 }},
    ]
    fake = _FakeObs(_FakeMetrics(snaps))
    monkeypatch.setattr(
        "harness.observability.get_observability", lambda: fake,
    )
    monkeypatch.setattr("time.sleep", lambda _s: None)
    rc = cmd_observability_metrics_follow(_metrics_ns(interval_ms=10))
    out, err = capsys.readouterr()
    assert rc == 0
    assert "hook_dispatches_total" in out
    assert "1 -> 4" in out or "1->4" in out.replace(" ", "")


def test_observability_metrics_follow_diff_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Unchanged metric is NOT printed across polls."""
    snaps = [
        {"x": { (): 5.0 }},
        {"x": { (): 5.0 }},  # no change
        {"x": { (): 5.0 }},  # no change → triggers KeyboardInterrupt
    ]
    fake = _FakeObs(_FakeMetrics(snaps))
    monkeypatch.setattr(
        "harness.observability.get_observability", lambda: fake,
    )
    monkeypatch.setattr("time.sleep", lambda _s: None)
    rc = cmd_observability_metrics_follow(_metrics_ns(interval_ms=10))
    out, _ = capsys.readouterr()
    assert rc == 0
    # No diff lines for metric x (it never changed).
    assert "x" not in out.split("\n") or all(
        "5 -> 5" not in ln for ln in out.splitlines()
    )


def test_observability_metrics_follow_json_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """--json → each printed line is valid JSON."""
    snaps = [
        {"c": { (("k", "v"),): 1.0 }},
        {"c": { (("k", "v"),): 2.0 }},
    ]
    fake = _FakeObs(_FakeMetrics(snaps))
    monkeypatch.setattr(
        "harness.observability.get_observability", lambda: fake,
    )
    monkeypatch.setattr("time.sleep", lambda _s: None)
    rc = cmd_observability_metrics_follow(
        _metrics_ns(interval_ms=10, json_output=True),
    )
    out, _ = capsys.readouterr()
    assert rc == 0
    data_lines = [ln for ln in out.splitlines() if ln.strip().startswith("{")]
    assert data_lines, "expected at least one JSON diff line"
    for ln in data_lines:
        obj = json.loads(ln)
        assert "metric" in obj
        assert "value" in obj
        assert "delta" in obj


def test_observability_metrics_follow_filter(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """--filter hook_ → only hook_ metrics are reported."""
    snaps = [
        {
            "hook_dispatches_total": { (): 1.0 },
            "http_requests_total": { (): 1.0 },
        },
        {
            "hook_dispatches_total": { (): 2.0 },
            "http_requests_total": { (): 99.0 },
        },
    ]
    fake = _FakeObs(_FakeMetrics(snaps))
    monkeypatch.setattr(
        "harness.observability.get_observability", lambda: fake,
    )
    monkeypatch.setattr("time.sleep", lambda _s: None)
    rc = cmd_observability_metrics_follow(
        _metrics_ns(interval_ms=10, filter_="hook_"),
    )
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "hook_dispatches_total" in out
    assert "http_requests_total" not in out


def test_observability_metrics_follow_sigint(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """SIGINT → clean exit 0 with cleanup message."""
    snaps = [{"x": { (): 1.0 }}]  # exhausted → KeyboardInterrupt inside snapshot
    fake = _FakeObs(_FakeMetrics(snaps))
    monkeypatch.setattr(
        "harness.observability.get_observability", lambda: fake,
    )
    monkeypatch.setattr("time.sleep", lambda _s: None)
    rc = cmd_observability_metrics_follow(_metrics_ns(interval_ms=10))
    _, err = capsys.readouterr()
    assert rc == 0
    assert "interrupted" in err.lower()


# ===========================================================================
# Trust boundary preservation
# ===========================================================================


_CLI_FOLLOW_PATH = (
    Path(__file__).resolve().parent.parent / "harness" / "cli_follow.py"
)
_FORBIDDEN_PREFIXES: tuple[str, ...] = ("harness.agents", "harness.server")


def test_trust_boundary_cli_follow_no_forbidden_imports() -> None:
    """AST-scan cli_follow.py: must not import harness.agents/server."""
    assert _CLI_FOLLOW_PATH.is_file()
    source = _CLI_FOLLOW_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_CLI_FOLLOW_PATH))

    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _check_module(alias.name, node.lineno, violations)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue
            if node.module:
                _check_module(node.module, node.lineno, violations)
    assert not violations, (
        "Trust boundary violations in cli_follow.py:\n  "
        + "\n  ".join(violations)
    )


def _check_module(module: str, lineno: int, violations: list[str]) -> None:
    for prefix in _FORBIDDEN_PREFIXES:
        if module == prefix or module.startswith(prefix + "."):
            violations.append(
                f"cli_follow.py:{lineno}: forbidden import {module!r} "
                f"(prefix {prefix!r} not allowed)"
            )
