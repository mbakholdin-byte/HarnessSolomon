"""Tests for the Phase 1.6 ``harness auth`` CLI subcommand.

Covers:
  - ``harness auth create --label L --scopes S`` — prints plaintext,
    persists hash, returns 0
  - ``harness auth create --bootstrap`` — mints with ALL_SCOPES
  - ``harness auth create`` with no scopes → exit 2
  - ``harness auth create`` with unknown scope → exit 2
  - ``harness auth list`` — shows active tokens
  - ``harness auth revoke <label>`` — marks as revoked, list excludes
  - ``harness auth revoke <hash>`` — programmatic path
  - ``harness auth whoami <plaintext>`` — shows scopes, exits 0/1
  - ``harness auth whoami <wrong>`` — exits 1 with error
  - bootstrap creates admin token on first ``list`` invocation when
    ``auth_required=True`` (and no tokens exist)
  - bootstrap does NOT re-create on subsequent ``list`` invocations
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


def _run_cli(
    *args: str,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run ``python -m harness <args>`` and return the result.

    We pass an env that includes the project root via PYTHONPATH
    and accept ``env_extra`` (typically ``DB_PATH`` and friends)
    so the CLI writes to a tmp dir. Auth-required and
    auth_db_path are also re-pointed at the tmp dir.
    """
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "harness", *args],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.fixture
def isolated_cli_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> dict[str, str]:
    """Build a CLI env pointing all auth + agent DBs at ``tmp_path``.

    Returns a dict suitable for ``env_extra`` in :func:`_run_cli`.
    Tests that need to read back the store can use the same
    paths to instantiate a :class:`TokenStore` directly.
    """
    data = tmp_path / "cli-data"
    data.mkdir(parents=True, exist_ok=True)
    return {
        "DB_PATH": str(data / "harness.db"),
        "AUTH_DB_PATH": str(data / "harness-scope.db"),
        # Disable bootstrap auto-create so tests are deterministic
        # unless they explicitly flip this.
        "AUTH_REQUIRED": "true",
    }


# === create ===

class TestAuthCreate:
    def test_create_prints_token_and_persists(
        self, isolated_cli_env: dict[str, str],
    ) -> None:
        res = _run_cli(
            "auth", "create", "--label", "test-1",
            "--scopes", "agents.read, memory.read",
            env_extra=isolated_cli_env,
        )
        assert res.returncode == 0, res.stderr
        # Parseable stdout format.
        assert "token=" in res.stdout
        assert "label=test-1" in res.stdout
        assert "agents.read" in res.stdout
        assert "memory.read" in res.stdout
        # Verify the persisted hash by re-loading the store.
        from harness.server.auth.tokens import TokenStore
        import asyncio
        plaintext = res.stdout.split("token=")[1].split()[0]
        async def _check():
            store = TokenStore(isolated_cli_env["AUTH_DB_PATH"])
            await store.init()
            return await store.lookup(plaintext)
        rec = asyncio.run(_check())
        assert rec is not None
        assert rec.label == "test-1"

    def test_create_with_bootstrap_gets_all_scopes(
        self, isolated_cli_env: dict[str, str],
    ) -> None:
        res = _run_cli(
            "auth", "create", "--label", "admin-1", "--bootstrap",
            env_extra=isolated_cli_env,
        )
        assert res.returncode == 0, res.stderr
        # ALL_SCOPES is rendered as "*" in format_scopes.
        assert "scopes=*" in res.stdout

    def test_create_with_no_scopes_exits_2(
        self, isolated_cli_env: dict[str, str],
    ) -> None:
        # No --scopes, no --bootstrap, and no auth_default_scopes env.
        env = {**isolated_cli_env, "AUTH_DEFAULT_SCOPES": ""}
        res = _run_cli(
            "auth", "create", "--label", "no-scopes",
            env_extra=env,
        )
        assert res.returncode == 2
        assert "no scopes" in res.stderr.lower()

    def test_create_with_unknown_scope_exits_2(
        self, isolated_cli_env: dict[str, str],
    ) -> None:
        res = _run_cli(
            "auth", "create", "--label", "bad", "--scopes", "foo.bar",
            env_extra=isolated_cli_env,
        )
        assert res.returncode == 2
        assert "unknown scope" in res.stderr.lower()

    def test_create_prints_warning_to_stderr(
        self, isolated_cli_env: dict[str, str],
    ) -> None:
        res = _run_cli(
            "auth", "create", "--label", "w", "--scopes", "agents.read",
            env_extra=isolated_cli_env,
        )
        assert res.returncode == 0
        # Warning on stderr so scripts parsing stdout aren't surprised.
        assert "WARNING" in res.stderr
        assert "only time" in res.stderr


# === list ===

class TestAuthList:
    def test_list_empty(self, isolated_cli_env: dict[str, str]) -> None:
        # Disable bootstrap for this test by setting auth_required=false.
        env = {**isolated_cli_env, "AUTH_REQUIRED": "false"}
        res = _run_cli("auth", "list", env_extra=env)
        assert res.returncode == 0
        assert "no active tokens" in res.stderr.lower()

    def test_list_shows_active_tokens(
        self, isolated_cli_env: dict[str, str],
    ) -> None:
        env = {**isolated_cli_env, "AUTH_REQUIRED": "false"}
        # Create two tokens, then list.
        _run_cli(
            "auth", "create", "--label", "a", "--scopes", "agents.read",
            env_extra=env,
        )
        _run_cli(
            "auth", "create", "--label", "b", "--scopes", "memory.write",
            env_extra=env,
        )
        res = _run_cli("auth", "list", env_extra=env)
        assert res.returncode == 0
        assert "a" in res.stdout
        assert "b" in res.stdout
        assert "agents.read" in res.stdout
        assert "memory.write" in res.stdout

    def test_list_excludes_revoked(
        self, isolated_cli_env: dict[str, str],
    ) -> None:
        env = {**isolated_cli_env, "AUTH_REQUIRED": "false"}
        _run_cli(
            "auth", "create", "--label", "rev", "--scopes", "agents.read",
            env_extra=env,
        )
        _run_cli("auth", "revoke", "rev", env_extra=env)
        res = _run_cli("auth", "list", env_extra=env)
        assert res.returncode == 0
        # The label is not in the output.
        # (We can't just check `assert "rev" not in res.stdout`
        # because the truncated hash might happen to start with
        # the same letters — but the label column is distinctive
        # enough. Better: check the no-active-tokens branch.)
        # With only one (now-revoked) token, we should see empty.
        assert "no active tokens" in res.stderr.lower()


# === revoke ===

class TestAuthRevoke:
    def test_revoke_by_label(
        self, isolated_cli_env: dict[str, str],
    ) -> None:
        env = {**isolated_cli_env, "AUTH_REQUIRED": "false"}
        _run_cli(
            "auth", "create", "--label", "killme", "--scopes", "agents.read",
            env_extra=env,
        )
        res = _run_cli("auth", "revoke", "killme", env_extra=env)
        assert res.returncode == 0
        assert "revoked: killme" in res.stdout

    def test_revoke_unknown_label_exits_1(
        self, isolated_cli_env: dict[str, str],
    ) -> None:
        env = {**isolated_cli_env, "AUTH_REQUIRED": "false"}
        res = _run_cli("auth", "revoke", "does-not-exist", env_extra=env)
        assert res.returncode == 1
        assert "no active token" in res.stderr.lower()

    def test_revoke_by_hash(
        self, isolated_cli_env: dict[str, str],
    ) -> None:
        env = {**isolated_cli_env, "AUTH_REQUIRED": "false"}
        create_res = _run_cli(
            "auth", "create", "--label", "h", "--scopes", "agents.read",
            env_extra=env,
        )
        # Look up the hash directly.
        import asyncio
        from harness.server.auth.tokens import TokenStore
        async def _hash() -> str:
            store = TokenStore(isolated_cli_env["AUTH_DB_PATH"])
            await store.init()
            recs = await store.list_active()
            return recs[0].token_hash
        token_hash = asyncio.run(_hash())
        res = _run_cli("auth", "revoke", token_hash, env_extra=env)
        assert res.returncode == 0
        assert "revoked:" in res.stdout

    def test_revoke_same_token_twice_exits_1(
        self, isolated_cli_env: dict[str, str],
    ) -> None:
        env = {**isolated_cli_env, "AUTH_REQUIRED": "false"}
        _run_cli(
            "auth", "create", "--label", "x", "--scopes", "agents.read",
            env_extra=env,
        )
        _run_cli("auth", "revoke", "x", env_extra=env)
        res = _run_cli("auth", "revoke", "x", env_extra=env)
        assert res.returncode == 1


# === whoami ===

class TestAuthWhoami:
    def test_whoami_valid_token(
        self, isolated_cli_env: dict[str, str],
    ) -> None:
        env = {**isolated_cli_env, "AUTH_REQUIRED": "false"}
        create_res = _run_cli(
            "auth", "create", "--label", "w", "--scopes", "agents.read",
            env_extra=env,
        )
        plaintext = create_res.stdout.split("token=")[1].split()[0]
        res = _run_cli("auth", "whoami", plaintext, env_extra=env)
        assert res.returncode == 0
        assert "label        : w" in res.stdout
        assert "agents.read" in res.stdout
        assert "(active)" in res.stdout  # not revoked

    def test_whoami_invalid_token_exits_1(
        self, isolated_cli_env: dict[str, str],
    ) -> None:
        env = {**isolated_cli_env, "AUTH_REQUIRED": "false"}
        res = _run_cli("auth", "whoami", "not-a-real-token", env_extra=env)
        assert res.returncode == 1
        assert "invalid or revoked" in res.stderr


# === bootstrap ===

class TestBootstrap:
    def test_bootstrap_creates_admin_on_first_list(
        self, isolated_cli_env: dict[str, str],
    ) -> None:
        # auth_required defaults to True; we keep that.
        res = _run_cli("auth", "list", env_extra=isolated_cli_env)
        assert res.returncode == 0
        # Bootstrap message on stderr.
        assert "bootstrap-admin" in res.stderr
        # The list output now shows one token.
        assert "bootstrap-admin" in res.stdout

    def test_bootstrap_does_not_recreate_when_token_exists(
        self, isolated_cli_env: dict[str, str],
    ) -> None:
        # First call bootstraps.
        res1 = _run_cli("auth", "list", env_extra=isolated_cli_env)
        assert "bootstrap-admin" in res1.stderr
        # Second call should NOT print the bootstrap message again.
        res2 = _run_cli("auth", "list", env_extra=isolated_cli_env)
        assert "SAVE THIS" not in res2.stderr
        # And still only one token in the list. Each row ends
        # with the truncated hash + ellipsis.
        hash_lines = [
            line for line in res2.stdout.splitlines()
            if line.rstrip().endswith("...")
        ]
        assert len(hash_lines) == 1

    def test_bootstrap_only_runs_for_readonly_commands(
        self, isolated_cli_env: dict[str, str],
    ) -> None:
        # ``create`` should NOT auto-bootstrap — the user is
        # explicitly minting a token, no need to surprise them.
        res = _run_cli(
            "auth", "create", "--label", "user", "--scopes", "agents.read",
            env_extra=isolated_cli_env,
        )
        assert res.returncode == 0
        # Bootstrap message should NOT be in stderr.
        assert "bootstrap-admin" not in res.stderr
        # But the user-minted token IS there.
        assert "user" in res.stdout

    def test_bootstrap_skipped_when_auth_required_false(
        self, isolated_cli_env: dict[str, str],
    ) -> None:
        env = {**isolated_cli_env, "AUTH_REQUIRED": "false"}
        res = _run_cli("auth", "list", env_extra=env)
        assert res.returncode == 0
        # No bootstrap because auth is off.
        assert "bootstrap-admin" not in res.stderr
        # List is empty.
        assert "no active tokens" in res.stderr.lower()
