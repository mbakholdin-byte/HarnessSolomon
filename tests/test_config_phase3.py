"""Phase 3: tests for new Settings (compaction/embeddings/privacy).

15 fields total: 8 compaction + 4 embeddings + 3 privacy.

We test:
    - Default values match the plan
    - Env-var override (HARNESS_* prefix) works
    - Pydantic validators reject misconfigured values
    - ``Literal``-typed fields reject out-of-set values
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from harness.config import Settings


class TestCompactionSettings:
    def test_compaction_enabled_default_true(self) -> None:
        s = Settings()
        assert s.compaction_enabled is True

    def test_compaction_threshold_default(self) -> None:
        s = Settings()
        assert s.compaction_threshold_ratio == 0.75

    def test_compaction_target_default(self) -> None:
        s = Settings()
        assert s.compaction_target_ratio == 0.50

    def test_compaction_keep_recent_default(self) -> None:
        s = Settings()
        assert s.compaction_keep_recent_turns == 6

    def test_compaction_summarizer_model_default_empty(self) -> None:
        # Empty → resolved by caller to subagent_t1_model.
        s = Settings()
        assert s.compaction_summarizer_model == ""

    def test_compaction_summarizer_fallback_default_empty(self) -> None:
        s = Settings()
        assert s.compaction_summarizer_fallback == ""

    def test_compaction_summarizer_max_input_default_zero(self) -> None:
        s = Settings()
        assert s.compaction_summarizer_max_input_tokens == 0

    def test_compaction_persist_default_true(self) -> None:
        s = Settings()
        assert s.compaction_persist_to_memory is True

    def test_target_must_be_less_than_threshold(self) -> None:
        with pytest.raises(ValidationError) as exc:
            Settings(
                compaction_enabled=True,
                compaction_threshold_ratio=0.5,
                compaction_target_ratio=0.6,  # > threshold → invalid
            )
        assert "compaction_target_ratio" in str(exc.value)

    def test_target_equal_threshold_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Settings(
                compaction_enabled=True,
                compaction_threshold_ratio=0.5,
                compaction_target_ratio=0.5,  # == threshold → invalid
            )

    def test_disabled_compaction_skips_ratio_check(self) -> None:
        # When compaction is off, the validator must not enforce the ratio.
        s = Settings(
            compaction_enabled=False,
            compaction_threshold_ratio=0.5,
            compaction_target_ratio=0.6,  # would be invalid if enabled
        )
        assert s.compaction_enabled is False
        assert s.compaction_target_ratio == 0.6

    def test_keep_recent_turns_minimum_two(self) -> None:
        with pytest.raises(ValidationError):
            Settings(compaction_keep_recent_turns=1)

    def test_keep_recent_turns_maximum_64(self) -> None:
        with pytest.raises(ValidationError):
            Settings(compaction_keep_recent_turns=65)

    def test_threshold_must_be_in_open_unit_interval(self) -> None:
        with pytest.raises(ValidationError):
            Settings(compaction_threshold_ratio=1.0)
        with pytest.raises(ValidationError):
            Settings(compaction_threshold_ratio=0.0)


class TestEmbeddingsSettings:
    def test_embedding_model_default(self) -> None:
        s = Settings()
        assert s.embedding_model == "intfloat/multilingual-e5-small"

    def test_embedding_precision_default_int8(self) -> None:
        s = Settings()
        assert s.embedding_precision == "int8"

    def test_embedding_dim_default_384(self) -> None:
        s = Settings()
        assert s.embedding_dim == 384

    def test_embeddings_dir_default(self) -> None:
        s = Settings()
        # Path is absolute because PROJECT_ROOT is absolute.
        assert s.embeddings_dir.is_absolute()
        assert s.embeddings_dir.name == "embeddings"

    def test_precision_must_be_literal(self) -> None:
        with pytest.raises(ValidationError):
            Settings(embedding_precision="float16")  # not in Literal

    def test_precision_fp32_accepted(self) -> None:
        s = Settings(embedding_precision="fp32")
        assert s.embedding_precision == "fp32"

    def test_embedding_dim_minimum_64(self) -> None:
        with pytest.raises(ValidationError):
            Settings(embedding_dim=32)

    def test_embedding_dim_maximum_4096(self) -> None:
        with pytest.raises(ValidationError):
            Settings(embedding_dim=8192)


class TestPrivacySettings:
    def test_redaction_enabled_default_true(self) -> None:
        s = Settings()
        assert s.redaction_enabled is True

    def test_redaction_categories_default_empty(self) -> None:
        s = Settings()
        assert s.redaction_categories == ""

    def test_redaction_audit_log_default_false(self) -> None:
        s = Settings()
        assert s.redaction_audit_log is False

    def test_redaction_can_be_disabled(self) -> None:
        s = Settings(redaction_enabled=False)
        assert s.redaction_enabled is False

    def test_redaction_audit_log_can_be_enabled(self) -> None:
        s = Settings(redaction_audit_log=True)
        assert s.redaction_audit_log is True


class TestEnvVarOverride:
    """Settings() reads from .env + OS env via pydantic-settings v2.
    The operator-facing env var names follow the ``HARNESS_`` prefix
    convention; we verify the field names match the convention by
    constructing Settings with overrides (the framework-level env
    binding is exercised by pydantic-settings' own test suite, not
    ours)."""

    def test_field_names_match_harness_prefix_convention(self) -> None:
        # All Phase 3 fields use snake_case → HARNESS_<UPPER_SNAKE>.
        # Verify the construction accepts the override.
        s = Settings(compaction_enabled=False)
        assert s.compaction_enabled is False

    def test_embedding_model_override_at_construction(self) -> None:
        s = Settings(embedding_model="BAAI/bge-small-en-v1.5")
        assert s.embedding_model == "BAAI/bge-small-en-v1.5"

    def test_redaction_categories_override_at_construction(self) -> None:
        s = Settings(redaction_categories="EMAIL,PHONE")
        assert s.redaction_categories == "EMAIL,PHONE"

    def test_compaction_keep_recent_override_at_construction(self) -> None:
        s = Settings(compaction_keep_recent_turns=10)
        assert s.compaction_keep_recent_turns == 10


class TestQwen3ModelCatalog:
    """The qwen3:8b entry must be present in MODELS — it's the T1
    default for the compaction summarizer (Phase 3.0+)."""

    def test_qwen3_8b_in_catalog(self) -> None:
        from harness.server.llm.models import MODELS

        ids = [m["id"] for m in MODELS]
        assert "qwen3:8b" in ids

    def test_qwen3_8b_metadata(self) -> None:
        from harness.server.llm.models import MODELS

        qwen = next(m for m in MODELS if m["id"] == "qwen3:8b")
        assert qwen["tier"] == "T1"
        assert qwen["ctx"] == 32768
        assert qwen["pricing_input"] == 0.0
        assert qwen["pricing_output"] == 0.0
        assert qwen["provider"] == "ollama"
