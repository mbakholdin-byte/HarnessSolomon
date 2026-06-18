"""Phase 4.0 + 4.3: Tests for 7 builtin hooks."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from harness.hooks import HookContext, HookDecision
from harness.hooks.builtin import (
    BUILTIN_HOOKS,
    autosave_hook,
    block_dangerous_hook,
    complexity_check_hook,
    confirm_dangerous_hook,
    inject_context_hook,
    license_check_hook,
    log_hook,
    notify_terminal_hook,
    validate_hook,
)


class TestBuiltinRegistry:
    """BUILTIN_HOOKS dict: 7 core hooks (Phase 4.0 + 4.3) + Phase 4.10 advisory hooks."""

    # Phase 4.0 + 4.3 core set (must always be present).
    _CORE_HOOKS = {
        "log",
        "validate",
        "block_dangerous",
        "inject_context",
        "autosave",
        "confirm_dangerous",
        "notify_terminal",
    }

    def test_all_7_present(self) -> None:
        # The original 7 must remain present (superset check).
        assert self._CORE_HOOKS.issubset(set(BUILTIN_HOOKS))

    def test_phase43_hooks_registered(self) -> None:
        assert "confirm_dangerous" in BUILTIN_HOOKS
        assert "notify_terminal" in BUILTIN_HOOKS
        assert BUILTIN_HOOKS["confirm_dangerous"] is confirm_dangerous_hook
        assert BUILTIN_HOOKS["notify_terminal"] is notify_terminal_hook


class TestLogHook:
    """log_hook emits INFO and returns allow."""

    async def test_returns_allow(self) -> None:
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="a1",
            payload={"tool_name": "read_file"},
        )
        d = await log_hook(ctx)
        assert d.decision == "allow"
        assert d.hook_id == "builtin.log"

    async def test_logs_at_info_level(self, caplog) -> None:
        with caplog.at_level(logging.INFO, logger="harness.hooks.builtin.log"):
            ctx = HookContext(
                event="PreToolUse",
                session_id="s1",
                agent_id="",
                payload={"tool_name": "read_file"},
            )
            await log_hook(ctx)
        assert any("PreToolUse" in r.message for r in caplog.records)


class TestValidateHook:
    """validate_hook enforces Pydantic schemas (if registered)."""

    async def test_no_schema_returns_allow(self) -> None:
        """Tool with no registered schema → no validation → allow."""
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={"tool_name": "unknown_tool_xyz", "arguments": {"x": 1}},
        )
        d = await validate_hook(ctx)
        assert d.decision == "allow"

    async def test_non_dict_arguments_blocked(self) -> None:
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={"tool_name": "x", "arguments": "not a dict"},
        )
        d = await validate_hook(ctx)
        assert d.decision == "block"
        assert "must be a dict" in d.output["reason"]

    async def test_not_pre_tool_use_skips(self) -> None:
        """ValidateHook only acts on PreToolUse; other events allow."""
        ctx = HookContext(
            event="PostToolUse",
            session_id="s1",
            agent_id="",
            payload={"tool_name": "x", "arguments": "any"},
        )
        d = await validate_hook(ctx)
        assert d.decision == "allow"

    async def test_valid_arguments_pass(self, monkeypatch) -> None:
        """With a registered schema, valid args pass."""
        from pydantic import BaseModel

        class MyArgs(BaseModel):
            path: str

        # Stub the schema lookup at the source: validate_hook calls
        # _get_tool_schemas() which imports from harness.tools.schemas.
        # We mock that function directly.
        from harness.hooks.builtin import validate

        monkeypatch.setitem(
            validate._SCHEMAS_OVERRIDE, "my_tool", MyArgs
        )
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={"tool_name": "my_tool", "arguments": {"path": "/x"}},
        )
        d = await validate_hook(ctx)
        assert d.decision == "allow"

    async def test_invalid_arguments_blocked(self, monkeypatch) -> None:
        from pydantic import BaseModel

        class MyArgs(BaseModel):
            path: str

        from harness.hooks.builtin import validate

        monkeypatch.setitem(
            validate._SCHEMAS_OVERRIDE, "my_tool", MyArgs
        )
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={"tool_name": "my_tool", "arguments": {"path": 123}},
        )
        d = await validate_hook(ctx)
        assert d.decision == "block"
        assert "validation failed" in d.output["reason"]


class TestBlockDangerousHook:
    """block_dangerous_hook catches destructive patterns."""

    async def test_rm_rf_root_blocked(self) -> None:
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={"tool_name": "bash", "arguments": {"command": "rm -rf /"}},
        )
        d = await block_dangerous_hook(ctx)
        assert d.decision == "block"
        assert "dangerous pattern" in d.output["reason"]

    async def test_mkfs_blocked(self) -> None:
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={"tool_name": "bash", "arguments": {"command": "mkfs /dev/sda1"}},
        )
        d = await block_dangerous_hook(ctx)
        assert d.decision == "block"

    async def test_drop_database_blocked(self) -> None:
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={"tool_name": "sql", "arguments": {"query": "DROP DATABASE users;"}},
        )
        d = await block_dangerous_hook(ctx)
        assert d.decision == "block"

    async def test_format_c_blocked(self) -> None:
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={"tool_name": "bash", "arguments": {"command": "format c:"}},
        )
        d = await block_dangerous_hook(ctx)
        assert d.decision == "block"

    async def test_safe_command_allowed(self) -> None:
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={"tool_name": "bash", "arguments": {"command": "ls -la"}},
        )
        d = await block_dangerous_hook(ctx)
        assert d.decision == "allow"

    async def test_non_pre_tool_use_skips(self) -> None:
        ctx = HookContext(
            event="PostToolUse",
            session_id="s1",
            agent_id="",
            payload={"tool_name": "bash", "arguments": {"command": "rm -rf /"}},
        )
        d = await block_dangerous_hook(ctx)
        # PostToolUse is not gated by this hook.
        assert d.decision == "allow"

    async def test_arguments_as_string(self) -> None:
        """If arguments is a raw string, match against it directly."""
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={"tool_name": "bash", "arguments": "rm -rf /etc"},
        )
        d = await block_dangerous_hook(ctx)
        assert d.decision == "block"


class TestInjectContextHook:
    """inject_context_hook prepends L0 to UserPromptSubmit."""

    async def test_non_user_prompt_skips(self) -> None:
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={"prompt": "hello"},
        )
        d = await inject_context_hook(ctx)
        assert d.decision == "allow"

    async def test_no_session_id_skips(self) -> None:
        ctx = HookContext(
            event="UserPromptSubmit",
            session_id="",
            agent_id="",
            payload={"prompt": "hello"},
        )
        d = await inject_context_hook(ctx)
        assert d.decision == "allow"

    async def test_with_l0_modifies_payload(self, monkeypatch) -> None:
        """If L0 is non-empty, modify payload with prepended context."""
        # Stub the L0 read.
        from harness.hooks.builtin import inject_context

        monkeypatch.setattr(
            inject_context, "_get_l0_section", lambda s: "## Plan\n- step 1"
        )
        ctx = HookContext(
            event="UserPromptSubmit",
            session_id="s1",
            agent_id="",
            payload={"prompt": "do thing"},
        )
        d = await inject_context_hook(ctx)
        assert d.decision == "modify"
        new_prompt = d.output["payload"]["prompt"]
        assert "[Harness Context]" in new_prompt
        assert "## Plan" in new_prompt
        assert "do thing" in new_prompt


class TestAutosaveHook:
    """autosave_hook writes NDJSON audit line on SessionEnd."""

    async def test_non_session_end_skips(self) -> None:
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={},
        )
        d = await autosave_hook(ctx)
        assert d.decision == "allow"

    async def test_session_end_writes_file(self, tmp_path, monkeypatch) -> None:
        # Redirect CWD so audit dir is under tmp.
        monkeypatch.chdir(tmp_path)
        ctx = HookContext(
            event="SessionEnd",
            session_id="s-42",
            agent_id="a-1",
            payload={"messages": 100, "exit_reason": "max_iter"},
        )
        d = await autosave_hook(ctx)
        assert d.decision == "allow"
        audit = tmp_path / "data" / "audit" / "session-end.ndjson"
        assert audit.exists()
        line = audit.read_text(encoding="utf-8").strip()
        data = json.loads(line)
        assert data["event"] == "SessionEnd"
        assert data["session_id"] == "s-42"
        assert data["agent_id"] == "a-1"
        assert data["payload"]["messages"] == 100
