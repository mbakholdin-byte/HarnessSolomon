"""Pytest config + fixtures for the test suite.

This file is loaded by pytest before any test module. It exposes:

* Async fixtures used by `test_sessions_api.py` and the new
  `test_smoke.py` (isolated data dir, isolated project_root,
  an `httpx.AsyncClient` bound to the in-process ASGI app).
* A sync ``client`` fixture used by `test_chat_ws.py` (starlette
  ``TestClient`` with WebSocket support).
* A ``session_id`` factory fixture used by smoke tests to create a
  fresh session via the REST API.

Markers
-------
* ``real_llm`` — tests that hit a real LLM provider. They are skipped
  automatically when ``MINIMAX_API_KEY`` (or any other provider key)
  is not set. To run them:

      pytest tests/test_smoke.py -v -m real_llm

  All other tests are mock-only and pass without any API key.

Run all mock tests (default CI mode):

    pytest tests/test_smoke.py -v
    pytest tests/ -v -m "not real_llm"

Run all tests including real LLM (requires key):

    pytest tests/test_smoke.py -v -m real_llm
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from asgi_lifespan import LifespanManager
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from harness.config import settings
from harness.server.app import create_app
from harness.server.db import sqlite as db_sqlite


# ---------------------------------------------------------------------------
# Data isolation (used by every test that touches the harness)
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> dict[str, Path]:
    """Point all settings paths at a fresh tmp dir and reset the DB.

    Yields the dict of paths actually used so tests can read/write
    ``project_root``, ``session_dir``, ``db_path`` and (Phase 1.6)
    ``auth_db_path`` as needed.
    """
    data_dir = tmp_path / "harness-data"
    project_root = tmp_path / "project-root"
    project_root.mkdir(parents=True, exist_ok=True)
    paths = {
        "data": data_dir,
        "project_root": project_root,
        "session_dir": data_dir / "sessions",
        "db_path": data_dir / "harness.db",
        "auth_db_path": data_dir / "harness-scope.db",
    }
    monkeypatch.setattr(settings, "session_dir", paths["session_dir"])
    monkeypatch.setattr(settings, "db_path", paths["db_path"])
    monkeypatch.setattr(settings, "project_root", paths["project_root"])
    # Phase 1.6: the auth token store lives in its own DB at a
    # sibling path. Re-point it at the tmp dir so the store is
    # rebuilt per-test, then reset the init flag so init_auth_db
    # actually runs against the new path.
    monkeypatch.setattr(settings, "auth_db_path", paths["auth_db_path"])
    from harness.server.auth import db as auth_db
    auth_db._reset_init_flag()
    # Test suite runs in "dev mode" — open auth by default so the
    # legacy Phase 0-2.2 tests don't need a token to hit /api/v1/*
    # endpoints. Tests that want to assert auth behaviour set
    # ``settings.auth_required = True`` explicitly (via
    # ``monkeypatch.setattr(settings, "auth_required", True)`` in
    # the test body or a per-test fixture).
    monkeypatch.setattr(settings, "auth_required", False)
    # Phase 2.3: the webhook receiver needs a non-empty secret for
    # HMAC verification. We set a test-only secret here so the
    # webhook tests can compute a valid HMAC; tests that want
    # "webhooks disabled" behaviour set this to ``""`` explicitly.
    # 32+ chars satisfies the "real-looking secret" assumption in
    # ``verify_github_signature`` (no upper bound enforced, but
    # GitHub's docs recommend 32+).
    monkeypatch.setattr(
        settings, "webhook_secret",
        "test-secret-32-chars-long-enough-for-hmac",
    )
    # Force the DB layer to re-init under the new path.
    db_sqlite._db_initialized = False
    return paths


# ---------------------------------------------------------------------------
# Async client + session factory (used by smoke tests and test_sessions_api)
# ---------------------------------------------------------------------------

@pytest.fixture
async def app(isolated_settings: dict[str, Path]):
    """A fresh FastAPI app built against the isolated settings."""
    return create_app()


@pytest.fixture
async def client(
    app,
) -> AsyncIterator[AsyncClient]:
    """An ``httpx.AsyncClient`` bound to the in-process ASGI transport.

    Use for REST calls: ``GET /api/sessions``, ``POST /api/sessions``,
    etc. The chat WebSocket tests prefer the sync ``TestClient`` (below)
    because starlette's WebSocket testing API is sync.

    v1.0.0 fix: trigger the FastAPI lifespan so ``app.state.auth_required``
    and ``app.state.token_store`` are initialised. ASGITransport alone
    does NOT run the lifespan — without it, ``require_scope`` cannot see
    the auth-required flag and 503s on every authenticated route.
    """
    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


@pytest.fixture
async def session_id(client: AsyncClient) -> str:
    """Create a new session and return its id."""
    r = await client.post(
        "/api/sessions", json={"title": "smoke-test", "model": "MiniMax-M2.7"}
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# Phase 1.6 — auth token store + token factory
# ---------------------------------------------------------------------------

@pytest.fixture
async def auth_store(isolated_settings: dict[str, Path]):
    """A :class:`TokenStore` pointed at the tmp auth DB.

    Initialised in place (the lifespan does the same thing for
    production). Returns the store object so tests can call
    ``create()`` / ``lookup()`` / ``revoke()`` directly.
    """
    from harness.server.auth.tokens import TokenStore
    store = TokenStore(isolated_settings["auth_db_path"])
    await store.init()
    return store


@pytest.fixture
def make_token(auth_store):
    """Factory: ``await make_token(label, scopes)`` -> ``(plaintext, record)``.

    The factory captures ``auth_store`` via closure so test code
    reads as a single line::

        plaintext, record = await make_token("my-test", {Scope.AGENTS_READ})
    """
    async def _factory(label: str, scopes: set | None = None):
        return await auth_store.create(label, scopes)
    return _factory


# ---------------------------------------------------------------------------
# Sync client with WebSocket support (used by test_chat_ws)
# ---------------------------------------------------------------------------

@pytest.fixture
def ws_client(
    isolated_settings: dict[str, Path],
) -> Iterator[TestClient]:
    """Starlette ``TestClient`` with isolated data dir.

    Kept as a *separate* fixture from ``client`` (the async one) because
    WebSocket testing in starlette is sync.
    """
    app = create_app()
    with TestClient(app) as tc:
        yield tc


# ---------------------------------------------------------------------------
# Git repo (used by Phase 2.0 worktree / merge-queue tests)
# ---------------------------------------------------------------------------

@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A throwaway git repository with one initial commit on ``main``.

    Initialised with ``git init -b main``, a configured user, a single
    ``README.md`` and a commit. Returns the repo directory. Each test
    gets its own repo under ``tmp_path``, so tests can run in parallel
    without colliding on ``.harness/worktrees/`` paths.
    """
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)

    def _git(*args: str, check: bool = True) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            check=check,
        )
        return proc.stdout

    _git("init", "-b", "main")
    _git("config", "user.email", "test@harness.local")
    _git("config", "user.name", "Harness Test")
    (repo / "README.md").write_text("# test repo\n", encoding="utf-8")
    _git("add", ".")
    _git("commit", "-m", "initial commit")
    return repo


@pytest.fixture
def agents_dir(tmp_path: Path) -> Path:
    """An empty ``.harness/agents/`` directory under a fresh project root.

    Returns the ``agents/`` directory itself (suitable for passing as
    ``project_root / '.harness' / 'agents'`` to the registry). The
    parent project root is at ``tmp_path / 'project'``.
    """
    d = tmp_path / "project" / ".harness" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Phase 2.1 — memory namespace + cascade fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def memory_namespace(tmp_path: Path) -> dict[str, Path]:
    """Four disjoint storage dirs for a single ``UnifiedMemory(agent_id=...)``.

    The harness/memory adapters are stateful on disk: each namespace needs
    its own ``hmem_dir`` / ``mem0_dir`` / ``hybrid_dir`` / ``file_dir``.
    Tests that exercise cross-namespace isolation use this fixture to
    spin up a fresh dir quadruple under ``tmp_path``.

    Returns a dict keyed by layer (hmem/mem0/hybrid/file). The caller
    unpacks into ``UnifiedMemory(..., **namespace_dirs, agent_id=...)``.
    """
    base = tmp_path / "memory"
    return {
        "hmem_dir":   base / "hmem",
        "mem0_dir":   base / "mem0",
        "hybrid_dir": base / "hybrid",
        "file_dir":   base / "file",
    }


@pytest.fixture
def cascade_decision() -> Any:
    """A factory for stub :class:`CascadeDecision` objects in tests.

    Returned as a callable so individual tests can build decisions
    with custom tiers/models without depending on the real
    ``TierSelector`` (which is what the cascade tests themselves verify).
    The factory is parametrized on the field most often overridden in
    tests (``tier``); other fields fall back to Phase 2.1 defaults.
    """
    from harness.agents.cascade import CascadeDecision  # lazy import
    return lambda **kw: CascadeDecision(
        chosen_model=kw.get("chosen_model", "glm-4.7"),
        tier=kw.get("tier", "T2"),
        reason=kw.get("reason", "fixture-default"),
    )


# ---------------------------------------------------------------------------
# Phase 2.2 — gh CLI subprocess stub
# ---------------------------------------------------------------------------

@pytest.fixture
def gh_subprocess_stub(monkeypatch: pytest.MonkeyPatch):
    """Replace ``harness.agents.pr_integration._gh`` with a programmable stub.

    Phase 2.2 tests use this to fake ``gh`` CLI calls without spawning
    a real subprocess. The fixture is a factory: pass a list of
    ``(args_predicate, returncode, stdout, stderr)`` tuples and the
    stub returns the matching response for each ``_gh`` call.

    Usage::

        def test_create_pr_parses_url(gh_subprocess_stub):
            gh_subprocess_stub([
                # First call: ``gh auth status`` returns 0 (authenticated)
                (("auth", "status"), 0, "Logged in to github.com\\n", ""),
                # Second call: ``gh pr create`` returns the PR URL
                (("pr", "create"), 0,
                 "https://github.com/owner/repo/pull/42\\n", ""),
            ])
            # ... call create_pr() and assert PRCreateResult(...)

    If no entries match, the stub returns ``(1, "", "gh: command not found")``
    so tests can detect "I forgot to register this call" rather than
    silently passing.
    """
    from harness.agents import pr_integration

    def _install(entries: list[tuple[tuple[str, ...], int, str, str]]) -> None:
        """Install a stub with a list of expected calls."""
        queue = list(entries)
        calls: list[tuple[str, ...]] = []

        async def stub_gh(*args: str, **kwargs: Any) -> tuple[int, str, str]:
            # Record only the positional args (no env= in records).
            calls.append(tuple(args))
            for i, (pred, rc, out, err) in enumerate(queue):
                if pred == tuple(args) or all(
                    a in tuple(args) for a in pred
                ):
                    queue.pop(i)
                    return (rc, out, err)
            # Phase 2.5 defaults: if no predicate matched, treat
            # ``gh auth status`` and ``gh pr edit --add-label`` as
            # successful no-ops so Phase 2.3 auto-merge tests (which
            # pre-date these calls) continue to pass without
            # modification. Tests that want to simulate a failure
            # register their own predicate for these commands.
            if len(args) >= 2 and args[0] == "auth" and args[1] == "status":
                return (0, "", "")
            if len(args) >= 3 and args[0] == "pr" and args[1] == "edit":
                return (0, "", "")
            # No match — return a generic failure.
            return (1, "", f"gh_subprocess_stub: no match for args={args}")

        monkeypatch.setattr(pr_integration, "_gh", stub_gh)
        # Stash the calls list on the module for test introspection.
        pr_integration._stub_calls = calls  # type: ignore[attr-defined]

    return _install


# ---------------------------------------------------------------------------
# Real LLM marker — auto-skip when no API key is set
# ---------------------------------------------------------------------------

# Any of these env vars is enough to "opt in" to real LLM tests.
_REAL_LLM_ENV_VARS = (
    "MINIMAX_API_KEY",
    "ZHIPUAI_API_KEY",
    "MOONSHOT_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)


def _has_real_llm_key() -> bool:
    return any(os.environ.get(name, "").strip() for name in _REAL_LLM_ENV_VARS)


# Apply the auto-skip at collection time. Tests that do NOT carry the
# ``real_llm`` marker are unaffected.
_default_reason = "no real LLM API key set (MINIMAX_API_KEY / ZHIPUAI_API_KEY / etc.)"


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip ``@pytest.mark.real_llm`` tests when no key is set."""
    if _has_real_llm_key():
        return
    skip_marker = pytest.mark.skip(reason=_default_reason)
    for item in items:
        if "real_llm" in item.keywords:
            item.add_marker(skip_marker)
