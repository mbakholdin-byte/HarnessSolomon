"""Integration tests for ``POST /api/v1/sessions/{id}/compact`` (Phase 3 v1.4.0).

Covers:
  - 200 OK with valid CompactResult JSON on success
  - 503 if neither ``compact_trigger`` nor ``compactor`` is wired
  - 401 / 403 with auth — requires ``sessions.write`` scope
  - ``bypass_cache`` query param is forwarded to the trigger

We use lightweight stub triggers to keep the test path independent
from the real ``ContextCompactor`` (which is exercised in its own
suite). The HTTP route is the unit under test.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from harness.config import settings
from harness.server.app import create_app
from harness.server.auth.scopes import Scope
from harness.server.auth.tokens import TokenStore


def _make_app_with_state(
    *,
    compact_trigger: Any = None,
    compactor: Any = None,
    loader: Any = None,
    auth_store: TokenStore | None = None,
) -> FastAPI:
    """Build a fresh app and pre-populate ``app.state`` for /compact route."""
    app = create_app()
    app.state.auth_required = settings.auth_required
    if auth_store is None:
        auth_store = TokenStore(settings.auth_db_path)
    app.state.token_store = auth_store
    if compact_trigger is not None:
        app.state.compact_trigger = compact_trigger
    if compactor is not None:
        app.state.compactor = compactor
    if loader is not None:
        app.state.load_session_messages = loader
    return app


class _FakeCompactResult:
    def __init__(
        self,
        *,
        original_tokens: int = 1000,
        compacted_tokens: int = 200,
        summary_preview: str = "preview",
        cache_hit: bool = False,
    ) -> None:
        self.original_tokens = original_tokens
        self.compacted_tokens = compacted_tokens
        self.summary_preview = summary_preview
        self.cache_hit = cache_hit

    @property
    def saved_tokens(self) -> int:
        return max(0, self.original_tokens - self.compacted_tokens)


class TestCompactRouteHappyPath:
    async def test_200_with_compact_result_json(self, isolated_settings) -> None:
        result = _FakeCompactResult(
            original_tokens=2000, compacted_tokens=400,
            summary_preview="the gist", cache_hit=False,
        )
        trigger = MagicMock()
        trigger.compact_now = AsyncMock(return_value=result)

        app = _make_app_with_state(compact_trigger=trigger)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post("/api/v1/sessions/sess-1/compact")
        assert r.status_code == 200
        body = r.json()
        assert body["original_tokens"] == 2000
        assert body["compacted_tokens"] == 400
        assert body["saved_tokens"] == 1600
        assert body["summary_preview"] == "the gist"
        assert body["cache_hit"] is False
        # The trigger was called once with the right session_id.
        trigger.compact_now.assert_awaited_once()
        call = trigger.compact_now.call_args
        assert call.kwargs["session_id"] == "sess-1"
        assert call.kwargs["bypass_cache"] is False

    async def test_bypass_cache_query_param_forwarded(
        self, isolated_settings,
    ) -> None:
        result = _FakeCompactResult(cache_hit=True)
        trigger = MagicMock()
        trigger.compact_now = AsyncMock(return_value=result)

        app = _make_app_with_state(compact_trigger=trigger)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post("/api/v1/sessions/sess-1/compact?bypass_cache=true")
        assert r.status_code == 200
        assert r.json()["cache_hit"] is True
        call = trigger.compact_now.call_args
        assert call.kwargs["bypass_cache"] is True


class TestCompactRouteErrorPaths:
    async def test_503_when_no_compactor_or_trigger(self, isolated_settings) -> None:
        app = _make_app_with_state()  # neither compactor nor trigger
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post("/api/v1/sessions/sess-1/compact")
        assert r.status_code == 503
        assert "trigger" in r.json()["detail"].lower() or "compactor" in r.json()["detail"].lower()

    async def test_503_when_trigger_returns_none(self, isolated_settings) -> None:
        trigger = MagicMock()
        trigger.compact_now = AsyncMock(return_value=None)
        app = _make_app_with_state(compact_trigger=trigger)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post("/api/v1/sessions/sess-1/compact")
        assert r.status_code == 503
        assert "failed" in r.json()["detail"].lower()


class TestCompactRouteAuth:
    async def test_401_without_token_when_auth_required(
        self, isolated_settings, monkeypatch, make_token, auth_store,
    ) -> None:
        from harness.server.auth.scopes import Scope as S

        result = _FakeCompactResult()
        trigger = MagicMock()
        trigger.compact_now = AsyncMock(return_value=result)
        monkeypatch.setattr(settings, "auth_required", True)

        app = _make_app_with_state(compact_trigger=trigger, auth_store=auth_store)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post("/api/v1/sessions/sess-1/compact")
        assert r.status_code == 401

    async def test_403_with_wrong_scope(
        self, isolated_settings, monkeypatch, make_token, auth_store,
    ) -> None:
        from harness.server.auth.scopes import Scope as S

        result = _FakeCompactResult()
        trigger = MagicMock()
        trigger.compact_now = AsyncMock(return_value=result)
        monkeypatch.setattr(settings, "auth_required", True)

        # Mint a token with a different scope (memory.write is NOT sessions.write).
        plaintext, _ = await make_token("wrong-scope", {S.MEMORY_WRITE})
        app = _make_app_with_state(compact_trigger=trigger, auth_store=auth_store)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post(
                "/api/v1/sessions/sess-1/compact",
                headers={"Authorization": f"Bearer {plaintext}"},
            )
        assert r.status_code == 403
        assert "sessions.write" in r.json()["detail"]

    async def test_200_with_sessions_write_scope(
        self, isolated_settings, monkeypatch, make_token, auth_store,
    ) -> None:
        from harness.server.auth.scopes import Scope as S

        result = _FakeCompactResult()
        trigger = MagicMock()
        trigger.compact_now = AsyncMock(return_value=result)
        monkeypatch.setattr(settings, "auth_required", True)

        # Mint a token with sessions.write (the right scope).
        plaintext, _ = await make_token("right-scope", {S.SESSIONS_WRITE})
        app = _make_app_with_state(compact_trigger=trigger, auth_store=auth_store)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post(
                "/api/v1/sessions/sess-1/compact",
                headers={"Authorization": f"Bearer {plaintext}"},
            )
        assert r.status_code == 200
        assert r.json()["original_tokens"] == 1000


class TestCompactRouteLoaderIntegration:
    async def test_loader_provides_messages(self, isolated_settings) -> None:
        """If ``load_session_messages`` is wired, it is used to fetch messages."""
        result = _FakeCompactResult()
        trigger = MagicMock()
        trigger.compact_now = AsyncMock(return_value=result)

        async def _loader(session_id: str) -> list[dict[str, Any]]:
            return [
                {"role": "user", "content": f"hello {session_id}"},
                {"role": "assistant", "content": "world"},
            ]
        app = _make_app_with_state(
            compact_trigger=trigger, loader=_loader,
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post("/api/v1/sessions/sess-99/compact")
        assert r.status_code == 200
        call = trigger.compact_now.call_args
        messages = call.args[0]
        assert isinstance(messages, list)
        assert len(messages) == 2
        assert "hello sess-99" in messages[0]["content"]

    async def test_loader_exception_is_swallowed(self, isolated_settings) -> None:
        """Loader raising → fall back to empty messages, request still succeeds."""
        result = _FakeCompactResult()
        trigger = MagicMock()
        trigger.compact_now = AsyncMock(return_value=result)

        async def _loader(session_id: str) -> list[dict[str, Any]]:
            raise RuntimeError("db is locked")

        app = _make_app_with_state(
            compact_trigger=trigger, loader=_loader,
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://t") as ac:
            r = await ac.post("/api/v1/sessions/sess-1/compact")
        assert r.status_code == 200
        call = trigger.compact_now.call_args
        assert call.args[0] == []  # empty messages


# ---------------------------------------------------------------------------
# CLI subcommand test
# ---------------------------------------------------------------------------


class TestSessionsCompactCli:
    def test_cli_subcommand_is_registered(self) -> None:
        """``harness sessions compact --help`` parses without error."""
        from harness.cli import _build_parser

        parser = _build_parser()
        # The subparser must be present.
        # Use parse_args to confirm the parser is wired.
        try:
            args = parser.parse_args([
                "sessions", "compact", "--session", "sess-1",
                "--base-url", "http://localhost:9999",
            ])
        except SystemExit as exc:
            pytest.fail(f"CLI parser rejected compact subcommand: {exc}")
        assert args.command == "sessions"
        assert args.sessions_command == "compact"
        assert args.session == "sess-1"
        assert args.base_url == "http://localhost:9999"
        assert args.bypass_cache is False
        assert args.func is not None

    def test_cli_bypass_cache_flag(self) -> None:
        from harness.cli import _build_parser

        parser = _build_parser()
        args = parser.parse_args([
            "sessions", "compact", "--session", "s1", "--bypass-cache",
        ])
        assert args.bypass_cache is True

    def test_cli_requires_session(self) -> None:
        """Omitting ``--session`` should error out via argparse."""
        from harness.cli import _build_parser

        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["sessions", "compact"])
