"""Phase 6.2B v1.27.0: Subprocess sandbox for plugin execution.

Runs a plugin in an isolated Python subprocess. Communication is JSON-RPC 2.0
over stdin/stdout (one request, one response per ``execute()`` call).

Trust boundary (CRITICAL)
-------------------------

The plugin subprocess is started with a clean environment:

* ``sys.path`` is NOT inherited from the harness. The plugin receives only
  stdlib + its own declared dependencies (resolved by the plugin's own venv
  or the system interpreter, NOT the harness venv).
* The plugin's stdin sees ONLY the JSON-RPC request (method name + params dict).
* The plugin's stdout is parsed as a single JSON-RPC response.
* There is NO shared memory, NO shared imports, NO access to harness globals.

Resource limits
---------------

* **Memory** (Unix only): ``resource.setrlimit(RLIMIT_AS, ...)`` caps the
  plugin's virtual address space. On Windows the limit is best-effort —
  we rely on the wall-clock timeout as the primary guard.
* **Wall clock**: ``asyncio.wait_for`` enforces ``timeout`` seconds.
  On timeout the process is killed (SIGKILL on Unix, TerminateProcess on
  Windows) and :class:`PluginTimeoutError` is raised.
* **CPU**: not limited (the wall-clock timeout is the backstop).

Permission model
----------------

The plugin declares required scopes in the response to the ``register`` call
(``result.scopes``). The harness validates these against
:attr:`Settings.plugins_allowed` (a whitelist of plugin stems).

Error taxonomy
--------------

* :class:`PluginLoadError` — plugin failed to import or register.
* :class:`PluginCrashError` — plugin process exited non-zero without a
  JSON-RPC response.
* :class:`PluginTimeoutError` — plugin exceeded ``timeout`` seconds.

Settings (reuse from 6.2A)
--------------------------

* ``plugins_enabled`` — master switch.
* ``plugins_dir`` — directory containing plugin ``.py`` files.
* ``plugins_allowed`` — whitelist of allowed plugin names (list[str]).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JSON-RPC constants
# ---------------------------------------------------------------------------

JSONRPC_VERSION = "2.0"

# Standard JSON-RPC error codes (subset relevant to plugins).
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Custom error codes (in the implementation-defined -32000..-32099 range).
PLUGIN_LOAD_ERROR = -32001
PLUGIN_CRASH_ERROR = -32002
PLUGIN_TIMEOUT_ERROR = -32003


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PluginError(Exception):
    """Base class for all plugin execution errors."""

    def __init__(self, message: str, *, plugin_path: str | Path = "", code: int = 0) -> None:
        super().__init__(message)
        self.plugin_path = str(plugin_path)
        self.code = code


class PluginLoadError(PluginError):
    """Plugin failed to import or register (missing module, syntax error, …)."""

    def __init__(self, message: str, *, plugin_path: str | Path = "") -> None:
        super().__init__(message, plugin_path=plugin_path, code=PLUGIN_LOAD_ERROR)


class PluginCrashError(PluginError):
    """Plugin process exited non-zero without a JSON-RPC response."""

    def __init__(self, message: str, *, plugin_path: str | Path = "") -> None:
        super().__init__(message, plugin_path=plugin_path, code=PLUGIN_CRASH_ERROR)


class PluginTimeoutError(PluginError):
    """Plugin exceeded the wall-clock timeout."""

    def __init__(self, message: str, *, plugin_path: str | Path = "", timeout: float = 0.0) -> None:
        super().__init__(message, plugin_path=plugin_path, code=PLUGIN_TIMEOUT_ERROR)
        self.timeout = timeout


# ---------------------------------------------------------------------------
# JSON-RPC message construction / parsing
# ---------------------------------------------------------------------------


def build_request(request_id: int, method: str, params: dict[str, Any]) -> bytes:
    """Build a JSON-RPC 2.0 request as UTF-8 bytes (single line, newline-terminated).

    Args:
        request_id: Monotonic request counter (1, 2, 3, …).
        method: The method name to invoke in the plugin.
        params: Parameters dict passed to the plugin method.

    Returns:
        ``b'{"jsonrpc":"2.0","id":1,"method":"register","params":{...}}\\n'``
    """
    msg = {"jsonrpc": JSONRPC_VERSION, "id": request_id, "method": method, "params": params}
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")


def parse_response(raw: str) -> dict[str, Any]:
    """Parse a JSON-RPC 2.0 response line.

    Args:
        raw: A single line read from the plugin's stdout.

    Returns:
        Dict with keys ``jsonrpc``, ``id``, and either ``result`` or ``error``.

    Raises:
        ValueError: If the line is not valid JSON or not a JSON-RPC response.
    """
    raw = raw.strip()
    if not raw:
        raise ValueError("empty response from plugin")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON from plugin: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"plugin response is not a JSON object: {type(data).__name__}")
    if data.get("jsonrpc") != JSONRPC_VERSION:
        raise ValueError(f"not a JSON-RPC 2.0 response: {data.get('jsonrpc')!r}")
    if "id" not in data:
        raise ValueError("JSON-RPC response missing 'id'")
    if "result" not in data and "error" not in data:
        raise ValueError("JSON-RPC response has neither 'result' nor 'error'")
    return data


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


@dataclass
class PluginResult:
    """Structured result of a single ``execute()`` call."""

    method: str
    result: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    duration_ms: float = 0.0
    returncode: int = 0

    @property
    def ok(self) -> bool:
        """True if the plugin returned a result (no error)."""
        return self.error is None

    @property
    def scopes(self) -> list[str]:
        """Scopes declared by the plugin in the ``register`` response."""
        return list(self.result.get("scopes", []))


class SubprocessPluginRunner:
    """Run a plugin in an isolated Python subprocess via JSON-RPC over stdin/stdout.

    Each ``execute()`` call:

    1. Starts a fresh subprocess: ``python <plugin_path>``.
    2. Writes a JSON-RPC request to stdin.
    3. Reads the JSON-RPC response from stdout (with timeout).
    4. Parses the result / error.
    5. On timeout → kills the subprocess → raises :class:`PluginTimeoutError`.

    The subprocess inherits a **clean** environment — no harness ``sys.path``,
    no harness globals. The only data crossing the boundary is the JSON-RPC
    request/response pair.

    Args:
        plugin_path: Path to the plugin ``.py`` file.
        timeout: Wall-clock timeout in seconds for the entire round-trip.
            Default 30.0.
        memory_limit_mb: Virtual address space cap in MiB (Unix only).
            Default 256. On Windows this is recorded but not enforced
            (best-effort).
    """

    def __init__(
        self,
        plugin_path: Path,
        timeout: float = 30.0,
        memory_limit_mb: int = 256,
    ) -> None:
        self.plugin_path = plugin_path
        self.timeout = timeout
        self.memory_limit_mb = memory_limit_mb
        self._next_id: int = 1

    async def execute(self, method: str, params: dict[str, Any]) -> PluginResult:
        """Send a JSON-RPC request to the plugin subprocess, return the result.

        Args:
            method: Method name to invoke (e.g. ``"register"``, ``"run"``).
            params: Parameters dict.

        Returns:
            :class:`PluginResult` with the plugin's response.

        Raises:
            PluginLoadError: Plugin failed to import or produced invalid output.
            PluginCrashError: Plugin exited non-zero without a valid response.
            PluginTimeoutError: Plugin exceeded ``timeout`` seconds.
        """
        request_id = self._next_id
        self._next_id += 1
        request_bytes = build_request(request_id, method, params)
        start = time.monotonic()

        # Pre-check: plugin file must exist.
        if not self.plugin_path.is_file():
            raise PluginLoadError(
                f"plugin not found: {self.plugin_path}",
                plugin_path=self.plugin_path,
            )

        # Build subprocess kwargs.
        creationflags = 0
        preexec_fn: Any = None
        if sys.platform == "win32":
            # CREATE_NO_WINDOW (0x08000000) prevents a console flash on Windows.
            creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        else:
            preexec_fn = self._unix_preexec

        proc: asyncio.subprocess.Process | None = None
        try:
            # v1.27.0 fix: 3-layer isolation to prevent plugin from accessing
            # harness internals via sys.path / cwd / .pth files.
            #
            # 1. ``python -I`` (isolated mode) — skips PYTHON* env vars, no
            #    user site-packages.
            # 2. ``python -S`` (no site) — don't prepend site-packages
            #    (which would include venv .pth files pointing at harness).
            # 3. ``cwd=tempdir`` — don't inherit parent's cwd (which may
            #    contain a 'harness/' package as Python module).
            # Without all 3, a malicious plugin can ``import harness.config``
            # and leak secrets.
            import tempfile
            _plugin_cwd = tempfile.mkdtemp(prefix="plugin_sandbox_")
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",  # isolated mode (skip PYTHON* env, no user site)
                "-S",  # don't prepend site-packages (skip .pth files)
                str(self.plugin_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=creationflags,
                preexec_fn=preexec_fn,
                env=self._build_env(),
                cwd=_plugin_cwd,
            )
        except (FileNotFoundError, OSError, PermissionError) as exc:
            raise PluginLoadError(
                f"cannot start plugin subprocess: {exc!r}",
                plugin_path=self.plugin_path,
            ) from exc

        assert proc is not None  # for type checker

        try:
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(input=request_bytes),
                    timeout=self.timeout,
                )
            except asyncio.TimeoutError:
                await self._kill(proc)
                raise PluginTimeoutError(
                    f"plugin {self.plugin_path.name} timed out after {self.timeout}s",
                    plugin_path=self.plugin_path,
                    timeout=self.timeout,
                ) from None

            duration_ms = (time.monotonic() - start) * 1000.0
            returncode = proc.returncode if proc.returncode is not None else -1

            stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()

            # Empty stdout → crash or import error.
            if not stdout_text:
                if returncode != 0:
                    # Non-zero exit + no output. Distinguish import/load
                    # errors (stderr contains a Python traceback with
                    # ModuleNotFoundError / SyntaxError / ImportError)
                    # from generic crashes.
                    lower_err = stderr_text.lower()
                    if any(
                        marker in lower_err
                        for marker in (
                            "modulenotfounderror",
                            "importerror",
                            "syntaxerror",
                            "traceback (most recent call last)",
                        )
                    ):
                        raise PluginLoadError(
                            f"plugin failed to load (exit {returncode}): "
                            f"{stderr_text[:300]}",
                            plugin_path=self.plugin_path,
                        )
                    raise PluginCrashError(
                        f"plugin crashed (exit {returncode}): {stderr_text[:300]}",
                        plugin_path=self.plugin_path,
                    )
                # Exit 0 but empty stdout → load error (plugin likely
                # printed nothing because it failed silently).
                raise PluginLoadError(
                    f"plugin produced no output (exit {returncode}): {stderr_text[:300]}",
                    plugin_path=self.plugin_path,
                )

            # Parse JSON-RPC response.
            try:
                response = parse_response(stdout_text)
            except ValueError as exc:
                raise PluginLoadError(
                    f"invalid JSON-RPC response: {exc}",
                    plugin_path=self.plugin_path,
                ) from exc

            # Validate request/response id match.
            if response.get("id") != request_id:
                raise PluginLoadError(
                    f"JSON-RPC id mismatch: expected {request_id}, "
                    f"got {response.get('id')}",
                    plugin_path=self.plugin_path,
                )

            error_obj = response.get("error")
            result_obj = response.get("result", {})

            if error_obj is not None:
                # Plugin returned a JSON-RPC error.
                return PluginResult(
                    method=method,
                    result={},
                    error=dict(error_obj) if isinstance(error_obj, dict) else {"message": str(error_obj)},
                    duration_ms=duration_ms,
                    returncode=returncode,
                )

            return PluginResult(
                method=method,
                result=dict(result_obj) if isinstance(result_obj, dict) else {"value": result_obj},
                duration_ms=duration_ms,
                returncode=returncode,
            )

        except (PluginTimeoutError,):
            raise
        except (PluginLoadError, PluginCrashError):
            raise
        except Exception as exc:  # noqa: BLE001
            # Unexpected error → treat as crash.
            raise PluginCrashError(
                f"unexpected error running plugin: {type(exc).__name__}: {exc}",
                plugin_path=self.plugin_path,
            ) from exc

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_env(self) -> dict[str, str]:
        """Build a clean environment for the subprocess.

        We strip harness-specific env vars (``HARNESS_*``) so the plugin
        cannot accidentally pick up harness configuration. Python's own
        ``PYTHONPATH`` is also cleared to prevent sys.path leakage.
        """
        env: dict[str, str] = {}
        for key, val in os.environ.items():
            if key.startswith("HARNESS_"):
                continue
            if key == "PYTHONPATH":
                continue
            env[key] = val
        # Ensure the plugin gets a clean sys.path.
        env.pop("PYTHONPATH", None)
        # v1.27.0 fix: PYTHONSAFEPATH prevents Python from prepending
        # the script's directory (or current dir) to sys.path. Without
        # this, malicious plugin could `import harness` if harness
        # is reachable via cwd/script-dir.
        env["PYTHONSAFEPATH"] = "1"
        return env

    def _unix_preexec(self) -> None:
        """``preexec_fn`` for Unix: set memory limit via ``resource.setrlimit``.

        Called in the child process after fork, before exec.
        """
        try:
            import resource

            limit_bytes = self.memory_limit_mb * 1024 * 1024
            # RLIMIT_AS = virtual address space. Soft + hard both set.
            resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, limit_bytes))
        except (ImportError, ValueError, OSError) as exc:  # noqa: BLE001
            logger.debug("Cannot set RLIMIT_AS for plugin: %s", exc)

    async def _kill(self, proc: asyncio.subprocess.Process) -> None:
        """Best-effort kill of the subprocess (and its children on Unix)."""
        try:
            if sys.platform == "win32":
                proc.terminate()
            else:
                import signal

                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)  # type: ignore[arg-type]
                except (ProcessLookupError, PermissionError):
                    proc.terminate()
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            try:
                proc.kill()
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass


__all__ = [
    "JSONRPC_VERSION",
    "PARSE_ERROR",
    "PLUGIN_CRASH_ERROR",
    "PLUGIN_LOAD_ERROR",
    "PLUGIN_TIMEOUT_ERROR",
    "PluginCrashError",
    "PluginError",
    "PluginLoadError",
    "PluginResult",
    "PluginTimeoutError",
    "SubprocessPluginRunner",
    "build_request",
    "parse_response",
]
