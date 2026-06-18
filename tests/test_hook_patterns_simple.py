"""Phase 4.10 Task A: tests for 3 simple hook patterns.

Covers:
    - auto_format (subprocess, standalone script)
    - license_check (builtin, PreToolUse regex)
    - complexity_check (builtin, PostToolUse AST)

Trust boundary: builtin hooks use only stdlib (``re`` / ``ast``) and
``harness.hooks.context``. The auto_format script is stdlib + subprocess
only and must NOT import ``harness.*`` — ``test_auto_format_is_standalone``
asserts that statically.
"""
from __future__ import annotations

import ast
import json
import logging
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from harness.hooks import HookContext
from harness.hooks.builtin import complexity_check_hook, license_check_hook
from harness.hooks.builtin import complexity_check as cc_mod
from harness.hooks.builtin import license_check as lc_mod

# Path to the standalone auto_format script.
_AUTO_FORMAT = (
    Path(__file__).resolve().parent.parent
    / "harness"
    / "hooks"
    / "patterns"
    / "auto_format.py"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _post_ctx(
    tool_name: str = "write_file",
    *,
    path: str = "module.py",
    ok: bool = True,
    content: str | None = None,
) -> HookContext:
    arguments: dict[str, object] = {"path": path}
    if content is not None:
        arguments["content"] = content
    return HookContext(
        event="PostToolUse",
        session_id="s1",
        agent_id="",
        payload={"tool_name": tool_name, "arguments": arguments, "ok": ok},
    )


def _pre_ctx(
    tool_name: str = "write_file",
    *,
    content: str = "",
    path: str = "module.py",
) -> HookContext:
    return HookContext(
        event="PreToolUse",
        session_id="s1",
        agent_id="",
        payload={
            "tool_name": tool_name,
            "arguments": {"path": path, "content": content},
        },
    )


# ---------------------------------------------------------------------------
# 1. auto_format (subprocess script)
# ---------------------------------------------------------------------------


class TestAutoFormatSkips:
    """auto_format exits 0 (no-op) for non-matching inputs."""

    def test_auto_format_skips_non_py(self) -> None:
        """Non-.py path → no ruff invocation."""
        payload = {
            "event": "PostToolUse",
            "payload": {
                "tool_name": "write_file",
                "arguments": {"path": "README.md"},
                "ok": True,
            },
        }
        proc = subprocess.run(
            [sys.executable, str(_AUTO_FORMAT)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 0

    def test_auto_format_skips_failed_tool(self) -> None:
        """ok=False → no ruff invocation."""
        payload = {
            "event": "PostToolUse",
            "payload": {
                "tool_name": "write_file",
                "arguments": {"path": "mod.py"},
                "ok": False,
            },
        }
        proc = subprocess.run(
            [sys.executable, str(_AUTO_FORMAT)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 0

    def test_auto_format_skips_non_write_tools(self) -> None:
        """read_file / bash → not formattable."""
        payload = {
            "event": "PostToolUse",
            "payload": {
                "tool_name": "read_file",
                "arguments": {"path": "mod.py"},
                "ok": True,
            },
        }
        proc = subprocess.run(
            [sys.executable, str(_AUTO_FORMAT)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 0


class TestAutoFormatRunsRuff:
    """When all conditions are met, ruff is invoked (mocked)."""

    def test_auto_format_runs_ruff(self) -> None:
        """For a successful .py write, subprocess.run(['ruff', ...]) is called."""
        # We invoke the script's main() directly with a mocked subprocess
        # to avoid requiring ruff on PATH in CI.
        import importlib.util

        spec = importlib.util.spec_from_file_location("auto_format", _AUTO_FORMAT)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        payload = {
            "event": "PostToolUse",
            "payload": {
                "tool_name": "write_file",
                "arguments": {"path": "good.py"},
                "ok": True,
            },
        }
        with mock.patch.object(mod.subprocess, "run") as fake_run:
            fake_run.return_value = mock.Mock(returncode=0)
            with mock.patch.object(mod.sys, "stdin") as fake_stdin:
                fake_stdin.read.return_value = json.dumps(payload)
                rc = mod.main()
        assert rc == 0
        fake_run.assert_called_once()
        called_args = fake_run.call_args
        # First positional arg is the argv list; [0] should be 'ruff'.
        argv = called_args.args[0]
        assert argv[0] == "ruff"
        assert "format" in argv
        assert "good.py" in argv

    def test_auto_format_failure_logged_not_raised(self) -> None:
        """If ruff raises / is missing, main() still returns 0."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "auto_format_fail", _AUTO_FORMAT
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        payload = {
            "event": "PostToolUse",
            "payload": {
                "tool_name": "write_file",
                "arguments": {"path": "broken.py"},
                "ok": True,
            },
        }
        with mock.patch.object(
            mod.subprocess, "run", side_effect=FileNotFoundError("no ruff")
        ):
            with mock.patch.object(mod.sys, "stdin") as fake_stdin:
                fake_stdin.read.return_value = json.dumps(payload)
                rc = mod.main()
        assert rc == 0  # never propagates

    def test_auto_format_is_standalone(self) -> None:
        """Statically verify the script has no ``harness.*`` imports."""
        source = _AUTO_FORMAT.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("harness."), (
                        f"auto_format.py imports {alias.name!r} — must be standalone"
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.startswith("harness."):
                    pytest.fail(
                        f"auto_format.py imports from {node.module!r} — must be standalone"
                    )


# ---------------------------------------------------------------------------
# 2. license_check (builtin)
# ---------------------------------------------------------------------------


class TestLicenseCheck:
    """license_check_hook blocks GPL-family imports, allows MIT-style code."""

    def setup_method(self) -> None:
        # Pin the forbidden set so the test is deterministic regardless of
        # the host's Settings.hooks_license_check_forbidden value.
        self._orig = lc_mod._FORBIDDEN_OVERRIDE
        lc_mod._FORBIDDEN_OVERRIDE = (
            "gpl3",
            "gpl-3",
            "agpl-3",
            "sspl",
            "commons-clause",
        )

    def teardown_method(self) -> None:
        lc_mod._FORBIDDEN_OVERRIDE = self._orig

    async def test_license_check_blocks_gpl(self) -> None:
        ctx = _pre_ctx(content="import gpl3_licensed_lib\n")
        decision = await license_check_hook(ctx)
        assert decision.decision == "block"
        assert decision.hook_id == "builtin.license_check"
        assert "forbidden" in decision.output.get("reason", "").lower()

    async def test_license_check_allows_mit(self) -> None:
        ctx = _pre_ctx(content="import requests\nimport numpy as np\n")
        decision = await license_check_hook(ctx)
        assert decision.decision == "allow"
        assert decision.hook_id == "builtin.license_check"

    async def test_license_check_skips_non_import_args(self) -> None:
        """A read-only tool with no text content → allow (nothing to scan)."""
        ctx = HookContext(
            event="PreToolUse",
            session_id="s",
            agent_id="",
            payload={"tool_name": "read_file", "arguments": {"path": "x.py"}},
        )
        decision = await license_check_hook(ctx)
        assert decision.decision == "allow"

    async def test_license_check_ignores_non_pre_tool_use(self) -> None:
        ctx = _post_ctx(content="import gpl3_thing\n")
        decision = await license_check_hook(ctx)
        assert decision.decision == "allow"


# ---------------------------------------------------------------------------
# 3. complexity_check (builtin)
# ---------------------------------------------------------------------------


_HIGH_COMPLEXITY_SRC = """
def big(a, b, c, d):
    if a:
        if b:
            for i in range(10):
                while c:
                    if d:
                        if i:
                            x = 1
                    elif i > 5:
                        x = 2
    return x
"""


_LOW_COMPLEXITY_SRC = "def simple(a, b):\n    return a + b\n"


class TestComplexityCheck:
    """complexity_check_hook warns on high-complexity code, never blocks."""

    def setup_method(self) -> None:
        self._orig = cc_mod._THRESHOLD_OVERRIDE
        cc_mod._THRESHOLD_OVERRIDE = 5  # deterministic, below _HIGH's count

    def teardown_method(self) -> None:
        cc_mod._THRESHOLD_OVERRIDE = self._orig

    async def test_complexity_check_warns_high_complexity(
        self, caplog
    ) -> None:
        ctx = _post_ctx(content=_HIGH_COMPLEXITY_SRC)
        with caplog.at_level(
            logging.WARNING, logger="harness.hooks.builtin.complexity_check"
        ):
            decision = await complexity_check_hook(ctx)
        assert decision.decision == "allow"  # advisory, never blocks
        assert decision.hook_id == "builtin.complexity_check"
        assert any("complexity" in r.message for r in caplog.records)

    async def test_complexity_check_skips_low_complexity(
        self, caplog
    ) -> None:
        ctx = _post_ctx(content=_LOW_COMPLEXITY_SRC)
        with caplog.at_level(
            logging.WARNING, logger="harness.hooks.builtin.complexity_check"
        ):
            decision = await complexity_check_hook(ctx)
        assert decision.decision == "allow"
        # No warning emitted for a trivial function.
        assert not any(
            "complexity" in r.message and "threshold" in r.message
            for r in caplog.records
        )

    async def test_complexity_check_uses_ast_handles_syntax_error(
        self, caplog
    ) -> None:
        """Malformed Python → SyntaxError swallowed → allow (no block)."""
        ctx = _post_ctx(content="def broken(:\n    pass\n")
        with caplog.at_level(
            logging.WARNING, logger="harness.hooks.builtin.complexity_check"
        ):
            decision = await complexity_check_hook(ctx)
        assert decision.decision == "allow"
        # No crash, no warning about complexity (parse failed early).
        assert not any(
            "has complexity" in r.message for r in caplog.records
        )

    async def test_complexity_check_ignores_non_text_tools(
        self, caplog
    ) -> None:
        """Tools other than write_file/edit_file are ignored."""
        ctx = _post_ctx(tool_name="read_file", content=_HIGH_COMPLEXITY_SRC)
        with caplog.at_level(
            logging.WARNING, logger="harness.hooks.builtin.complexity_check"
        ):
            decision = await complexity_check_hook(ctx)
        assert decision.decision == "allow"
        assert not any(
            "has complexity" in r.message for r in caplog.records
        )
