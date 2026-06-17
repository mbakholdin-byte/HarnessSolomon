"""Phase 4.7 v1.17.0: PermissionRequest wiring in 5 file-tools.

15 tests covering the new ``_resolve_permission_via_hook`` wiring in
``ToolRuntime._read_file`` / ``_write_file`` / ``_edit_file`` /
``_grep`` / ``_glob`` (Phase 4.7 v1.17.0). The wiring complements
the Phase 4.5 v1.15.0 wiring in ``_bash``.

Scope:
    * Denylist hits → ``deny`` with the documented error string.
    * Clean paths → ``allow`` (the tool proceeds normally).
    * Hook ``allow`` overrides a denylist deny.
    * Hook ``block`` forces deny even for clean paths.
    * Hook ``modify`` with ``permission_decision`` field.
    * Hook failure / no registered hooks → initial decision stands
      (fail-open, but explicit).
    * ``arguments_preview`` truncated to 200 chars.
    * Regression: ``_bash`` still calls ``PermissionRequest``.

The tests exercise ``ToolRuntime.execute(name, ...)`` end-to-end and
assert either that the tool ran (allow path) or that it was denied
with the documented error string (deny path).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

from harness.hooks.context import HookContext, HookDecision
from harness.hooks.events import EventType
from harness.hooks.registry import HookRegistry, HookSpec, reset_registry
from harness.hooks.runner import (
    HookRunner,
    set_global_hook_runner,
)
from harness.server.agent.runtime import (
    ToolRuntime,
    _match_read_denylist,
    _match_write_denylist,
)


# === Fixtures ===========================================================


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Empty project root under pytest's tmp_path."""
    root = tmp_path / "project"
    root.mkdir()
    return root


@pytest.fixture
def runtime(tmp_project: Path) -> ToolRuntime:
    """Plain ``ToolRuntime`` — no injected ``hook_runner``.

    Only ``PermissionRequest`` (which uses the global runner) is
    exercised. ``PreToolUse`` / ``PostToolUse`` do NOT fire.
    """
    return ToolRuntime(project_root=tmp_project, session_id="sess-test")


@pytest.fixture
def fresh_runner() -> Iterator[HookRunner]:
    """Bind a clean global ``HookRunner`` with an empty registry."""
    registry = HookRegistry()
    runner = HookRunner(registry, default_timeout_ms=500)
    set_global_hook_runner(runner)
    yield runner
    set_global_hook_runner(None)
    reset_registry()


@pytest.fixture(autouse=True)
def _reset_global_runner() -> Iterator[None]:
    """Ensure no leftover global runner leaks between tests."""
    set_global_hook_runner(None)
    reset_registry()
    yield
    set_global_hook_runner(None)
    reset_registry()


def _make_permission_hook(
    decision: str,
    *,
    override_payload: dict[str, Any] | None = None,
    seen: list[HookContext] | None = None,
    raise_exc: Exception | None = None,
):
    """Build a builtin PermissionRequest hook callable."""

    async def _hook(ctx: HookContext) -> HookDecision:
        if seen is not None:
            seen.append(ctx)
        if raise_exc is not None:
            raise raise_exc
        output: dict[str, Any] = {}
        if decision == "modify" and override_payload is not None:
            output = {"payload": override_payload}
        return HookDecision(decision=decision, hook_id="test.perm", output=output)

    return _hook


async def _register_permission_hook(
    runner: HookRunner,
    decision: str,
    *,
    override_payload: dict[str, Any] | None = None,
    seen: list[HookContext] | None = None,
    raise_exc: Exception | None = None,
) -> None:
    """Register a builtin PermissionRequest hook on ``runner``."""
    callable_ = _make_permission_hook(
        decision,
        override_payload=override_payload,
        seen=seen,
        raise_exc=raise_exc,
    )
    await runner._registry.register(  # noqa: SLF001 — test-only
        HookSpec(
            hook_id="test.permission_request",
            event=EventType.PERMISSION_REQUEST,
            transport="builtin",
            callable=callable_,
        )
    )


# === Unit: denylist helpers ============================================


def test_match_read_denylist_pyc() -> None:
    """Helper detects ``__pycache__/`` in the path."""
    assert _match_read_denylist("__pycache__/foo.pyc") == "__pycache__/"


def test_match_read_denylist_env() -> None:
    """Helper detects a ``.env`` suffix (case-insensitive)."""
    assert _match_read_denylist("prod.ENV") == ".env"


def test_match_write_denylist_superset_of_read() -> None:
    """Write denylist inherits the read patterns (no false negative)."""
    assert _match_write_denylist("config/secrets/token.key") is not None
    assert _match_write_denylist("bin/app.exe") == ".exe"
    # Read-only helper misses .exe, write helper catches it.
    assert _match_read_denylist("bin/app.exe") is None


def test_match_read_denylist_clean() -> None:
    """Clean path returns ``None`` (allow)."""
    assert _match_read_denylist("src/app/main.py") is None
    assert _match_read_denylist("README.md") is None


# === read_file: denylist denials =======================================


async def test_read_file_denied_on_pyc(runtime: ToolRuntime) -> None:
    """``__pycache__/foo.py`` is denied without any hook registered."""
    result = await runtime.execute(
        "read_file", {"path": "__pycache__/foo.py"}
    )
    assert not result.ok
    assert "denied" in result.error.lower()
    assert "__pycache__" in result.error


async def test_read_file_denied_on_env(runtime: ToolRuntime) -> None:
    """``.env`` is denied at the read boundary."""
    result = await runtime.execute("read_file", {"path": ".env"})
    assert not result.ok
    assert "denied" in result.error.lower()
    assert ".env" in result.error


async def test_read_file_denied_on_pem(runtime: ToolRuntime) -> None:
    """``secrets/cert.pem`` is denied (matches both ``secrets/`` and
    ``.pem``). The first match in the pattern tuple wins.
    """
    result = await runtime.execute(
        "read_file", {"path": "secrets/cert.pem"}
    )
    assert not result.ok
    assert "denied" in result.error.lower()


# === read_file: allow + normal read ====================================


async def test_read_file_allowed_normal(
    runtime: ToolRuntime, tmp_project: Path,
) -> None:
    """``README.md`` is allowed and the content is returned."""
    (tmp_project / "README.md").write_text("# hi\n", encoding="utf-8")
    result = await runtime.execute("read_file", {"path": "README.md"})
    assert result.ok, f"expected allow, got error={result.error!r}"
    assert "# hi" in result.output


# === write_file: denylist + allow ======================================


async def test_write_file_denied_on_binary(runtime: ToolRuntime) -> None:
    """``binary.exe`` is denied by the write denylist."""
    result = await runtime.execute(
        "write_file", {"path": "binary.exe", "content": "MZ..."}
    )
    assert not result.ok
    assert ".exe" in result.error


async def test_write_file_allowed_normal(
    runtime: ToolRuntime, tmp_project: Path,
) -> None:
    """``notes.md`` is allowed and the file is written."""
    result = await runtime.execute(
        "write_file", {"path": "notes.md", "content": "hello"}
    )
    assert result.ok, f"expected allow, got error={result.error!r}"
    assert (tmp_project / "notes.md").read_text(encoding="utf-8") == "hello"


# === edit_file: denylist ==============================================


async def test_edit_file_denied_on_env(
    runtime: ToolRuntime, tmp_project: Path,
) -> None:
    """``.env`` edit is denied by the write denylist (superset)."""
    (tmp_project / ".env").write_text("KEY=val\n", encoding="utf-8")
    result = await runtime.execute(
        "edit_file",
        {"path": ".env", "old_string": "KEY=val", "new_string": "KEY=other"},
    )
    assert not result.ok
    assert ".env" in result.error


# === grep / glob: path-based denial ====================================


async def test_grep_denied_on_node_modules(runtime: ToolRuntime) -> None:
    """Grep rooted in ``node_modules/`` is denied."""
    result = await runtime.execute(
        "grep", {"pattern": "foo", "path": "node_modules/pkg"}
    )
    assert not result.ok
    assert "node_modules" in result.error


async def test_glob_denied_on_git(runtime: ToolRuntime) -> None:
    """Glob rooted in ``.git/`` is denied."""
    result = await runtime.execute(
        "glob", {"pattern": "**/*", "path": ".git/refs"}
    )
    assert not result.ok
    assert ".git" in result.error


# === Hook override: allow / block / modify =============================


async def test_permission_hook_override_allow(
    runtime: ToolRuntime,
    fresh_runner: HookRunner,
    tmp_project: Path,
) -> None:
    """Hook ``allow`` overrides the read denylist.

    Without the hook, ``.env`` would be denied. With the hook, the
    read proceeds (file is materialised under tmp_project).
    """
    (tmp_project / ".env").write_text("KEY=secret\n", encoding="utf-8")
    seen: list[HookContext] = []
    await _register_permission_hook(fresh_runner, "allow", seen=seen)

    result = await runtime.execute("read_file", {"path": ".env"})

    # Hook observed the deny decision from the denylist.
    assert len(seen) == 1
    assert seen[0].payload["permission_decision"] == "deny"
    # And overrode it → read succeeded (content may be redacted but
    # the file was opened).
    assert result.ok, f"hook allow should override; got error={result.error!r}"
    assert "KEY" in result.output  # redaction leaves the key name


async def test_permission_hook_override_deny(
    runtime: ToolRuntime,
    fresh_runner: HookRunner,
    tmp_project: Path,
) -> None:
    """Hook ``block`` forces deny even for a clean ``.md`` path."""
    (tmp_project / "notes.md").write_text("hello\n", encoding="utf-8")
    await _register_permission_hook(fresh_runner, "block")

    result = await runtime.execute("read_file", {"path": "notes.md"})

    assert not result.ok
    assert "denied" in result.error.lower() or "deny" in result.error.lower()
    assert "PermissionRequest hook" in result.error or "denied:" in result.error


async def test_permission_hook_failure_fails_open(
    runtime: ToolRuntime,
    fresh_runner: HookRunner,
    tmp_project: Path,
) -> None:
    """Broken hook → initial denylist decision stands (fail-open,
    explicit).

    For a clean path (``notes.md``), the original decision was
    ``allow`` → tool must still succeed.
    """
    (tmp_project / "notes.md").write_text("hi\n", encoding="utf-8")
    await _register_permission_hook(
        fresh_runner,
        decision="allow",
        raise_exc=RuntimeError("hook exploded"),
    )

    result = await runtime.execute("read_file", {"path": "notes.md"})

    assert result.ok, (
        f"hook failure must not break clean reads; got error={result.error!r}"
    )
    assert "hi" in result.output


async def test_permission_hook_modify_with_decision(
    runtime: ToolRuntime,
    fresh_runner: HookRunner,
    tmp_project: Path,
) -> None:
    """Hook ``modify`` with ``permission_decision="deny"`` blocks a
    clean write.
    """
    await _register_permission_hook(
        fresh_runner,
        "modify",
        override_payload={
            "tool_name": "write_file",
            "arguments_preview": "notes.md",
            "permission_decision": "deny",
            "denied_reason": "policy",
        },
    )

    result = await runtime.execute(
        "write_file", {"path": "notes.md", "content": "hello"}
    )

    assert not result.ok
    # modify-deny path uses the generic message (no denylist match).
    assert "denied" in result.error.lower()


# === arguments_preview truncation =====================================


async def test_arguments_preview_truncated(
    runtime: ToolRuntime,
    fresh_runner: HookRunner,
) -> None:
    """``arguments_preview`` is truncated to 200 chars even when the
    raw ``str(args)`` exceeds that.
    """
    seen: list[HookContext] = []
    await _register_permission_hook(fresh_runner, "allow", seen=seen)

    long_path = "x" * 500 + ".md"
    await runtime.execute("read_file", {"path": long_path})

    assert len(seen) == 1
    preview = seen[0].payload["arguments_preview"]
    assert isinstance(preview, str)
    assert len(preview) <= 200, (
        f"preview must be truncated to 200 chars, got {len(preview)}"
    )


# === Regression: bash still wired =====================================


async def test_existing_bash_permission_still_works(
    runtime: ToolRuntime,
    fresh_runner: HookRunner,
) -> None:
    """Regression: ``_bash`` still calls ``PermissionRequest`` (Phase
    4.5 wiring must survive the Phase 4.7 refactor).
    """
    seen: list[HookContext] = []
    await _register_permission_hook(fresh_runner, "allow", seen=seen)

    await runtime.execute("bash", {"command": "echo hi", "timeout": 5})

    assert len(seen) == 1
    assert seen[0].payload["tool_name"] == "bash"
    assert seen[0].event == "PermissionRequest"
