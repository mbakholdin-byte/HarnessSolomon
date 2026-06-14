"""Solomon Harness — configuration.

Loads from environment variables (and .env file in dev).
Single source of truth for all paths, ports, API keys.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
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


settings = Settings()
