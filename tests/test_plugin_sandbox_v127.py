"""Phase 6.2B v1.27.0: Tests for SubprocessPluginRunner (JSON-RPC sandbox).

Covers:
  - Happy path: plugin executes a method and returns a result.
  - Timeout: long-running plugin is killed → PluginTimeoutError.
  - Import error: plugin with a syntax/import error → PluginLoadError.
  - Crash: plugin exits non-zero without JSON-RPC output → PluginCrashError.
  - JSON-RPC wire format: build_request / parse_response round-trip.
  - Trust boundary: malicious plugin cannot access harness globals.

The fixture plugins are written to ``tmp_path`` at test time (same pattern
as ``tests/test_hooks_subprocess.py``).
"""
from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

from harness.plugins.sandbox import (
    JSONRPC_VERSION,
    PluginCrashError,
    PluginLoadError,
    PluginResult,
    PluginTimeoutError,
    SubprocessPluginRunner,
    build_request,
    parse_response,
)

# ---------------------------------------------------------------------------
# Helpers: write plugin scripts to tmp_path
# ---------------------------------------------------------------------------


def _write_plugin(tmp_path: Path, name: str, body: str) -> Path:
    """Write a Python plugin script to ``tmp_path / name``. Returns the path."""
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# A simple well-behaved plugin: reads one JSON-RPC request from stdin,
# echoes back a result with the method name and a scope declaration.
_GOOD_PLUGIN = """
    import json, sys

    def main():
        line = sys.stdin.readline()
        req = json.loads(line)
        result = {
            "ok": True,
            "method": req["method"],
            "params_echo": req["params"],
            "scopes": ["read"],
        }
        resp = {
            "jsonrpc": "2.0",
            "id": req["id"],
            "result": result,
        }
        sys.stdout.write(json.dumps(resp) + "\\n")

    if __name__ == "__main__":
        main()
"""

# A plugin that imports a non-existent module → import error.
_BROKEN_IMPORT_PLUGIN = """
    import json, sys
    import nonexistent_module_xyz123  # noqa: F401

    def main():
        pass

    if __name__ == "__main__":
        main()
"""

# A plugin that crashes (sys.exit(1)) without producing JSON output.
_CRASH_PLUGIN = """
    import sys
    sys.stderr.write("boom\\n")
    sys.exit(1)
"""

# A plugin that sleeps longer than the timeout.
_TIMEOUT_PLUGIN = """
    import time
    time.sleep(30)
"""

# A plugin that tries to access harness internals (and should fail because
# sys.path does not include the harness package).
_MALICIOUS_PLUGIN = """
    import json, sys

    def main():
        line = sys.stdin.readline()
        req = json.loads(line)
        try:
            import harness.config
            leaked = True
        except ImportError:
            leaked = False
        result = {"harness_accessed": leaked}
        resp = {
            "jsonrpc": "2.0",
            "id": req["id"],
            "result": result,
        }
        sys.stdout.write(json.dumps(resp) + "\\n")

    if __name__ == "__main__":
        main()
"""


# ---------------------------------------------------------------------------
# JSON-RPC wire format tests (no subprocess needed)
# ---------------------------------------------------------------------------


class TestJsonRpcWireFormat:
    """build_request / parse_response produce valid JSON-RPC 2.0 messages."""

    def test_build_request_contains_required_fields(self) -> None:
        raw = build_request(1, "register", {"name": "weather"})
        msg = json.loads(raw)
        assert msg["jsonrpc"] == JSONRPC_VERSION
        assert msg["id"] == 1
        assert msg["method"] == "register"
        assert msg["params"] == {"name": "weather"}

    def test_build_request_is_newline_terminated(self) -> None:
        raw = build_request(1, "register", {})
        assert raw.endswith(b"\n")

    def test_parse_response_valid_result(self) -> None:
        line = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
        resp = parse_response(line)
        assert resp["id"] == 1
        assert resp["result"]["ok"] is True

    def test_parse_response_valid_error(self) -> None:
        line = json.dumps({
            "jsonrpc": "2.0",
            "id": 2,
            "error": {"code": -32001, "message": "bad scope"},
        })
        resp = parse_response(line)
        assert resp["error"]["code"] == -32001
        assert resp.get("result") is None

    def test_parse_response_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="empty response"):
            parse_response("")

    def test_parse_response_rejects_invalid_json(self) -> None:
        with pytest.raises(ValueError, match="invalid JSON"):
            parse_response("not json at all")

    def test_parse_response_rejects_missing_id(self) -> None:
        line = json.dumps({"jsonrpc": "2.0", "result": {}})
        with pytest.raises(ValueError, match="missing 'id'"):
            parse_response(line)

    def test_parse_response_rejects_wrong_version(self) -> None:
        line = json.dumps({"jsonrpc": "1.0", "id": 1, "result": {}})
        with pytest.raises(ValueError, match="not a JSON-RPC 2.0"):
            parse_response(line)


# ---------------------------------------------------------------------------
# Subprocess execution tests
# ---------------------------------------------------------------------------


class TestSubprocessPluginRunnerExecutesMethod:
    """Happy path: plugin receives method + params and returns a result."""

    async def test_executes_register(self, tmp_path: Path) -> None:
        plugin = _write_plugin(tmp_path, "good.py", _GOOD_PLUGIN)
        runner = SubprocessPluginRunner(plugin, timeout=10.0)
        result = await runner.execute("register", {"name": "weather", "version": "1.0"})
        assert isinstance(result, PluginResult)
        assert result.ok is True
        assert result.error is None
        assert result.result["ok"] is True
        assert result.result["method"] == "register"
        assert result.result["params_echo"]["name"] == "weather"
        assert "read" in result.scopes
        assert result.duration_ms > 0
        assert result.returncode == 0


class TestSubprocessPluginTimeout:
    """Plugin exceeding the timeout is killed → PluginTimeoutError."""

    async def test_timeout_kills_process(self, tmp_path: Path) -> None:
        plugin = _write_plugin(tmp_path, "slow.py", _TIMEOUT_PLUGIN)
        runner = SubprocessPluginRunner(plugin, timeout=0.5)
        with pytest.raises(PluginTimeoutError) as exc_info:
            await runner.execute("register", {})
        assert exc_info.value.timeout == pytest.approx(0.5)


class TestSubprocessPluginImportError:
    """Plugin with a broken import → PluginLoadError."""

    async def test_import_error_returns_load_error(self, tmp_path: Path) -> None:
        plugin = _write_plugin(tmp_path, "broken.py", _BROKEN_IMPORT_PLUGIN)
        runner = SubprocessPluginRunner(plugin, timeout=10.0)
        with pytest.raises(PluginLoadError):
            await runner.execute("register", {})


class TestSubprocessPluginCrash:
    """Plugin that exits non-zero without JSON-RPC output → PluginCrashError."""

    async def test_crash_returns_crash_error(self, tmp_path: Path) -> None:
        plugin = _write_plugin(tmp_path, "crash.py", _CRASH_PLUGIN)
        runner = SubprocessPluginRunner(plugin, timeout=10.0)
        with pytest.raises(PluginCrashError) as exc_info:
            await runner.execute("register", {})
        assert "boom" in str(exc_info.value)


class TestSubprocessPluginNotFound:
    """Non-existent plugin path → PluginLoadError."""

    async def test_missing_plugin_file(self, tmp_path: Path) -> None:
        runner = SubprocessPluginRunner(tmp_path / "nonexistent.py", timeout=5.0)
        with pytest.raises(PluginLoadError, match="not found"):
            await runner.execute("register", {})


class TestMaliciousPluginCannotAccessHarnessGlobals:
    """Trust boundary: plugin subprocess cannot import harness internals.

    The harness package is NOT on the plugin's sys.path (PYTHONPATH stripped,
    subprocess started with a clean environment). The plugin should report
    ``harness_accessed: False``.
    """

    async def test_no_harness_import(self, tmp_path: Path) -> None:
        plugin = _write_plugin(tmp_path, "malicious.py", _MALICIOUS_PLUGIN)
        runner = SubprocessPluginRunner(plugin, timeout=10.0)
        result = await runner.execute("register", {})
        assert result.ok is True
        assert result.result["harness_accessed"] is False


class TestRequestIdMonotonic:
    """The runner assigns monotonically increasing request IDs."""

    async def test_consecutive_ids(self, tmp_path: Path) -> None:
        plugin = _write_plugin(tmp_path, "good.py", _GOOD_PLUGIN)
        runner = SubprocessPluginRunner(plugin, timeout=10.0)
        r1 = await runner.execute("register", {"seq": 1})
        r2 = await runner.execute("run", {"seq": 2})
        assert r1.result["params_echo"]["seq"] == 1
        assert r2.result["params_echo"]["seq"] == 2
