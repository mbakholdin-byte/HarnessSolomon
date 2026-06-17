"""Phase 4.7 v1.17.0: tests for ``harness observability stats --diff``.

Covers:
  - ``_run_stats_diff`` (the diff engine) and the ``--diff`` flag
    plumbing through ``_cmd_observability_stats``.
  - exit codes: 0 (no changes), 1 (file/JSON error), 2 (delta).
  - NEW / REMOVED / CHANGED statuses.
  - ``--json`` NDJSON output.
  - Trust boundary preservation (cli_observability.py still does not
    import harness.agents / harness.server).

Strategy: invoke the handler directly with a synthesised argparse
Namespace (no subprocess) so the tests stay fast. Snapshot files are
written to ``tmp_path`` in the same JSON layout that
``harness observability stats --json`` produces.
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any

import pytest

from harness.cli_observability import _cmd_observability_stats, _run_stats_diff


# === Helpers ===============================================================


def _snapshot_json(metrics: dict[str, dict[str, float]]) -> str:
    """Serialise a metrics dict in the ``stats --json`` layout.

    Mirrors the shape printed by :func:`_cmd_observability_stats`
    (``{"metrics": {...}, "count": N, "note": "..."}``). The diff
    loader also accepts the bare-metrics shape, but we use the full
    layout here so we exercise the primary production path.
    """
    return json.dumps(
        {
            "metrics": metrics,
            "count": len(metrics),
            "note": "test fixture",
        },
        ensure_ascii=False,
        indent=2,
    )


def _write_snapshot(path: Path, metrics: dict[str, dict[str, float]]) -> Path:
    path.write_text(_snapshot_json(metrics), encoding="utf-8")
    return path


def _ns(
    *,
    diff: list[str] | None = None,
    json_output: bool = False,
) -> argparse.Namespace:
    """Build an argparse.Namespace mirroring the ``stats`` parser."""
    return argparse.Namespace(diff=diff, json=json_output)


# === Diff engine: no changes ==============================================


def test_stats_diff_no_changes(capsys: pytest.CaptureFixture, tmp_path: Path) -> None:
    """Two identical snapshots → exit 0, "(no metric changes)"."""
    metrics = {
        "http_requests_total": {"route=/,method=GET,status=200": 5.0},
        "queue_depth": {"(no labels)": 3.0},
    }
    before = _write_snapshot(tmp_path / "before.json", metrics)
    after = _write_snapshot(tmp_path / "after.json", metrics)
    rc = _run_stats_diff(before, after, json_output=False)
    out, err = capsys.readouterr()
    assert rc == 0, f"expected exit 0, got {rc}; stderr={err}"
    assert "(no metric changes)" in out


def test_stats_diff_no_changes_json(
    capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """Identical snapshots with --json → exit 0, empty stdout."""
    metrics = {"counter_a": {"k=v": 10.0}}
    before = _write_snapshot(tmp_path / "b.json", metrics)
    after = _write_snapshot(tmp_path / "a.json", metrics)
    rc = _run_stats_diff(before, after, json_output=True)
    out, _ = capsys.readouterr()
    assert rc == 0
    # No delta rows → no NDJSON lines.
    assert out.strip() == ""


# === Diff engine: counter changes ========================================


def test_stats_diff_counter_increased(
    capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """Counter +5 → exit 2, Δ=+5."""
    before = _write_snapshot(tmp_path / "b.json", {"c": {"l=1": 10.0}})
    after = _write_snapshot(tmp_path / "a.json", {"c": {"l=1": 15.0}})
    rc = _run_stats_diff(before, after, json_output=False)
    out, _ = capsys.readouterr()
    assert rc == 2
    assert "CHANGED" in out
    # Δ is rendered as "+5" (g-format strips the trailing .0).
    assert "+5" in out


def test_stats_diff_counter_decreased(
    capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """Counter −3 → exit 2, Δ=−3."""
    before = _write_snapshot(tmp_path / "b.json", {"c": {"l=1": 10.0}})
    after = _write_snapshot(tmp_path / "a.json", {"c": {"l=1": 7.0}})
    rc = _run_stats_diff(before, after, json_output=False)
    out, _ = capsys.readouterr()
    assert rc == 2
    assert "CHANGED" in out
    # Δ is rendered as "-3".
    assert "-3" in out


def test_stats_diff_float_gauge_change(
    capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """Gauge 1.5 → 2.25 → Δ=+0.75."""
    before = _write_snapshot(tmp_path / "b.json", {"g": {"": 1.5}})
    after = _write_snapshot(tmp_path / "a.json", {"g": {"": 2.25}})
    rc = _run_stats_diff(before, after, json_output=False)
    out, _ = capsys.readouterr()
    assert rc == 2
    assert "+0.75" in out


# === Diff engine: NEW / REMOVED ==========================================


def test_stats_diff_new_metric(
    capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """A metric present only in AFTER is marked NEW with +value."""
    before = _write_snapshot(tmp_path / "b.json", {"old": {"k=1": 1.0}})
    after = _write_snapshot(
        tmp_path / "a.json",
        {"old": {"k=1": 1.0}, "fresh": {"k=2": 42.0}},
    )
    rc = _run_stats_diff(before, after, json_output=False)
    out, _ = capsys.readouterr()
    assert rc == 2
    assert "NEW" in out
    assert "fresh" in out
    assert "+42" in out


def test_stats_diff_removed_metric(
    capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """A metric present only in BEFORE is marked REMOVED with -value."""
    before = _write_snapshot(
        tmp_path / "b.json",
        {"keep": {"k=1": 1.0}, "gone": {"k=2": 7.0}},
    )
    after = _write_snapshot(tmp_path / "a.json", {"keep": {"k=1": 1.0}})
    rc = _run_stats_diff(before, after, json_output=False)
    out, _ = capsys.readouterr()
    assert rc == 2
    assert "REMOVED" in out
    assert "gone" in out
    assert "-7" in out


# === Diff engine: --json output ==========================================


def test_stats_diff_json_output(
    capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """``--json`` → one NDJSON record per delta row."""
    before = _write_snapshot(tmp_path / "b.json", {"c": {"l=1": 5.0}})
    after = _write_snapshot(tmp_path / "a.json", {"c": {"l=1": 8.0}})
    rc = _run_stats_diff(before, after, json_output=True)
    out, _ = capsys.readouterr()
    assert rc == 2
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["metric"] == "c"
    assert record["status"] == "CHANGED"
    assert record["delta"] == 3.0
    assert record["before"] == 5.0
    assert record["after"] == 8.0


def test_stats_diff_json_new_and_removed(
    capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """--json emits NEW and REMOVED rows in the same NDJSON stream."""
    before = _write_snapshot(
        tmp_path / "b.json", {"gone": {"k=1": 2.0}, "same": {"k=2": 9.0}},
    )
    after = _write_snapshot(
        tmp_path / "a.json", {"same": {"k=2": 9.0}, "fresh": {"k=3": 1.0}},
    )
    rc = _run_stats_diff(before, after, json_output=True)
    out, _ = capsys.readouterr()
    assert rc == 2
    records = [json.loads(ln) for ln in out.splitlines() if ln.strip()]
    statuses = {r["status"] for r in records}
    assert statuses == {"NEW", "REMOVED"}
    new_rec = next(r for r in records if r["status"] == "NEW")
    assert new_rec["metric"] == "fresh"
    assert new_rec["before"] is None
    assert new_rec["after"] == 1.0
    rem_rec = next(r for r in records if r["status"] == "REMOVED")
    assert rem_rec["metric"] == "gone"
    assert rem_rec["before"] == 2.0
    assert rem_rec["after"] is None


# === Diff engine: error paths ============================================


def test_stats_diff_missing_before_file(
    capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """BEFORE file not found → exit 1 with a clear message."""
    after = _write_snapshot(tmp_path / "a.json", {"c": {"k=1": 1.0}})
    rc = _run_stats_diff(tmp_path / "missing.json", after, json_output=False)
    out, err = capsys.readouterr()
    assert rc == 1
    assert "BEFORE file not found" in err


def test_stats_diff_missing_after_file(
    capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """AFTER file not found → exit 1."""
    before = _write_snapshot(tmp_path / "b.json", {"c": {"k=1": 1.0}})
    rc = _run_stats_diff(before, tmp_path / "nope.json", json_output=False)
    out, err = capsys.readouterr()
    assert rc == 1
    assert "AFTER file not found" in err


def test_stats_diff_invalid_json(
    capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """Corrupt JSON in BEFORE → exit 1 with parse error."""
    (tmp_path / "b.json").write_text("{not valid json", encoding="utf-8")
    after = _write_snapshot(tmp_path / "a.json", {"c": {"k=1": 1.0}})
    rc = _run_stats_diff(tmp_path / "b.json", after, json_output=False)
    out, err = capsys.readouterr()
    assert rc == 1
    assert "cannot parse BEFORE" in err


def test_stats_diff_invalid_json_after(
    capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """Corrupt JSON in AFTER → exit 1."""
    before = _write_snapshot(tmp_path / "b.json", {"c": {"k=1": 1.0}})
    (tmp_path / "a.json").write_text("<<<", encoding="utf-8")
    rc = _run_stats_diff(before, tmp_path / "a.json", json_output=False)
    out, err = capsys.readouterr()
    assert rc == 1
    assert "cannot parse AFTER" in err


# === Plumbing through _cmd_observability_stats ===========================


def test_cmd_stats_diff_dispatches_to_run_diff(
    capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """Passing ``--diff BEFORE AFTER`` via the Namespace triggers diff mode."""
    before = _write_snapshot(tmp_path / "b.json", {"c": {"k=1": 1.0}})
    after = _write_snapshot(tmp_path / "a.json", {"c": {"k=1": 2.0}})
    rc = _cmd_observability_stats(
        _ns(diff=[str(before), str(after)], json_output=False),
    )
    out, _ = capsys.readouterr()
    assert rc == 2
    assert "CHANGED" in out
    assert "+1" in out


def test_cmd_stats_no_diff_runs_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Without --diff, the handler falls through to the snapshot path.

    We monkeypatch ``get_observability`` so no real metrics registry
    is touched. The snapshot will be empty → "(no metrics ...)" on
    stderr and exit 0.
    """

    class _StubObs:
        class metrics:
            @staticmethod
            def snapshot() -> dict:
                return {}

    import harness.observability as obs_pkg
    monkeypatch.setattr(obs_pkg, "get_observability", lambda: _StubObs())
    rc = _cmd_observability_stats(_ns(diff=None, json_output=False))
    out, err = capsys.readouterr()
    assert rc == 0
    assert "no metrics" in err or "no metrics" in out


# === Bare-metrics JSON (no ``metrics`` wrapper) ==========================


def test_stats_diff_accepts_bare_metrics_json(
    capsys: pytest.CaptureFixture, tmp_path: Path,
) -> None:
    """A bare ``{name: {labels: value}}`` JSON is also accepted."""
    (tmp_path / "b.json").write_text(
        json.dumps({"c": {"k=1": 1.0}}), encoding="utf-8",
    )
    (tmp_path / "a.json").write_text(
        json.dumps({"c": {"k=1": 4.0}}), encoding="utf-8",
    )
    rc = _run_stats_diff(tmp_path / "b.json", tmp_path / "a.json", json_output=False)
    out, _ = capsys.readouterr()
    assert rc == 2
    assert "+3" in out


# === Trust boundary preservation =========================================


_CLI_OBS_PATH = (
    Path(__file__).resolve().parent.parent / "harness" / "cli_observability.py"
)
_FORBIDDEN_PREFIXES: tuple[str, ...] = ("harness.agents", "harness.server")


def test_trust_boundary_cli_observability_no_forbidden_imports() -> None:
    """AST-scan cli_observability.py: still no agents/server imports."""
    assert _CLI_OBS_PATH.is_file()
    source = _CLI_OBS_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_CLI_OBS_PATH))

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
        "Trust boundary violations in harness/cli_observability.py:\n  "
        + "\n  ".join(violations)
    )


def _check_module(module: str, lineno: int, violations: list[str]) -> None:
    for prefix in _FORBIDDEN_PREFIXES:
        if module == prefix or module.startswith(prefix + "."):
            violations.append(
                f"harness/cli_observability.py:{lineno}: forbidden import "
                f"{module!r} (prefix {prefix!r} not allowed)"
            )
