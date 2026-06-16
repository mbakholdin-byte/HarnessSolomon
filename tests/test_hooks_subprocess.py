"""Phase 4.0: Tests for subprocess transport (JSON via stdin/stdout)."""
from __future__ import annotations

import json
import os
import sys
import textwrap

import pytest

from harness.hooks.context import HookContext
from harness.hooks.subprocess import invoke_subprocess_hook


def _write_hook(tmp_path, body: str) -> str:
    """Write a Python hook script to a temp file. Returns the path."""
    path = tmp_path / "hook.py"
    path.write_text(body, encoding="utf-8")
    return str(path)


@pytest.fixture
def ctx() -> HookContext:
    return HookContext(
        event="PreToolUse",
        session_id="s1",
        agent_id="",
        payload={"tool_name": "read_file", "args": "test.txt"},
    )


class TestSubprocessAllow:
    """Subprocess returns decision='allow' on exit 0 + JSON."""

    async def test_simple_allow(self, tmp_path, ctx) -> None:
        body = textwrap.dedent(
            """
            import json, sys
            data = json.loads(sys.stdin.read())
            sys.stdout.write(json.dumps({"decision": "allow", "output": {}}))
            """
        )
        path = _write_hook(tmp_path, body)
        d = await invoke_subprocess_hook(path, ctx, timeout_ms=5000)
        assert d.decision == "allow"
        assert d.hook_id == "subprocess.hook.py"
        assert d.duration_ms > 0
        assert d.error == ""


class TestSubprocessBlock:
    """Subprocess returns decision='block' on exit 2."""

    async def test_exit_2_with_stderr(self, tmp_path, ctx) -> None:
        body = textwrap.dedent(
            """
            import sys
            sys.stderr.write("blocked by policy")
            sys.exit(2)
            """
        )
        path = _write_hook(tmp_path, body)
        d = await invoke_subprocess_hook(path, ctx, timeout_ms=5000)
        assert d.decision == "block"
        assert "blocked by policy" in d.output.get("reason", "")


class TestSubprocessModify:
    """Subprocess returns decision='modify' with JSON output."""

    async def test_modify_with_payload(self, tmp_path, ctx) -> None:
        body = textwrap.dedent(
            """
            import json, sys
            data = json.loads(sys.stdin.read())
            sys.stdout.write(json.dumps({
                "decision": "modify",
                "output": {"payload": {"new_key": "new_value"}}
            }))
            """
        )
        path = _write_hook(tmp_path, body)
        d = await invoke_subprocess_hook(path, ctx, timeout_ms=5000)
        assert d.decision == "modify"
        assert d.output == {"payload": {"new_key": "new_value"}}


class TestSubprocessErrors:
    """Subprocess errors fail open (decision=allow + error populated)."""

    async def test_missing_script(self, ctx) -> None:
        d = await invoke_subprocess_hook(
            "/nonexistent/hook.py", ctx, timeout_ms=5000
        )
        assert d.decision == "allow"
        assert "not found" in d.error

    async def test_nonzero_exit_no_block(self, tmp_path, ctx) -> None:
        body = textwrap.dedent(
            """
            import sys
            sys.exit(1)
            """
        )
        path = _write_hook(tmp_path, body)
        d = await invoke_subprocess_hook(path, ctx, timeout_ms=5000)
        assert d.decision == "allow"
        assert "exited with code 1" in d.error

    async def test_invalid_json_stdout(self, tmp_path, ctx) -> None:
        body = textwrap.dedent(
            """
            import sys
            sys.stdout.write("not valid json")
            """
        )
        path = _write_hook(tmp_path, body)
        d = await invoke_subprocess_hook(path, ctx, timeout_ms=5000)
        assert d.decision == "allow"
        assert "invalid JSON" in d.error

    async def test_empty_stdout(self, tmp_path, ctx) -> None:
        body = textwrap.dedent(
            """
            import sys
            # exit 0 with no stdout
            pass
            """
        )
        path = _write_hook(tmp_path, body)
        d = await invoke_subprocess_hook(path, ctx, timeout_ms=5000)
        assert d.decision == "allow"
        assert "empty stdout" in d.error


class TestSubprocessTimeout:
    """Subprocess exceeding timeout_ms is killed and fails open."""

    async def test_timeout_kills_subprocess(self, tmp_path, ctx) -> None:
        body = textwrap.dedent(
            """
            import time, sys
            time.sleep(10)
            """
        )
        path = _write_hook(tmp_path, body)
        d = await invoke_subprocess_hook(path, ctx, timeout_ms=200)
        assert d.decision == "allow"  # fail-open
        assert "timeout" in d.error


class TestSubprocessWireFormat:
    """Subprocess receives a well-formed JSON context on stdin."""

    async def test_receives_full_context(self, tmp_path) -> None:
        body = textwrap.dedent(
            """
            import json, sys
            data = json.loads(sys.stdin.read())
            assert data["event"] == "PreToolUse"
            assert data["session_id"] == "s-1"
            assert data["agent_id"] == "a-1"
            assert data["payload"]["tool_name"] == "write_file"
            assert data["recursion_depth"] == 0
            sys.stdout.write(json.dumps({"decision": "allow", "output": {}}))
            """
        )
        path = _write_hook(tmp_path, body)
        ctx = HookContext(
            event="PreToolUse",
            session_id="s-1",
            agent_id="a-1",
            payload={"tool_name": "write_file"},
        )
        d = await invoke_subprocess_hook(path, ctx, timeout_ms=5000)
        assert d.decision == "allow"
        assert d.error == ""
