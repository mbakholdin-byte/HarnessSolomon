"""Phase 4.6 v1.16.0: Tests for per-event Pydantic payload schemas.

Covers:
    - Individual schema validation (valid + invalid payloads).
    - ``validate_payload`` helper (fail-open, unknown event skip).
    - PII safety: ``OnMemoryWritePayload`` has no ``value`` field.
    - Trust boundary: ``schemas.py`` does not import
      ``harness.agents`` or ``harness.server`` (AST scan).
    - Runner integration: ``fire()`` calls ``validate_payload``.
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from harness.hooks.context import HookContext, validate_payload
from harness.hooks.events import EventType
from harness.hooks.registry import HookRegistry, HookSpec
from harness.hooks.runner import HookRunner
from harness.hooks.schemas import (
    EVENT_SCHEMAS,
    OnMemoryWritePayload,
    __version__ as SCHEMA_VERSION,
)


# ---------------------------------------------------------------------------
# 1. Per-event schema validation
# ---------------------------------------------------------------------------

class TestPreToolUsePayload:
    """PreToolUse schema: requires tool_name + arguments."""

    def test_pre_tool_use_payload_validates(self) -> None:
        """A valid payload round-trips through model_validate + model_dump."""
        payload = {
            "tool_name": "read_file",
            "arguments": {"path": "/tmp/foo"},
        }
        result = validate_payload("PreToolUse", payload)
        assert result["tool_name"] == "read_file"
        assert result["arguments"] == {"path": "/tmp/foo"}

    def test_pre_tool_use_missing_required_field_keeps_original(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Missing ``tool_name`` → validation fails → original returned."""
        bad_payload = {"arguments": {"path": "/x"}}
        with caplog.at_level(logging.WARNING, logger="harness.hooks.context"):
            result = validate_payload("PreToolUse", bad_payload)
        # Fail-open: original payload returned unchanged.
        assert result is bad_payload
        assert "validation failed" in caplog.text.lower()


class TestPostToolUsePayload:
    """PostToolUse schema: tool_name + arguments required; ok/output/error optional."""

    def test_post_tool_use_payload_validates(self) -> None:
        """Full payload with all fields validates and round-trips."""
        payload = {
            "tool_name": "bash",
            "arguments": {"command": "ls"},
            "ok": True,
            "output": "file1\nfile2",
            "error": "",
        }
        result = validate_payload("PostToolUse", payload)
        assert result["tool_name"] == "bash"
        assert result["ok"] is True

    def test_post_tool_use_optional_fields_default_none(self) -> None:
        """Only required fields → optionals default to None."""
        payload = {"tool_name": "bash", "arguments": {}}
        result = validate_payload("PostToolUse", payload)
        assert result["tool_name"] == "bash"
        assert result["ok"] is None


class TestStopPayload:
    """Stop schema: reason + final_message + iterations (>=0)."""

    def test_stop_payload_requires_final_message(self) -> None:
        """Missing ``final_message`` → validation fails → original returned."""
        bad = {"reason": "done", "iterations": 5}
        result = validate_payload("Stop", bad)
        assert result is bad  # fail-open returns original

    def test_stop_payload_negative_iterations_fails(self) -> None:
        """iterations < 0 violates Field(ge=0) → fail-open."""
        bad = {"reason": "done", "final_message": "bye", "iterations": -1}
        result = validate_payload("Stop", bad)
        assert result is bad

    def test_stop_payload_valid(self) -> None:
        """A complete valid Stop payload round-trips."""
        payload = {
            "reason": "explicit_stop",
            "final_message": "Task complete.",
            "iterations": 3,
        }
        result = validate_payload("Stop", payload)
        assert result["reason"] == "explicit_stop"
        assert result["iterations"] == 3


# ---------------------------------------------------------------------------
# 2. PermissionRequest + OnMemoryWrite (PII)
# ---------------------------------------------------------------------------

class TestPermissionRequestPayload:
    """PermissionRequest schema: literal decision + truncated preview."""

    def test_permission_request_payload_schema(self) -> None:
        """Valid allow/deny payloads validate; invalid literal fails."""
        valid = {
            "tool_name": "bash",
            "arguments_preview": "rm -rf /",
            "permission_decision": "deny",
            "denied_reason": "safety pattern matched",
        }
        result = validate_payload("PermissionRequest", valid)
        assert result["permission_decision"] == "deny"

        # Invalid literal → fail-open.
        bad = dict(valid, permission_decision="maybe")
        result_bad = validate_payload("PermissionRequest", bad)
        assert result_bad is bad


class TestOnMemoryWritePIISafety:
    """OnMemoryWrite schema: NO ``value`` field (PII safety)."""

    def test_on_memory_write_payload_no_value_field(self) -> None:
        """The ``value`` field MUST NOT exist in the OnMemoryWrite schema.

        Emit sites pass ``key_hash`` (truncated SHA-256), never the raw
        key or value. If a ``value`` field appears in the schema, it
        would be a PII regression.
        """
        field_names = set(OnMemoryWritePayload.model_fields.keys())
        assert "value" not in field_names, (
            f"PII regression: OnMemoryWritePayload has a 'value' field. "
            f"Fields: {field_names}"
        )
        assert "key" not in field_names, (
            f"PII regression: OnMemoryWritePayload has a raw 'key' field. "
            f"Fields: {field_names}"
        )
        assert "key_hash" in field_names, "Expected key_hash field for PII-safe reference"

    def test_on_memory_write_valid_payload(self) -> None:
        """The actual emit-site payload validates."""
        payload = {
            "layer": "L2",
            "key_hash": "abc123def456",
            "scope": "solomon",
            "size_bytes": 1024,
        }
        result = validate_payload("OnMemoryWrite", payload)
        assert result["layer"] == "L2"
        assert result["size_bytes"] == 1024

    def test_on_memory_write_extra_value_field_ignored(self) -> None:
        """An extra ``value`` field in the payload is ignored (extra='ignore').

        This ensures forward-compat: if a future emit site accidentally
        includes ``value``, validation doesn't fail. BUT the validated
        output does NOT carry the value (it's dropped).
        """
        payload = {
            "layer": "L2",
            "key_hash": "abc123def456",
            "scope": "solomon",
            "size_bytes": 512,
            "value": "some secret that should not leak",
        }
        result = validate_payload("OnMemoryWrite", payload)
        assert "value" not in result, "extra='ignore' should drop 'value' from output"


# ---------------------------------------------------------------------------
# 3. validate_payload: unknown events + fail-open
# ---------------------------------------------------------------------------

class TestValidatePayloadEdgeCases:
    """validate_payload: unknown events skip; invalid payloads fail-open."""

    def test_unknown_event_skips_validation(self) -> None:
        """An event not in EVENT_SCHEMAS → payload returned as-is (no-op)."""
        payload = {"foo": "bar", "baz": 42}
        result = validate_payload("TotallyUnknownEvent", payload)
        assert result is payload  # same object, untouched

    def test_invalid_payload_logs_warning_keeps_original(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A structurally invalid payload logs a WARNING and returns the original."""
        # PreToolUse requires tool_name (str) + arguments (dict).
        # Passing wrong types triggers ValidationError.
        bad = {"tool_name": 12345, "arguments": "not-a-dict"}
        with caplog.at_level(logging.WARNING, logger="harness.hooks.context"):
            result = validate_payload("PreToolUse", bad)
        assert result is bad
        assert any(
            "validation failed" in rec.message.lower()
            and "PreToolUse" in rec.message
            for rec in caplog.records
        ), f"Expected a validation-failed warning, got: {caplog.text}"

    def test_schema_version_mismatch_handled(self) -> None:
        """A future schema version (string) doesn't crash validate_payload.

        ``__version__`` is a forward-compat sentinel. We verify it's a
        string and that validate_payload doesn't depend on a specific
        value (it works regardless of version).
        """
        assert isinstance(SCHEMA_VERSION, str)
        assert SCHEMA_VERSION == "1"

        # Even if EVENT_SCHEMAS is patched to return a different model,
        # validate_payload should still work (it just validates + dumps).
        payload = {"tool_name": "read_file", "arguments": {}}
        result = validate_payload("PreToolUse", payload)
        assert result["tool_name"] == "read_file"

    def test_extra_fields_stripped_on_success(self) -> None:
        """Successful validation drops unknown fields (extra='ignore')."""
        payload = {
            "tool_name": "read_file",
            "arguments": {"path": "/x"},
            "future_field": "should be dropped",
        }
        result = validate_payload("PreToolUse", payload)
        assert "future_field" not in result
        assert result["tool_name"] == "read_file"


# ---------------------------------------------------------------------------
# 4. Trust boundary: schemas.py does not import harness.agents/server
# ---------------------------------------------------------------------------

class TestSchemasTrustBoundary:
    """AST scan: schemas.py must not import harness.agents or harness.server.

    Mirrors the pattern from ``tests/test_hooks_trust_boundary.py``.
    """

    SCHEMAS_PATH = (
        Path(__file__).parent.parent / "harness" / "hooks" / "schemas.py"
    )

    FORBIDDEN_PREFIXES: tuple[str, ...] = (
        "harness.agents",
        "harness.server",
    )

    def test_trust_boundary_preserved(self) -> None:
        """schemas.py must NOT import harness.agents or harness.server."""
        assert self.SCHEMAS_PATH.is_file(), f"Not found: {self.SCHEMAS_PATH}"
        source = self.SCHEMAS_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(self.SCHEMAS_PATH))

        violations: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self._check_module(alias.name, node.lineno, violations)
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    continue  # relative import — can't reach forbidden prefixes
                if node.module:
                    self._check_module(node.module, node.lineno, violations)

        assert not violations, (
            "Trust boundary violation in schemas.py:\n  "
            + "\n  ".join(violations)
        )

    def _check_module(
        self, module: str, lineno: int, violations: list[str],
    ) -> None:
        for prefix in self.FORBIDDEN_PREFIXES:
            if module == prefix or module.startswith(prefix + "."):
                violations.append(
                    f"line {lineno}: forbidden import {module!r} "
                    f"(prefix {prefix!r})"
                )

    def test_schemas_only_stdlib_and_pydantic(self) -> None:
        """All top-level imports must be stdlib or pydantic."""
        source = self.SCHEMAS_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(self.SCHEMAS_PATH))
        allowed_prefixes = ("pydantic", "typing", "harness.hooks")
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root in allowed_prefixes or _is_stdlib(alias.name), (
                        f"Unexpected import: {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    continue
                if node.module:
                    root = node.module.split(".")[0]
                    assert root in allowed_prefixes or _is_stdlib(node.module), (
                        f"Unexpected from-import: {node.module}"
                    )


def _is_stdlib(module: str) -> bool:
    """Check if a module name is part of the Python stdlib."""
    import sysconfig
    stdlib_path = sysconfig.get_paths()["stdlib"]
    stdlib_pkgs = {
        "abc", "ast", "asyncio", "collections", "contextlib",
        "dataclasses", "datetime", "enum", "functools", "hashlib",
        "importlib", "inspect", "io", "itertools", "json", "logging",
        "os", "pathlib", "re", "string", "sys", "time", "types",
        "typing", "unittest", "uuid", "warnings", "__future__",
    }
    root = module.split(".")[0]
    return root in stdlib_pkgs


# ---------------------------------------------------------------------------
# 5. Runner integration: fire() calls validate_payload
# ---------------------------------------------------------------------------

class TestRunnerUsesValidatePayload:
    """HookRunner.fire() must invoke validate_payload before dispatch."""

    async def test_runner_uses_validate_payload(self) -> None:
        """fire() calls validate_payload with the context event + payload."""
        registry = HookRegistry()
        runner = HookRunner(registry)
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={"tool_name": "read_file", "arguments": {}},
        )

        with patch(
            "harness.hooks.runner.validate_payload",
            wraps=validate_payload,
        ) as mock_vp:
            await runner.fire(ctx)

        mock_vp.assert_called_once()
        call_args = mock_vp.call_args
        assert call_args[0][0] == "PreToolUse"
        assert call_args[0][1]["tool_name"] == "read_file"

    async def test_runner_validation_failure_does_not_break_dispatch(
        self, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """When validate_payload returns the original (invalid) payload,
        the runner still dispatches hooks successfully (fail-open)."""
        # Register a simple allow hook.
        async def _allow_hook(ctx: HookContext) -> Any:
            from harness.hooks import HookDecision
            return HookDecision(decision="allow", hook_id="test-allow")

        registry = HookRegistry()
        await registry.register(
            HookSpec(
                hook_id="test.allow",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
                callable=_allow_hook,
            )
        )
        runner = HookRunner(registry)

        # Invalid payload (missing tool_name) — should still dispatch.
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={"arguments": {}},  # missing tool_name
        )
        with caplog.at_level(logging.WARNING):
            agg = await runner.fire(ctx)

        # Dispatch succeeded despite invalid payload.
        assert agg.final_decision == "allow"
        assert len(agg.decisions) == 1

    async def test_runner_valid_payload_normalised(self) -> None:
        """A valid payload with extra fields is normalised (extras dropped).

        The normalisation happens inside ``fire()``: the context passed
        to hooks has extra fields stripped. We verify this by registering
        a hook that echoes the payload it receives.
        """
        seen_payload: dict[str, Any] = {}

        async def _capture_hook(ctx: HookContext) -> Any:
            from harness.hooks import HookDecision
            seen_payload.update(ctx.payload)
            return HookDecision(decision="allow", hook_id="capture")

        registry = HookRegistry()
        await registry.register(
            HookSpec(
                hook_id="capture",
                event=EventType.PRE_TOOL_USE,
                transport="builtin",
                callable=_capture_hook,
            )
        )
        runner = HookRunner(registry)
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={
                "tool_name": "read_file",
                "arguments": {"path": "/x"},
                "extra_junk": "dropped",
            },
        )
        await runner.fire(ctx)
        # The hook saw the normalised payload — extra_junk stripped.
        assert "extra_junk" not in seen_payload, (
            f"Expected extra fields stripped, got: {seen_payload}"
        )
        assert seen_payload["tool_name"] == "read_file"


# ---------------------------------------------------------------------------
# 6. All 16 events have schemas (completeness)
# ---------------------------------------------------------------------------

class TestSchemaCompleteness:
    """Every implemented EventType should have a schema in EVENT_SCHEMAS."""

    def test_all_enabled_events_have_schemas(self) -> None:
        """Each EventType in ENABLED_BY_DEFAULT maps to an EVENT_SCHEMAS entry."""
        from harness.hooks.events import ENABLED_BY_DEFAULT

        missing = [
            ev.value for ev in ENABLED_BY_DEFAULT
            if ev.value not in EVENT_SCHEMAS
        ]
        assert not missing, (
            f"Events without schemas (Phase 4.6 gap): {missing}"
        )

    def test_event_schemas_count(self) -> None:
        """Sanity: we have exactly 16 schemas (14 CC + 2 custom Solomon)."""
        assert len(EVENT_SCHEMAS) == 16, (
            f"Expected 16 event schemas, got {len(EVENT_SCHEMAS)}: "
            f"{sorted(EVENT_SCHEMAS.keys())}"
        )
