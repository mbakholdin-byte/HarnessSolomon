"""Phase 4.0: Tests for the 31 new hooks Settings."""
from __future__ import annotations

import pytest

from harness.config import Settings


class TestHooksSettingsDefaults:
    """Settings defaults match the Phase 4.0 plan."""

    def test_master_switch_default(self) -> None:
        s = Settings()
        assert s.hooks_enabled is True

    def test_default_max_ms_default(self) -> None:
        s = Settings()
        assert s.hooks_default_max_ms == 3000

    def test_max_per_event_default(self) -> None:
        s = Settings()
        assert s.hooks_max_per_event == 10

    def test_max_recursion_depth_default(self) -> None:
        s = Settings()
        assert s.hooks_max_recursion_depth == 3

    def test_spec_lists_default_empty(self) -> None:
        s = Settings()
        assert s.hooks_subprocess_specs == ""
        assert s.hooks_http_specs == ""
        assert s.hooks_llm_specs == ""

    def test_filter_chain_default_empty(self) -> None:
        s = Settings()
        assert s.hooks_filter_chain == ""

    def test_fail_open_default(self) -> None:
        s = Settings()
        assert s.hooks_fail_open is True

    def test_redact_payloads_default(self) -> None:
        s = Settings()
        assert s.hooks_redact_payloads is True

    def test_audit_log_default(self) -> None:
        s = Settings()
        assert s.hooks_audit_log is False

    def test_subprocess_allowed_paths_default(self) -> None:
        s = Settings()
        assert s.hooks_subprocess_allowed_paths == ".harness/hooks/**"

    def test_on_memory_write_silent_layers_default(self) -> None:
        s = Settings()
        assert s.hooks_on_memory_write_silent_layers == "L1"

    def test_on_compaction_skip_cache_hit_default(self) -> None:
        s = Settings()
        assert s.hooks_on_compaction_skip_cache_hit is True


class TestHooksPerEventDefaults:
    """All 14 per-event enable settings default to True (Elicitation/Notification deferred)."""

    @pytest.mark.parametrize(
        "field",
        [
            "hooks_pre_tool_use_enabled",
            "hooks_post_tool_use_enabled",
            "hooks_stop_enabled",
            "hooks_subagent_start_enabled",
            "hooks_subagent_stop_enabled",
            "hooks_session_start_enabled",
            "hooks_session_end_enabled",
            "hooks_user_prompt_submit_enabled",
            "hooks_pre_compact_enabled",
            "hooks_instructions_loaded_enabled",
            "hooks_permission_request_enabled",
            "hooks_on_memory_write_enabled",
            "hooks_on_routing_decision_enabled",
            "hooks_on_compaction_enabled",
        ],
    )
    def test_event_default_true(self, field: str) -> None:
        s = Settings()
        assert getattr(s, field) is True


class TestHooksBuiltinDefaults:
    """5 builtin hook enable settings: 4 default True, InjectContext defaults False."""

    def test_log_enabled(self) -> None:
        assert Settings().hooks_builtin_log_enabled is True

    def test_validate_enabled(self) -> None:
        assert Settings().hooks_builtin_validate_enabled is True

    def test_block_dangerous_enabled(self) -> None:
        assert Settings().hooks_builtin_block_dangerous_enabled is True

    def test_inject_context_disabled_by_default(self) -> None:
        """InjectContext off by default (L0 already injected via Phase 3 v1.2.1)."""
        assert Settings().hooks_builtin_inject_context_enabled is False

    def test_autosave_enabled(self) -> None:
        assert Settings().hooks_builtin_autosave_enabled is True


class TestHooksSettingsValidation:
    """Pydantic validators catch misconfigurations."""

    def test_default_max_ms_min_100(self) -> None:
        with pytest.raises(Exception):  # Pydantic ValidationError
            Settings(hooks_default_max_ms=50)

    def test_default_max_ms_max_60000(self) -> None:
        with pytest.raises(Exception):
            Settings(hooks_default_max_ms=70000)

    def test_max_per_event_min_1(self) -> None:
        with pytest.raises(Exception):
            Settings(hooks_max_per_event=0)

    def test_max_recursion_depth_min_1(self) -> None:
        with pytest.raises(Exception):
            Settings(hooks_max_recursion_depth=0)

    def test_max_recursion_depth_max_10(self) -> None:
        with pytest.raises(Exception):
            Settings(hooks_max_recursion_depth=20)
