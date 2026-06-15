"""Tests for Phase 3.5 wiring + new Settings (Step 2)."""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.config import Settings


# === Settings (Phase 3.5) ===

class TestPhase35Settings:
    def test_compaction_persistent_store_default_true(self, tmp_path: Path) -> None:
        s = Settings(
            compaction_enabled=True,
            db_path=tmp_path / "harness.db",
        )
        assert s.compaction_persistent_store is True

    def test_compaction_cache_max_versions_default_five(self, tmp_path: Path) -> None:
        s = Settings(
            compaction_enabled=True,
            db_path=tmp_path / "harness.db",
        )
        assert s.compaction_cache_max_versions == 5

    def test_compaction_audit_log_default_false(self, tmp_path: Path) -> None:
        s = Settings(
            compaction_enabled=True,
            db_path=tmp_path / "harness.db",
        )
        assert s.compaction_audit_log is False

    def test_compaction_persistent_store_explicit_false(
        self, tmp_path: Path,
    ) -> None:
        s = Settings(
            compaction_enabled=True,
            compaction_persistent_store=False,
            db_path=tmp_path / "harness.db",
        )
        assert s.compaction_persistent_store is False

    def test_compaction_cache_max_versions_override(
        self, tmp_path: Path,
    ) -> None:
        s = Settings(
            compaction_enabled=True,
            compaction_cache_max_versions=20,
            db_path=tmp_path / "harness.db",
        )
        assert s.compaction_cache_max_versions == 20

    def test_compaction_audit_log_explicit_true(
        self, tmp_path: Path,
    ) -> None:
        s = Settings(
            compaction_enabled=True,
            compaction_audit_log=True,
            db_path=tmp_path / "harness.db",
        )
        assert s.compaction_audit_log is True

    def test_compaction_cache_max_versions_zero_rejected(
        self, tmp_path: Path,
    ) -> None:
        # Pydantic ``ge=1`` constraint catches this at the field level.
        with pytest.raises(ValidationError):
            Settings(
                compaction_enabled=True,
                compaction_cache_max_versions=0,
                db_path=tmp_path / "harness.db",
            )

    def test_compaction_cache_max_versions_negative_rejected(
        self, tmp_path: Path,
    ) -> None:
        with pytest.raises(ValidationError):
            Settings(
                compaction_enabled=True,
                compaction_cache_max_versions=-3,
                db_path=tmp_path / "harness.db",
            )

    def test_phase35_settings_coexist_with_phase3(
        self, tmp_path: Path,
    ) -> None:
        """The 3 new settings don't break any Phase 3 setting."""
        s = Settings(
            compaction_enabled=True,
            compaction_persist_to_memory=True,
            compaction_persistent_store=True,
            compaction_cache_max_versions=5,
            compaction_audit_log=False,
            db_path=tmp_path / "harness.db",
        )
        # Phase 3 fields still present and correct.
        assert s.compaction_enabled is True
        assert s.compaction_persist_to_memory is True


# === app.py wiring (lifespan integration) ===

class TestAppWiring:
    """Lifespan correctly instantiates UnifiedMemory + CompactStore
    and wires them into the ContextCompactor.

    We test the wiring logic in isolation (without spinning up
    a full FastAPI app) by importing the lifespan and patching
    the heavy dependencies.
    """

    async def test_lifespan_wires_compact_store_and_unified_memory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The Phase 3.5 hook on app.py:117 is closed:
        ``memory=unified_memory, store=compact_store`` are passed to
        ``ContextCompactor``."""
        from harness.config import Settings
        from harness.server import app as app_mod

        test_settings = Settings(
            compaction_enabled=True,
            compaction_threshold_ratio=0.5,
            compaction_target_ratio=0.25,
            compaction_keep_recent_turns=2,
            compaction_summarizer_model="qwen3:8b",
            compaction_summarizer_fallback="glm-4.7",
            compaction_persistent_store=True,
            compaction_cache_max_versions=5,
            compaction_audit_log=False,
            db_path=tmp_path / "harness.db",
        )
        # Patch module-level settings so the lifespan picks it up.
        monkeypatch.setattr(app_mod, "settings", test_settings)

        # Stub LLMRouter to avoid needing API keys.
        class _StubRouter:
            def __init__(self) -> None:
                pass

        monkeypatch.setattr(
            "harness.server.llm.router.LLMRouter", _StubRouter,
        )

        # Stub UnifiedMemory to a no-op class.
        class _StubMemory:
            def __init__(self, settings: object, db_path: Path) -> None:
                self.db_path = db_path
            async def write(self, mem: object) -> None:
                return None

        monkeypatch.setattr(
            "harness.memory.unified.UnifiedMemory", _StubMemory,
        )

        # Capture the ContextCompactor constructor kwargs.
        captured: dict[str, object] = {}

        def _capturing_compactor(*args: object, **kwargs: object) -> object:
            captured.update(kwargs)
            captured["args"] = args
            class _Stub:
                pass
            return _Stub()

        monkeypatch.setattr(
            "harness.context.compaction.ContextCompactor",
            _capturing_compactor,
        )

        from fastapi import FastAPI
        app = FastAPI()
        app.state.compactor = None

        async with app_mod.lifespan(app):
            pass

        # Assert: the compactor was constructed with both memory and store.
        assert "memory" in captured
        assert "store" in captured
        assert captured["store"] is not None
        assert captured["memory"] is not None

    async def test_lifespan_disables_persistent_store_via_setting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ``compaction_persistent_store=False``, the compactor
        still works but ``store=None`` is passed (cache disabled)."""
        from harness.config import Settings
        from harness.server import app as app_mod

        test_settings = Settings(
            compaction_enabled=True,
            compaction_persistent_store=False,
            compaction_cache_max_versions=5,
            compaction_audit_log=False,
            db_path=tmp_path / "harness.db",
        )
        monkeypatch.setattr(app_mod, "settings", test_settings)

        class _StubRouter:
            def __init__(self) -> None:
                pass

        monkeypatch.setattr(
            "harness.server.llm.router.LLMRouter", _StubRouter,
        )

        class _StubMemory:
            def __init__(self, settings: object, db_path: Path) -> None:
                pass
            async def write(self, mem: object) -> None:
                return None

        monkeypatch.setattr(
            "harness.memory.unified.UnifiedMemory", _StubMemory,
        )

        captured: dict[str, object] = {}

        def _capturing_compactor(*args: object, **kwargs: object) -> object:
            captured.update(kwargs)
            class _Stub:
                pass
            return _Stub()

        monkeypatch.setattr(
            "harness.context.compaction.ContextCompactor",
            _capturing_compactor,
        )

        from fastapi import FastAPI
        app = FastAPI()
        app.state.compactor = None

        async with app_mod.lifespan(app):
            pass

        # store=None because the setting is False.
        assert captured.get("store") is None
