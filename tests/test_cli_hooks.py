"""Phase 4.4 v1.13.0: tests for the ``harness hooks`` CLI subcommand.

Covers:
  - ``harness hooks list`` — empty project, list of 7 builtins,
    filter by --event / --transport / --enabled / --disabled, --json.
  - ``harness hooks show <id>`` — found, not-found, --json.
  - ``harness hooks status`` — exists/does-not-exist, project errors.
  - Project file with a valid HookSpec and one with malformed JSON.
  - CLI subcommand parsing (no subcommand defaults to list).
  - Authorization header redaction (transport=http).

Strategy: invoke the subcommand handlers directly (no subprocess) to
keep the tests fast and inspectable. We do NOT use ``subprocess`` —
that's a layer the user can add if they want smoke tests of the
console script.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterator

import pytest

from harness import cli as harness_cli
from harness.cli_hooks import (
    _cmd_hooks_list,
    _cmd_hooks_show,
    _cmd_hooks_status,
)
from harness.hooks.registry import reset_registry


@pytest.fixture(autouse=True)
def _reset_singletons() -> Iterator[None]:
    """Reset the HookRegistry singleton before/after each test."""
    reset_registry()
    yield
    reset_registry()


def _ns(
    *,
    project_root: str | None = None,
    json: bool = False,  # noqa: A002 — mirror argparse dest
    event: str | None = None,
    transport: str | None = None,
    enabled_flag: str | None = None,
    hook_id: str | None = None,
) -> argparse.Namespace:
    """Build a minimal argparse.Namespace mirroring the parser's fields."""
    return argparse.Namespace(
        project_root=project_root,
        json=json,
        event=event,
        transport=transport,
        enabled_flag=enabled_flag,
        hook_id=hook_id,
    )


def _capture(capsys: pytest.CaptureFixture, rc: int) -> tuple[str, str, int]:
    """Return (stdout, stderr, returncode) for inspection."""
    out = capsys.readouterr()
    return out.out, out.err, rc


# === list ===

class TestHooksListBuiltins:
    def test_lists_seven_builtin_hooks(
        self, capsys: pytest.CaptureFixture, tmp_path: Path,
    ) -> None:
        rc = _cmd_hooks_list(_ns(project_root=str(tmp_path)))
        out, err, _ = _capture(capsys, rc)
        assert rc == 0, err
        # All 7 builtin hook_ids must be present.
        for name in (
            "builtin.log", "builtin.validate", "builtin.block_dangerous",
            "builtin.inject_context", "builtin.autosave",
            "builtin.confirm_dangerous", "builtin.notify_terminal",
        ):
            assert name in out, f"{name} missing from list output"

    def test_event_filter(
        self, capsys: pytest.CaptureFixture, tmp_path: Path,
    ) -> None:
        rc = _cmd_hooks_list(_ns(project_root=str(tmp_path), event="PreToolUse"))
        out, err, _ = _capture(capsys, rc)
        assert rc == 0, err
        assert "PreToolUse" in out
        # Elicitation event is NOT PreToolUse; builtin.confirm_dangerous
        # should be filtered out.
        assert "builtin.confirm_dangerous" not in out

    def test_transport_filter(
        self, capsys: pytest.CaptureFixture, tmp_path: Path,
    ) -> None:
        rc = _cmd_hooks_list(
            _ns(project_root=str(tmp_path), transport="builtin"),
        )
        out, err, _ = _capture(capsys, rc)
        assert rc == 0, err
        # The transport column shows "builtin" for the 7 builtin rows.
        # Verify via the JSON path for a deterministic assertion.
        rc = _cmd_hooks_list(
            _ns(project_root=str(tmp_path), transport="builtin", json=True),
        )
        out_json, _, _ = _capture(capsys, rc)
        assert rc == 0
        payload = json.loads(out_json)
        transports = {r["transport"] for r in payload["hooks"]}
        assert transports == {"builtin"}, f"unexpected transports: {transports}"

    def test_enabled_disabled_filters(
        self, capsys: pytest.CaptureFixture, tmp_path: Path,
    ) -> None:
        # All builtins are enabled=True, so --disabled returns empty.
        rc = _cmd_hooks_list(
            _ns(project_root=str(tmp_path), enabled_flag="no"),
        )
        out, _, _ = _capture(capsys, rc)
        assert rc == 0
        assert "no hooks" in out

        # --enabled shows all 7 builtins.
        rc = _cmd_hooks_list(
            _ns(project_root=str(tmp_path), enabled_flag="yes"),
        )
        out, _, _ = _capture(capsys, rc)
        assert rc == 0
        assert "builtin.log" in out

    def test_json_output_wraps_in_dict(
        self, capsys: pytest.CaptureFixture, tmp_path: Path,
    ) -> None:
        rc = _cmd_hooks_list(_ns(project_root=str(tmp_path), json=True))
        out, _, _ = _capture(capsys, rc)
        assert rc == 0
        payload = json.loads(out)
        assert isinstance(payload, dict)
        assert "hooks" in payload
        assert "count" in payload
        assert "errors" in payload
        assert payload["count"] == 7
        # Each row has the required fields.
        for row in payload["hooks"]:
            assert "hook_id" in row
            assert "event" in row
            assert "transport" in row
            assert "source" in row
            assert row["source"] == "builtin"


# === project files ===

class TestProjectHooks:
    def test_project_spec_loaded(
        self, capsys: pytest.CaptureFixture, tmp_path: Path,
    ) -> None:
        hooks_dir = tmp_path / ".harness" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "pre-tool.json").write_text(
            json.dumps(
                {
                    "hook_id": "user.pre-tool-1",
                    "event": "PreToolUse",
                    "transport": "subprocess",
                    "script_path": "/usr/local/bin/check.sh",
                    "matcher": "tool_name=bash",
                    "timeout_ms": 1500,
                    "enabled": True,
                    "priority": 50,
                }
            ),
            encoding="utf-8",
        )
        rc = _cmd_hooks_list(_ns(project_root=str(tmp_path), json=True))
        out, _, _ = _capture(capsys, rc)
        assert rc == 0
        payload = json.loads(out)
        # 7 builtins + 1 project = 8
        assert payload["count"] == 8
        proj_rows = [r for r in payload["hooks"] if r.get("source") == "project"]
        assert len(proj_rows) == 1
        assert proj_rows[0]["hook_id"] == "user.pre-tool-1"
        assert proj_rows[0]["script_path"] == "/usr/local/bin/check.sh"
        assert proj_rows[0]["priority"] == 50
        assert proj_rows[0]["file"] == "pre-tool.json"

    def test_malformed_project_file_does_not_crash(
        self, capsys: pytest.CaptureFixture, tmp_path: Path,
    ) -> None:
        hooks_dir = tmp_path / ".harness" / "hooks"
        hooks_dir.mkdir(parents=True)
        # Bad JSON.
        (hooks_dir / "bad.json").write_text(
            "{ this is not valid JSON }", encoding="utf-8",
        )
        # Valid one.
        (hooks_dir / "ok.json").write_text(
            json.dumps(
                {
                    "hook_id": "user.ok-1",
                    "event": "PostToolUse",
                    "transport": "subprocess",
                    "script_path": "/tmp/x.sh",
                }
            ),
            encoding="utf-8",
        )
        rc = _cmd_hooks_list(_ns(project_root=str(tmp_path), json=True))
        out, err, _ = _capture(capsys, rc)
        assert rc == 0
        payload = json.loads(out)
        # Errors are reported in the JSON payload.
        assert any("bad.json" in e["file"] for e in payload["errors"])
        # Valid file's hook is still present.
        assert any(r.get("hook_id") == "user.ok-1" for r in payload["hooks"])


# === show ===

class TestHooksShow:
    def test_show_builtin(
        self, capsys: pytest.CaptureFixture, tmp_path: Path,
    ) -> None:
        rc = _cmd_hooks_show(
            _ns(project_root=str(tmp_path), hook_id="builtin.log"),
        )
        out, _, _ = _capture(capsys, rc)
        assert rc == 0
        assert "hook_id : builtin.log" in out
        assert "event   : PreToolUse" in out
        assert "callable: log_hook" in out

    def test_show_not_found(
        self, capsys: pytest.CaptureFixture, tmp_path: Path,
    ) -> None:
        rc = _cmd_hooks_show(
            _ns(project_root=str(tmp_path), hook_id="does.not.exist"),
        )
        out, err, _ = _capture(capsys, rc)
        assert rc == 1
        assert "not found" in err

    def test_show_json(
        self, capsys: pytest.CaptureFixture, tmp_path: Path,
    ) -> None:
        rc = _cmd_hooks_show(
            _ns(
                project_root=str(tmp_path),
                hook_id="builtin.confirm_dangerous",
                json=True,
            ),
        )
        out, _, _ = _capture(capsys, rc)
        assert rc == 0
        payload = json.loads(out)
        assert payload["found"] is True
        assert payload["hook"]["event"] == "Elicitation"

    def test_show_no_hook_id_exits_2(
        self, capsys: pytest.CaptureFixture, tmp_path: Path,
    ) -> None:
        rc = _cmd_hooks_show(_ns(project_root=str(tmp_path), hook_id=None))
        _, err, _ = _capture(capsys, rc)
        assert rc == 2
        assert "hook_id is required" in err

    def test_show_http_redacts_authorization(
        self, capsys: pytest.CaptureFixture, tmp_path: Path,
    ) -> None:
        hooks_dir = tmp_path / ".harness" / "hooks"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "http.json").write_text(
            json.dumps(
                {
                    "hook_id": "user.http-1",
                    "event": "PreToolUse",
                    "transport": "http",
                    "url": "https://example.com/hook",
                    "headers": {"Authorization": "Bearer secret-token-xyz"},
                }
            ),
            encoding="utf-8",
        )
        rc = _cmd_hooks_show(
            _ns(project_root=str(tmp_path), hook_id="user.http-1", json=True),
        )
        out, _, _ = _capture(capsys, rc)
        assert rc == 0
        payload = json.loads(out)
        headers = payload["hook"]["headers"]
        assert headers["Authorization"].startswith("Bearer")
        # The secret part must be masked.
        assert "secret-token-xyz" not in headers["Authorization"]


# === status ===

class TestHooksStatus:
    def test_status_no_project_dir(
        self, capsys: pytest.CaptureFixture, tmp_path: Path,
    ) -> None:
        rc = _cmd_hooks_status(_ns(project_root=str(tmp_path)))
        out, _, _ = _capture(capsys, rc)
        assert rc == 0
        assert "exists        : False" in out
        assert "total_specs   : 7" in out  # builtins only

    def test_status_json(
        self, capsys: pytest.CaptureFixture, tmp_path: Path,
    ) -> None:
        rc = _cmd_hooks_status(_ns(project_root=str(tmp_path), json=True))
        out, _, _ = _capture(capsys, rc)
        assert rc == 0
        payload = json.loads(out)
        assert payload["hooks_dir_exists"] is False
        assert payload["builtin_specs"] == 7
        assert payload["project_specs"] == 0


# === CLI parser ===

class TestCliParser:
    def test_parser_has_hooks_subcommand(self) -> None:
        parser = harness_cli._build_parser()
        # Parse ``harness hooks list`` to verify the subcommand is wired.
        args = parser.parse_args(["hooks", "list"])
        assert args.command == "hooks"
        assert args.hooks_command == "list"
        assert args.func == _cmd_hooks_list

    def test_hooks_default_to_list(self) -> None:
        """``harness hooks`` with no subcommand falls back to list."""
        parser = harness_cli._build_parser()
        args = parser.parse_args(["hooks"])
        assert args.command == "hooks"
        # No subcommand → default to list (per set_defaults).
        assert args.func == _cmd_hooks_list

    def test_parser_has_observability_subcommand(self) -> None:
        parser = harness_cli._build_parser()
        args = parser.parse_args(["observability", "metrics"])
        assert args.command == "observability"
        assert args.obs_command == "metrics"


# === Trust boundary ===

class TestTrustBoundary:
    """The new CLI modules must NOT import harness.agents or harness.server.

    Trust boundary applies to the module SOURCE (what the module
    itself imports). We can't check ``sys.modules`` globally
    because the harness test suite legitimately imports
    ``harness.server`` elsewhere (it is allowed in the CLI
    process — only the *observability* and *hooks* packages are
    locked to stdlib).
    """

    def test_cli_hooks_source_has_no_production_imports(self) -> None:
        import harness.cli_hooks as mod
        src = Path(mod.__file__).read_text(encoding="utf-8")
        # We do allow ``harness.config`` (settings) and
        # ``harness.hooks`` (the inspected packages). Forbid only
        # the production runtime modules.
        for forbidden in (
            "harness.agents",
            "harness.server",
        ):
            assert f"import {forbidden}" not in src, (
                f"cli_hooks has hard import of {forbidden}"
            )
            assert f"from {forbidden}" not in src, (
                f"cli_hooks has hard import of {forbidden}"
            )

    def test_cli_observability_source_has_no_production_imports(self) -> None:
        import harness.cli_observability as mod
        src = Path(mod.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "harness.agents",
            "harness.server",
        ):
            assert f"import {forbidden}" not in src, (
                f"cli_observability has hard import of {forbidden}"
            )
            assert f"from {forbidden}" not in src, (
                f"cli_observability has hard import of {forbidden}"
            )
