"""Phase 7.6: Tests for LlmUsageLogger + router integration."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from harness.observability.llm_usage_log import LlmUsageLogger
from harness.server.llm.router import LLMRouter


def _make_completion_response(
    content: str = "Hello!",
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
) -> MagicMock:
    """Build a mock litellm completion response."""
    choice = MagicMock()
    choice.message.content = content
    choice.message.tool_calls = None
    choice.finish_reason = "stop"

    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    usage.total_tokens = prompt_tokens + completion_tokens

    response = MagicMock()
    response.choices = [choice]
    response.usage = usage
    response.model = "MiniMax-M2.7"
    return response


# === Unit tests for LlmUsageLogger ===


class TestLlmUsageLogger:
    """Unit tests for LlmUsageLogger — NDJSON append-only logger."""

    def test_log_usage_writes_jsonl(self, tmp_path: Path) -> None:
        """log_usage creates a valid JSONL file with one line."""
        path = tmp_path / "usage.jsonl"
        logger = LlmUsageLogger(path=path, enabled=True)

        logger.log_usage({
            "event": "llm_completion",
            "model": "MiniMax-M2.7",
            "tier": "T2",
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "cost_usd": 0.0001,
            "duration_s": 1.5,
            "status": "ok",
            "timestamp": "2026-06-21T00:00:00+00:00",
        })

        assert path.exists()
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["event"] == "llm_completion"
        assert data["model"] == "MiniMax-M2.7"
        assert data["prompt_tokens"] == 100
        assert data["completion_tokens"] == 50
        assert data["total_tokens"] == 150

    def test_log_usage_disabled_no_file(self, tmp_path: Path) -> None:
        """enabled=False → file is NOT created."""
        path = tmp_path / "disabled.jsonl"
        logger = LlmUsageLogger(path=path, enabled=False)

        logger.log_usage({"event": "llm_completion", "model": "test"})

        assert not path.exists()

    def test_log_usage_null_path_no_error(self) -> None:
        """path=None → no-op, no exception raised."""
        logger = LlmUsageLogger(path=None, enabled=True)

        # Should not raise
        logger.log_usage({"event": "llm_completion", "model": "test"})

    def test_log_usage_adds_timestamp(self, tmp_path: Path) -> None:
        """timestamp is auto-added when not present in the event dict."""
        path = tmp_path / "auto_ts.jsonl"
        logger = LlmUsageLogger(path=path, enabled=True)

        logger.log_usage({
            "event": "llm_completion",
            "model": "qwen3",
            "tier": "T1",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "cost_usd": 0.0,
            "duration_s": 0.1,
            "status": "ok",
            # no timestamp key
        })

        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8").strip())
        assert "timestamp" in data
        # Should be a valid ISO-8601 with timezone
        assert "+" in data["timestamp"] or "Z" in data["timestamp"]

    def test_log_usage_multiple_events(self, tmp_path: Path) -> None:
        """Multiple events append correctly — one JSON line each."""
        path = tmp_path / "multi.jsonl"
        logger = LlmUsageLogger(path=path, enabled=True)

        for i in range(3):
            logger.log_usage({
                "event": "llm_completion",
                "model": f"model-{i}",
                "tier": "T3",
                "prompt_tokens": 100 + i,
                "completion_tokens": 50 + i,
                "total_tokens": 150 + 2 * i,
                "cost_usd": 0.001 * (i + 1),
                "duration_s": 1.0 * (i + 1),
                "status": "ok",
                "timestamp": f"2026-06-21T0{i}:00:00+00:00",
            })

        assert path.exists()
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3
        for i, line in enumerate(lines):
            data = json.loads(line)
            assert data["model"] == f"model-{i}"
            assert data["prompt_tokens"] == 100 + i


# === Integration test: router emits NDJSON log ===


class TestRouterUsageLogIntegration:
    """Integration: LLMRouter writes to LlmUsageLogger after completion."""

    async def test_integration_router_emits_log(self, tmp_path: Path) -> None:
        """Mock LLM call → NDJSON file contains one usage event."""
        path = tmp_path / "router_usage.jsonl"
        usage_logger = LlmUsageLogger(path=path, enabled=True)

        with patch("harness.server.llm.router.litellm") as mock_litellm:
            mock_litellm.completion = AsyncMock(
                return_value=_make_completion_response("OK", 12, 4)
            )
            router = LLMRouter()
            router.set_usage_logger(usage_logger)

            result = await router.completion(
                messages=[{"role": "user", "content": "Hi"}],
                model="MiniMax-M2.7",
            )

            assert result.content == "OK"

        # Verify the NDJSON log was written
        assert path.exists()
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1, f"Expected 1 line, got {len(lines)}"
        data = json.loads(lines[0])
        assert data["event"] == "llm_completion"
        assert data["model"] == "MiniMax-M2.7"
        assert data["prompt_tokens"] == 12
        assert data["completion_tokens"] == 4
        assert data["total_tokens"] == 16
        assert data["status"] == "ok"
        assert "timestamp" in data
        assert "cost_usd" in data
        assert "duration_s" in data
