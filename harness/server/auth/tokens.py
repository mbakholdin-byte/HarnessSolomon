"""Phase 1.6 — ``TokenStore``: the public API for the auth token table.

This is the layer route code and CLI code use. The aiosqlite plumbing
lives in :mod:`harness.server.auth.db`; this module is the dataclass-ish
``TokenRecord`` and the operations (``create``, ``lookup``, ``revoke``,
``list_active``) that the rest of the codebase calls.

Token lifecycle
---------------
1. ``create(label, scopes)`` generates ``secrets.token_bytes(N)``,
   base64-url-encodes it (43 chars for 32 bytes), stores the SHA-256
   hash, and returns the plaintext to the caller — *exactly once*.
2. ``lookup(plaintext)`` hashes the candidate token, queries the
   table, and returns the ``TokenRecord`` (or None if missing /
   revoked). On hit, stamps ``last_used_at``.
3. ``revoke(token_hash)`` marks the row as revoked. The plaintext
   cannot be recovered from the hash, so the operator must keep
   their plaintext or be prepared to mint a new one.

Concurrency
-----------
SQLite under aiosqlite serialises writes. We open a fresh
connection per call (matching the rest of the harness codebase —
see ``harness/server/db/sqlite.py``) so test isolation is just
"point at a different ``db_path``".
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from harness.config import settings
from harness.server.auth import db as auth_db
from harness.server.auth.scopes import ALL_SCOPES, Scope, parse_scopes


def _gen_token_plaintext(nbytes: int) -> str:
    """Generate a fresh opaque token (URL-safe, no padding).

    We use ``secrets.token_urlsafe`` rather than ``hex`` because the
    URL-safe alphabet (A-Z / a-z / 0-9 / '-' / '_') produces shorter
    strings for the same entropy: 32 bytes → 43 chars (vs 64 hex).
    This makes the token easier to copy from a terminal and to pass
    in ``Authorization: Bearer ...`` headers without wrapping.
    """
    return secrets.token_urlsafe(nbytes)


@dataclass(frozen=True)
class TokenRecord:
    """A row from the ``auth_tokens`` table, public-API-shaped.

    ``token_hash`` is the SHA-256 of the plaintext (never the
    plaintext itself). The plaintext is *not* stored anywhere
    after ``create()`` returns.

    ``scopes`` is a ``frozenset[Scope]`` rather than the raw
    comma-separated string from the DB. The conversion is done
    once in :meth:`TokenStore.lookup` so callers can write
    ``if Scope.AGENTS_READ in token.scopes`` without parsing.

    The dataclass is frozen so a token record can be safely
    passed across function boundaries without anyone mutating
    the scopes or label out from under us.
    """

    token_hash: str
    label: str
    scopes: frozenset[Scope] = field(default_factory=frozenset)
    created_at: datetime | None = None
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None


class TokenStore:
    """Async CRUD facade over the auth_tokens table.

    All methods are coroutines because they open an aiosqlite
    connection. The DB path is read from ``settings.auth_db_path``
    at construction time so the FastAPI lifespan handler and the
    CLI (which both go through ``settings``) stay in sync.
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else settings.auth_db_path

    async def init(self) -> None:
        """Idempotent schema init. Called by the FastAPI lifespan."""
        await auth_db.init_auth_db(self.db_path)

    # === Create ===

    async def create(
        self,
        label: str,
        scopes: set[Scope] | None = None,
    ) -> tuple[str, TokenRecord]:
        """Create a new token, returning ``(plaintext, record)``.

        The plaintext is generated here and returned to the caller.
        It is NOT stored — only the SHA-256 hash is persisted. The
        caller is responsible for showing it to the user exactly
        once (the CLI does this; the HTTP route, when added in a
        later phase, will return it in the response body with a
        'save this — it will not be shown again' warning header).

        The label is required (no anonymous tokens — every token
        should be attributable to the operator / service that
        minted it). If ``scopes`` is None, we use
        ``settings.auth_default_scopes``; that setting is empty
        by default, which means a default ``create()`` returns
        a token with no permissions. The CLI explicitly passes
        ``--scopes``; only the bootstrap path passes ALL_SCOPES.
        """
        if not label or not label.strip():
            raise ValueError("label must be a non-empty string")
        if scopes is None:
            scopes = parse_scopes(settings.auth_default_scopes)
        # Validate the scope set early — better to fail here than
        # to write a row that will fail every auth check.
        unknown = scopes - ALL_SCOPES
        if unknown:
            raise ValueError(f"unknown scopes: {unknown}")

        plaintext = _gen_token_plaintext(settings.auth_token_bytes)
        token_hash = auth_db.hash_token(plaintext)
        scopes_csv = ",".join(sorted(s.value for s in scopes))

        # SHA-256 collisions are 1-in-2^256 for distinct plaintexts,
        # so a primary-key collision here is an impossible-by-construction
        # condition. We don't retry on collision; we raise so the
        # operator notices the impossible.
        await auth_db.insert_token(
            self.db_path,
            token_hash=token_hash,
            label=label.strip(),
            scopes_csv=scopes_csv,
        )
        record = await self._load_by_hash(token_hash)
        if record is None:  # pragma: no cover — defensive
            raise RuntimeError(
                "token row vanished between insert and select "
                "(concurrent wipe?)"
            )
        return plaintext, record

    # === Lookup ===

    async def lookup(self, plaintext: str) -> TokenRecord | None:
        """Look up by plaintext, return ``TokenRecord`` or None.

        On hit, stamps ``last_used_at`` so the CLI can show
        'last used 2 minutes ago' for ops debugging. The stamp
        is best-effort: if the write fails (e.g. DB locked), the
        lookup still succeeded — we don't reject a valid auth
        because of an audit-log write failure. We re-fetch the
        row after stamping so the returned ``TokenRecord`` carries
        the up-to-date ``last_used_at``.
        """
        if not plaintext:
            return None
        token_hash = auth_db.hash_token(plaintext)
        record = await self._load_by_hash(token_hash)
        if record is None:
            return None
        try:
            await auth_db.stamp_last_used(self.db_path, token_hash)
        except Exception:  # noqa: BLE001 — audit stamp is best-effort
            return record
        refreshed = await self._load_by_hash(token_hash)
        return refreshed if refreshed is not None else record

    async def _load_by_hash(self, token_hash: str) -> TokenRecord | None:
        raw = await auth_db.select_active_token(self.db_path, token_hash)
        if raw is None:
            return None
        return _record_from_raw(raw)

    # === Revoke ===

    async def revoke(self, token_hash: str) -> bool:
        """Revoke by hash. Returns True iff a row was updated."""
        return await auth_db.revoke_token(self.db_path, token_hash)

    # === List ===

    async def list_active(self) -> list[TokenRecord]:
        """All non-revoked tokens, newest first."""
        rows = await auth_db.list_active_tokens(self.db_path)
        return [_record_from_raw(r) for r in rows]

    # === Bootstrap ===

    async def has_any_active(self) -> bool:
        """True iff at least one non-revoked token exists.

        The CLI's bootstrap path uses this to decide whether to
        mint an admin token on first run. We keep the method on
        the store (rather than reaching into ``auth_db``) so
        tests can mock the high-level operation cleanly.
        """
        return await auth_db.has_any_active_token(self.db_path)


# === Internal helpers ===

def _record_from_raw(raw: dict[str, Any]) -> TokenRecord:
    """Convert the DB-layer dict into a public ``TokenRecord``.

    The DB layer returns ``scopes`` as a comma-separated string;
    we parse it back into a ``frozenset[Scope]`` here. Unknown
    scope strings in a stored row are silently dropped (we
    don't want a stale enum value from a previous version to
    break lookups forever; better to log + skip than to fail).
    """
    raw_scopes = raw.get("scopes") or ""
    parsed: set[Scope] = set()
    if raw_scopes == "*":
        parsed = set(ALL_SCOPES)
    else:
        for s in raw_scopes.split(","):
            name = s.strip()
            if not name:
                continue
            try:
                parsed.add(Scope(name))
            except ValueError:
                # Unknown scope in stored row (older version, or
                # manual DB edit). Skip rather than crash.
                continue
    return TokenRecord(
        token_hash=raw["token_hash"],
        label=raw["label"],
        scopes=frozenset(parsed),
        created_at=raw.get("created_at"),
        last_used_at=raw.get("last_used_at"),
        revoked_at=raw.get("revoked_at"),
    )


__all__ = ["TokenRecord", "TokenStore"]
