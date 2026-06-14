"""Solomon Harness — configuration.

Loads from environment variables (and .env file in dev).
Single source of truth for all paths, ports, API keys.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Harness settings (Pydantic v2)."""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # === Server ===
    host: str = Field(default="0.0.0.0", description="Bind host")
    port: int = Field(
        default=8765,
        description=(
            "Bind port. Default 8765 because Windows 11 + Docker Desktop reserves "
            "8000/8001 via hns (WSAEACCES). See _output/2026-06/14.06/ports-map.md"
        ),
    )
    log_level: str = Field(default="INFO", description="Logging level")

    # === Storage ===
    project_root: Path = Field(
        default=Path("C:/MyAI"),
        description="Project root for file tools (paths are resolved under this)",
    )
    session_dir: Path = Field(
        default=PROJECT_ROOT / "data" / "sessions",
        description="JSONL session mirror directory",
    )
    db_path: Path = Field(
        default=PROJECT_ROOT / "data" / "harness.db",
        description="SQLite path for session metadata index",
    )

    # === LLM Providers (Phase 0: cloud only) ===
    minimax_api_key: str = Field(default="", description="MiniMax API key")
    zhipuai_api_key: str = Field(default="", description="ZhipuAI (GLM) API key")
    moonshot_api_key: str = Field(default="", description="Moonshot (Kimi) API key")

    # === CORS ===
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",  # Vite dev
            "http://127.0.0.1:5173",
        ],
        description="Allowed CORS origins",
    )

    # === Agent Loop ===
    max_iterations: int = Field(
        default=5,
        ge=1,
        le=20,
        description="Max agent loop iterations per task (safety cap)",
    )

    # === Sub-agents (Phase 2) ===
    agents_dir: Path = Field(
        default=Path(".harness/agents"),
        description=(
            "Directory for user-editable sub-agent .md files (overrides built-ins). "
            "Resolved relative to settings.project_root."
        ),
    )
    subagent_default_model: str = Field(
        default="MiniMax-M2.7",
        description="Default LLM model id for built-in sub-agents (must be in catalog)",
    )
    subagent_judges: int = Field(
        default=2,
        ge=1,
        le=5,
        description="Number of adversarial judges (used by AdversarialVerify).",
    )
    subagent_timeout_s: float = Field(
        default=300.0,
        gt=0,
        description="Wall-clock cap (seconds) for a single sub-agent run (used by MergeQueue).",
    )

    # === Sub-agents cost-aware cascade (Phase 2.1) ===
    subagent_t1_model: str = Field(
        default="qwen3:8b",
        description=(
            "Tier-1 model id (cheap local, e.g. Ollama). Used by TierSelector "
            "when router confidence >= subagent_confidence_high. Set to empty "
            "string to disable T1 (cascade falls back to T2)."
        ),
    )
    subagent_t2_model: str = Field(
        default="glm-4.7",
        description=(
            "Tier-2 model id (cloud mid-tier). Used when confidence is in "
            "[subagent_confidence_low, subagent_confidence_high)."
        ),
    )
    subagent_confidence_high: float = Field(
        default=0.85,
        ge=0.0,
        le=1.0,
        description=(
            "Confidence >= this threshold -> Tier-1 (cheap local). "
            "Calibrate via Phase 5 eval harness. See docs/MODEL_REGISTRY.md."
        ),
    )
    subagent_confidence_low: float = Field(
        default=0.55,
        ge=0.0,
        le=1.0,
        description=(
            "Confidence in [low, high) -> Tier-2. Below low -> Tier-3 (premium). "
            "Calibrate via Phase 5 eval harness. See docs/MODEL_REGISTRY.md."
        ),
    )

    @model_validator(mode="after")
    def _cascade_thresholds_ordered(self) -> "Settings":
        """Guard against a misconfigured cascade: low must be strictly below high.

        When the operator sets ``subagent_confidence_low >= subagent_confidence_high``,
        no confidence value would fall in the [low, high) T2 band and the cascade
        would degenerate to a binary T1/T3. We reject the configuration at
        load time so the user notices immediately, not on the first router call.
        """
        if self.subagent_confidence_low >= self.subagent_confidence_high:
            raise ValueError(
                f"subagent_confidence_low ({self.subagent_confidence_low}) must be "
                f"< subagent_confidence_high ({self.subagent_confidence_high})"
            )
        return self


settings = Settings()
