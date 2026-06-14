"""Tests for the Phase 1.6 token store + scopes.

Covers:
  - ``parse_scopes``: round-trip, whitespace, case, unknown names
  - ``has_scope``: ANY match semantics, empty required = True
  - ``TokenStore.create``: returns plaintext + record, hash stored (not plaintext)
  - ``TokenStore.lookup``: happy path, wrong token, revoked, not-found
  - ``TokenStore.revoke``: removes from list_active, idempotent (False on second revoke)
  - ``TokenStore.list_active``: filters revoked, ordered by created_at DESC
  - Scope roundtrip via storage (set -> csv -> set)
"""
from __future__ import annotations

import pytest

from harness.server.auth.scopes import (
    ALL_SCOPES,
    Scope,
    format_scopes,
    has_scope,
    parse_scopes,
)
from harness.server.auth.tokens import TokenStore


# === Scope parsing ===

class TestParseScopes:
    def test_round_trip_simple(self) -> None:
        assert parse_scopes("agents.read") == {Scope.AGENTS_READ}

    def test_multiple_with_whitespace(self) -> None:
        result = parse_scopes("agents.read, memory.write , sessions.read")
        assert result == {
            Scope.AGENTS_READ, Scope.MEMORY_WRITE, Scope.SESSIONS_READ,
        }

    def test_case_insensitive(self) -> None:
        assert parse_scopes("AGENTS.READ") == {Scope.AGENTS_READ}
        assert parse_scopes("Agents.Read") == {Scope.AGENTS_READ}

    def test_empty_string_returns_empty_set(self) -> None:
        assert parse_scopes("") == set()
        assert parse_scopes("   ") == set()

    def test_unknown_scope_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown scope: 'foo.bar'"):
            parse_scopes("agents.read, foo.bar")

    def test_wildcard_is_rejected(self) -> None:
        """The ``*`` wildcard is a display format, not a parse format."""
        with pytest.raises(ValueError, match="unknown scope:"):
            parse_scopes("*")

    def test_all_scopes_parse(self) -> None:
        csv = ",".join(s.value for s in ALL_SCOPES)
        assert parse_scopes(csv) == ALL_SCOPES


class TestHasScope:
    def test_any_match_returns_true(self) -> None:
        token_scopes = {Scope.AGENTS_READ, Scope.MEMORY_WRITE}
        assert has_scope(token_scopes, {Scope.AGENTS_READ})
        assert has_scope(token_scopes, {Scope.MEMORY_WRITE})
        assert has_scope(
            token_scopes,
            {Scope.AGENTS_READ, Scope.SESSIONS_READ},  # one match = True
        )

    def test_no_match_returns_false(self) -> None:
        token_scopes = {Scope.AGENTS_READ}
        assert not has_scope(token_scopes, {Scope.MEMORY_WRITE})
        assert not has_scope(
            token_scopes, {Scope.MEMORY_WRITE, Scope.SESSIONS_READ},
        )

    def test_empty_required_returns_true(self) -> None:
        """``require_scope()`` with no args is a no-op."""
        assert has_scope(set(), set())
        assert has_scope({Scope.AGENTS_READ}, set())

    def test_format_scopes_all_returns_wildcard(self) -> None:
        assert format_scopes(ALL_SCOPES) == "*"

    def test_format_scopes_subset_sorted(self) -> None:
        assert (
            format_scopes({Scope.SESSIONS_READ, Scope.AGENTS_READ})
            == "agents.read, sessions.read"
        )


# === TokenStore CRUD ===

class TestTokenStoreCreate:
    async def test_create_returns_plaintext_and_record(
        self, auth_store: TokenStore, make_token,
    ) -> None:
        plaintext, record = await make_token(
            "test-1", {Scope.AGENTS_READ, Scope.MEMORY_READ},
        )
        # Plaintext is a non-empty opaque string.
        assert isinstance(plaintext, str)
        assert len(plaintext) >= 40  # 32 bytes -> 43 chars
        # Record is a TokenRecord with the requested label + scopes.
        assert record.label == "test-1"
        assert record.scopes == frozenset({Scope.AGENTS_READ, Scope.MEMORY_READ})
        assert record.is_active
        assert record.created_at is not None

    async def test_create_does_not_store_plaintext(
        self, auth_store: TokenStore, make_token,
    ) -> None:
        """The plaintext must never be persisted — only the SHA-256 hash.

        We verify this by:
          1. Confirming the returned ``token_hash`` is NOT the plaintext
          2. Confirming the hash matches ``hashlib.sha256(plaintext)``
        """
        import hashlib
        plaintext, record = await make_token("no-plaintext", {Scope.AGENTS_READ})
        assert record.token_hash != plaintext
        expected = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
        assert record.token_hash == expected

    async def test_create_empty_label_raises(
        self, auth_store: TokenStore,
    ) -> None:
        with pytest.raises(ValueError, match="label must be a non-empty"):
            await auth_store.create("", {Scope.AGENTS_READ})
        with pytest.raises(ValueError, match="label must be a non-empty"):
            await auth_store.create("   ", {Scope.AGENTS_READ})

    async def test_create_with_unknown_scope_raises(
        self, auth_store: TokenStore,
    ) -> None:
        with pytest.raises(ValueError, match="unknown scopes"):
            await auth_store.create("bad", {"not.a.scope"})  # type: ignore[arg-type]


class TestTokenStoreLookup:
    async def test_lookup_happy_path(
        self, auth_store: TokenStore, make_token,
    ) -> None:
        plaintext, _ = await make_token("lookup-ok", {Scope.AGENTS_READ})
        record = await auth_store.lookup(plaintext)
        assert record is not None
        assert record.label == "lookup-ok"
        assert Scope.AGENTS_READ in record.scopes
        # ``last_used_at`` is stamped on successful lookup.
        assert record.last_used_at is not None

    async def test_lookup_wrong_token_returns_none(
        self, auth_store: TokenStore,
    ) -> None:
        assert await auth_store.lookup("not-a-real-token") is None

    async def test_lookup_revoked_token_returns_none(
        self, auth_store: TokenStore, make_token,
    ) -> None:
        plaintext, record = await make_token("revoked-1", {Scope.AGENTS_READ})
        assert await auth_store.lookup(plaintext) is not None
        revoked = await auth_store.revoke(record.token_hash)
        assert revoked is True
        assert await auth_store.lookup(plaintext) is None

    async def test_lookup_empty_returns_none(
        self, auth_store: TokenStore,
    ) -> None:
        assert await auth_store.lookup("") is None


class TestTokenStoreRevoke:
    async def test_revoke_returns_true_then_false(
        self, auth_store: TokenStore, make_token,
    ) -> None:
        _, record = await make_token("revoke-1", {Scope.AGENTS_READ})
        assert await auth_store.revoke(record.token_hash) is True
        # Idempotent: second revoke returns False.
        assert await auth_store.revoke(record.token_hash) is False


class TestTokenStoreListActive:
    async def test_filters_revoked_and_orders_newest_first(
        self, auth_store: TokenStore, make_token,
    ) -> None:
        for i in range(3):
            await make_token(f"t-{i}", {Scope.AGENTS_READ})
        active = await auth_store.list_active()
        assert len(active) == 3
        # Newest first (last created -> first in list).
        assert [r.label for r in active] == ["t-2", "t-1", "t-0"]

        # Revoke the middle one, confirm it disappears.
        await auth_store.revoke(active[1].token_hash)
        active = await auth_store.list_active()
        assert [r.label for r in active] == ["t-2", "t-0"]

    async def test_empty_list(self, auth_store: TokenStore) -> None:
        assert await auth_store.list_active() == []
        assert await auth_store.has_any_active() is False

    async def test_has_any_active(
        self, auth_store: TokenStore, make_token,
    ) -> None:
        await make_token("only", {Scope.AGENTS_READ})
        assert await auth_store.has_any_active() is True
