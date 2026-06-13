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
    port: int = Field(default=8000, description="Bind port")
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


settings = Settings()
