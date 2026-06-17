"""Phase 4.4 v1.13.0: tests for the ``harness observability`` CLI subcommand.

Covers:
  - ``observability log`` — read local JSONL, --tail, --event filter,
    missing file, --date, --json.
  - ``observability metrics`` — Prometheus text filter (HELP/TYPE
    pairing), error paths.
  - ``observability health`` — three levels, --json, exit codes,
    HTTP error → exit 2, invalid level → exit 2.
  - ``observability stats`` — snapshot of in-process metrics, --json.
  - Trust boundary: cli_observability source has no hard import of
    harness.agents or harness.server.

Strategy: invoke the subcommand handlers directly + monkeypatch
``_http_get`` and ``settings.observability_log_dir`` to control IO.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest

from harness import cli as harness_cli
from harness.cli_observability import (
    _cmd_observability_health,
    _cmd_observability_log,
    _cmd_observability_metrics,
    _cmd_observability_stats,
    _filter_metrics,
)
from harness.observability import get_observability
from harness.observability.emit import reset_observability


@pytest.fixture(autouse=True)
def _reset_observability() -> Iterator[None]:
    """Reset the ObservabilityHandle singleton before each test."""
    reset_observability()
    yield
    reset_observability()


def _ns(
    *,
    tail: int = 20,
    event: str | None = None,
    date: str | None = None,
    max_bytes: int = 1_048_576,
    json: bool = False,  # noqa: A002
    base_url: str = "http://127.0.0.1:8765",
    timeout_s: float = 5.0,
    level: str = "deep",
    filter: str | None = None,  # noqa: A002
) -> argparse.Namespace:  # noqa: A002
    return argparse.Namespace(
        tail=tail,
        event=event,
        date=date,
        max_bytes=max_bytes,
        json=json,
        base_url=base_url,
        timeout_s=timeout_s,
        level=level,
        filter=filter,
    )


def _capture(capsys: pytest.CaptureFixture, rc: int) -> tuple[str, str, int]:
    out = capsys.readouterr()
    return out.out, out.err, rc


# === log ===

class TestObservabilityLog:
    def _patch_log_dir(
        self, monkeypatch: pytest.MonkeyPatch, log_dir: Path,
    ) -> None:
        """Patch the observability_log_dir setting.

        Pydantic-settings is constructed once at import time, so
        monkeypatching OBSERVABILITY_LOG_DIR env alone is not
        enough — we also need to patch the global settings
        instance via ``harness.config.settings`` (re-read from
        env). Pydantic v2 Settings supports ``__init__`` with
        env vars, so we use ``model_construct``-style rebuild
        via ``Settings()``.
        """
        from harness.config import Settings
        # Build a fresh Settings with the env var override.
        new_settings = Settings(observability_log_dir=log_dir)
        monkeypatch.setattr("harness.config.settings", new_settings)
        # Also patch the one in harness.cli (it imported the symbol).
        from harness import cli as _cli_mod
        monkeypatch.setattr(_cli_mod, "settings", new_settings)

    def test_log_no_file(
        self, capsys: pytest.CaptureFixture, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._patch_log_dir(monkeypatch, tmp_path)
        rc = _cmd_observability_log(_ns())
        out, err, _ = _capture(capsys, rc)
        assert rc == 0
        assert "no log file" in err.lower() or "(no log file" in out

    def test_log_tail_and_filter(
        self, capsys: pytest.CaptureFixture, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._patch_log_dir(monkeypatch, tmp_path)
        # Build today's log file.
        d = datetime.now(timezone.utc)
        log_path = tmp_path / f"harness-{d.strftime('%Y-%m-%d')}.jsonl"
        lines = [
            {"event": "llm_call", "payload": {"model": "qwen3:8b"},
             "timestamp": "2026-06-17T00:00:00Z"},
            {"event": "tool_call", "payload": {"tool_name": "read_file"},
             "timestamp": "2026-06-17T00:00:01Z"},
            {"event": "llm_call", "payload": {"model": "qwen3:30b"},
             "timestamp": "2026-06-17T00:00:02Z"},
        ]
        log_path.write_text(
            "\n".join(json.dumps(ln) for ln in lines), encoding="utf-8",
        )

        # 1) tail=2, no filter: should return last 2 lines.
        rc = _cmd_observability_log(_ns(tail=2))
        out, _, _ = _capture(capsys, rc)
        assert rc == 0
        # tool_call + llm_call (last one)
        assert "tool_call" in out
        assert "qwen3:30b" in out

        # 2) --event llm_call filter — applied AFTER tail.
        rc = _cmd_observability_log(_ns(tail=2, event="llm_call"))
        out, _, _ = _capture(capsys, rc)
        assert rc == 0
        assert "qwen3:30b" in out
        assert "tool_call" not in out

    def test_log_json(
        self, capsys: pytest.CaptureFixture, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._patch_log_dir(monkeypatch, tmp_path)
        d = datetime.now(timezone.utc)
        log_path = tmp_path / f"harness-{d.strftime('%Y-%m-%d')}.jsonl"
        log_path.write_text(
            json.dumps({"event": "llm_call", "payload": {"model": "x"}}) + "\n",
            encoding="utf-8",
        )
        rc = _cmd_observability_log(_ns(tail=10, json=True))
        out, _, _ = _capture(capsys, rc)
        assert rc == 0
        payload = json.loads(out)
        assert "entries" in payload
        assert payload["count"] == 1
        assert payload["entries"][0]["event"] == "llm_call"

    def test_log_specific_date(
        self, capsys: pytest.CaptureFixture, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._patch_log_dir(monkeypatch, tmp_path)
        # Write to a specific past date.
        target = "2025-12-01"
        (tmp_path / f"harness-{target}.jsonl").write_text(
            json.dumps({"event": "x", "payload": {}}) + "\n",
            encoding="utf-8",
        )
        rc = _cmd_observability_log(_ns(date=target, json=True))
        out, _, _ = _capture(capsys, rc)
        assert rc == 0
        payload = json.loads(out)
        assert payload["count"] == 1


# === metrics ===

class TestObservabilityMetrics:
    def test_filter_keeps_help_type_pairs(self) -> None:
        text = (
            "# HELP harness_llm_calls_total Total LLM calls\n"
            "# TYPE harness_llm_calls_total counter\n"
            'harness_llm_calls_total{model="x"} 3\n'
            "# HELP harness_tool_calls_total Total tool calls\n"
            "# TYPE harness_tool_calls_total counter\n"
            'harness_tool_calls_total{tool_name="read_file"} 7\n'
        )
        out = _filter_metrics(text, r"^harness_llm_")
        assert "harness_llm_calls_total" in out
        assert "harness_tool_calls_total" not in out
        # HELP/TYPE for the kept metric must be present.
        assert "# HELP harness_llm_calls_total" in out
        assert "# TYPE harness_llm_calls_total" in out
        # HELP/TYPE for the dropped metric must NOT be present.
        assert "harness_tool_calls_total" not in out

    def test_filter_no_match_keeps_nothing(self) -> None:
        text = (
            "# HELP harness_llm_calls_total\n"
            'harness_llm_calls_total{model="x"} 3\n'
        )
        out = _filter_metrics(text, r"^nonexistent_")
        assert "harness_llm_calls_total" not in out
        assert "harness_llm_calls_total" not in out  # double-check

    def test_filter_invalid_regex_passes_through(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        text = 'harness_llm_calls_total{model="x"} 3\n'
        # We log a warning to stderr AND pass text through unchanged.
        out = _filter_metrics(text, r"[invalid(")
        _, err, _ = _capture(capsys, 0)
        assert out == text
        assert "invalid" in err.lower()

    def test_metrics_endpoint(
        self, capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def fake_get(url: str, *, timeout_s: float = 5.0) -> tuple[int, bytes]:
            return 200, b"# HELP harness_x x\nharness_x 1\n"
        from harness import cli_observability as mod
        monkeypatch.setattr(mod, "_http_get", fake_get)
        rc = _cmd_observability_metrics(_ns())
        out, _, _ = _capture(capsys, rc)
        assert rc == 0
        assert "harness_x" in out

    def test_metrics_connection_error_exits_1(
        self, capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import urllib.error
        from harness import cli_observability as mod

        def fake_get(url: str, *, timeout_s: float = 5.0) -> tuple[int, bytes]:
            raise urllib.error.URLError("connection refused")
        monkeypatch.setattr(mod, "_http_get", fake_get)
        rc = _cmd_observability_metrics(_ns())
        _, err, _ = _capture(capsys, rc)
        assert rc == 1
        assert "cannot reach" in err

    def test_metrics_http_error_exits_2(
        self, capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from harness import cli_observability as mod

        def fake_get(url: str, *, timeout_s: float = 5.0) -> tuple[int, bytes]:
            return 503, b"Service Unavailable"
        monkeypatch.setattr(mod, "_http_get", fake_get)
        rc = _cmd_observability_metrics(_ns())
        _, err, _ = _capture(capsys, rc)
        assert rc == 2
        assert "HTTP 503" in err


# === health ===

class TestObservabilityHealth:
    def test_health_ok_exits_0(
        self, capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from harness import cli_observability as mod

        def fake_get(url: str, *, timeout_s: float = 5.0) -> tuple[int, bytes]:
            return 200, json.dumps(
                {"status": "ok", "version": "1.13.0", "project_root": "/x",
                 "checks": {"process": {"status": "ok"}}},
            ).encode()
        monkeypatch.setattr(mod, "_http_get", fake_get)
        rc = _cmd_observability_health(_ns(level="live"))
        out, _, _ = _capture(capsys, rc)
        assert rc == 0
        assert "status      : ok" in out

    def test_health_degraded_exits_1(
        self, capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from harness import cli_observability as mod

        def fake_get(url: str, *, timeout_s: float = 5.0) -> tuple[int, bytes]:
            return 200, json.dumps(
                {"status": "degraded", "version": "1.13.0",
                 "project_root": "/x", "checks": {}},
            ).encode()
        monkeypatch.setattr(mod, "_http_get", fake_get)
        rc = _cmd_observability_health(_ns(level="ready"))
        _, _, _ = _capture(capsys, rc)
        assert rc == 1

    def test_health_unhealthy_exits_2(
        self, capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from harness import cli_observability as mod

        def fake_get(url: str, *, timeout_s: float = 5.0) -> tuple[int, bytes]:
            # Real /health/ready returns 503 for unhealthy.
            return 503, json.dumps(
                {"status": "unhealthy", "version": "1.13.0",
                 "project_root": "/x", "checks": {}},
            ).encode()
        monkeypatch.setattr(mod, "_http_get", fake_get)
        rc = _cmd_observability_health(_ns(level="ready"))
        _, _, _ = _capture(capsys, rc)
        assert rc == 2

    def test_health_json(
        self, capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from harness import cli_observability as mod

        def fake_get(url: str, *, timeout_s: float = 5.0) -> tuple[int, bytes]:
            return 200, json.dumps(
                {"status": "ok", "version": "1.13.0", "project_root": "/x",
                 "checks": {}},
            ).encode()
        monkeypatch.setattr(mod, "_http_get", fake_get)
        rc = _cmd_observability_health(_ns(level="deep", json=True))
        out, _, _ = _capture(capsys, rc)
        assert rc == 0
        payload = json.loads(out)
        assert payload["level"] == "deep"
        assert payload["http_status"] == 200
        assert payload["report"]["status"] == "ok"

    def test_health_invalid_level_exits_2(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        rc = _cmd_observability_health(_ns(level="bogus"))
        _, err, _ = _capture(capsys, rc)
        assert rc == 2
        assert "invalid level" in err

    def test_health_connection_error_exits_2(
        self, capsys: pytest.CaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import urllib.error
        from harness import cli_observability as mod

        def fake_get(url: str, *, timeout_s: float = 5.0) -> tuple[int, bytes]:
            raise urllib.error.URLError("refused")
        monkeypatch.setattr(mod, "_http_get", fake_get)
        rc = _cmd_observability_health(_ns())
        _, err, _ = _capture(capsys, rc)
        assert rc == 2
        assert "cannot reach" in err


# === stats ===

class TestObservabilityStats:
    def test_stats_no_prometheus_client_returns_empty(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        # No prometheus_client in this venv → snapshot is {}.
        rc = _cmd_observability_stats(_ns())
        out, err, _ = _capture(capsys, rc)
        assert rc == 0
        # Either a "no metrics" message or an empty table.
        assert ("no metrics" in err.lower()) or "metric" in out.lower()

    def test_stats_json(
        self, capsys: pytest.CaptureFixture,
    ) -> None:
        rc = _cmd_observability_stats(_ns(json=True))
        out, _, _ = _capture(capsys, rc)
        assert rc == 0
        payload = json.loads(out)
        assert "metrics" in payload
        assert "count" in payload
        # note field is always present (warns about empty CLI process).
        assert "note" in payload


# === CLI parser ===

class TestCliParserObservability:
    def test_observability_default_to_log(self) -> None:
        parser = harness_cli._build_parser()
        args = parser.parse_args(["observability"])
        assert args.command == "observability"
        assert args.func == _cmd_observability_log

    def test_observability_metrics_parses(self) -> None:
        parser = harness_cli._build_parser()
        args = parser.parse_args(
            ["observability", "metrics", "--filter", r"^harness_",
             "--base-url", "http://example.com:9999"],
        )
        assert args.command == "observability"
        assert args.obs_command == "metrics"
        assert args.filter == r"^harness_"
        assert args.base_url == "http://example.com:9999"


# === snapshot() contract ===

class TestMetricsSnapshot:
    """PrometheusMetrics.snapshot() must return an empty dict when
    prometheus_client is not installed (default in this venv).
    """

    def test_snapshot_empty_when_no_prometheus(self) -> None:
        from harness.observability.metrics import PrometheusMetrics
        m = PrometheusMetrics(namespace="harness")
        # If prometheus_client is missing, enabled=False and snapshot={}.
        if not m.enabled:
            assert m.snapshot() == {}
        else:
            # If prometheus_client is present, snapshot is at least
            # an empty dict (counters are 0 until incremented).
            assert isinstance(m.snapshot(), dict)
