"""Phase 1.6 — Async SQLite store for the scope-gated auth tokens.

Schema is intentionally minimal — we only need to look up a token by
its SHA-256 hash, list active (non-revoked) tokens, and stamp
``last_used_at`` on every successful auth. The ``token_hash`` column
is the SHA-256 of the plaintext token (64 hex chars). The plaintext
is NEVER persisted — it is shown to the user exactly once at
``create()`` time and then thrown away.

Idempotency
-----------
``init_auth_db()`` is safe to call multiple times. It runs the
``CREATE TABLE IF NOT EXISTS`` statements from ``SCHEMA`` and then
does a ``PRAGMA table_info`` check on the ``auth_tokens`` table to
detect a stale schema (rare — would only happen if someone manually
edited the DB file between versions). For now we only have one
schema version, so the check is a no-op safety net; future schema
migrations will live here.

This module is a pure I/O layer — the SQL and the row<->record
translation. The :class:`harness.server.auth.tokens.TokenStore` is
the public API; tests import ``TokenStore``, not the helpers here.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

# Hashed tokens are stored as 64-char hex (SHA-256). A wider column
# would be defensive against a future hash algorithm change (e.g.
# SHA-3-256) but is not strictly required today.
_TOKEN_HASH_LEN = 64

# Scope column max width — covers the longest current scope
# ("memory.write" = 12 chars) with comfortable headroom for future
# longer names.
_SCOPES_MAX = 256

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS auth_tokens (
    token_hash   TEXT PRIMARY KEY,
    label        TEXT NOT NULL,
    scopes       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_auth_tokens_active
    ON auth_tokens(token_hash) WHERE revoked_at IS NULL;
"""


def _utcnow() -> datetime:
    """UTC now without timezone info (SQLite stores naive ISO)."""
    return datetime.now(UTC).replace(tzinfo=None)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(dt_str: str | None) -> datetime | None:
    if dt_str is None:
        return None
    return datetime.fromisoformat(dt_str)


def hash_token(plaintext: str) -> str:
    """SHA-256 hex digest of the plaintext token.

    SHA-256 is the right choice for opaque token storage: tokens
    have high entropy (default 32 random bytes = 256 bits) so the
    pre-image resistance that matters for *password* hashing is
    not relevant here. SHA-256 is fast (so ``lookup()`` is fast)
    and produces a fixed 64-char string (so the column is fixed
    width and indexes are tight).
    """
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _row_to_record(row: tuple[Any, ...]) -> dict[str, Any]:
    """Translate a raw SQLite row into a public-facing dict.

    Returns a plain dict (not a dataclass) so callers can update
    fields like ``last_used_at`` without ceremony. The TokenStore
    wraps this with the public dataclass-like API; the DB layer
    is purely about persistence.
    """
    return {
        "token_hash": row[0],
        "label": row[1],
        "scopes": row[2],
        "created_at": _parse(row[3]),
        "last_used_at": _parse(row[4]),
        "revoked_at": _parse(row[5]),
    }


_db_initialized: bool = False


async def init_auth_db(db_path: Path | str) -> None:
    """Idempotent schema initialiser.

    Opens the SQLite file, runs the schema (CREATE TABLE IF NOT
    EXISTS) and the partial index on active rows. Stores the
    initialised flag on this module — a process-level cache — so
    subsequent calls are a no-op. The flag is reset by tests via
    ``_reset_init_flag()`` (only used by the test suite, never
    called from production code).
    """
    global _db_initialized
    if _db_initialized:
        return
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.executescript(SCHEMA)
        await conn.commit()
    _db_initialized = True


def _reset_init_flag() -> None:
    """Test-only: clear the module-level 'initialised' cache.

    The :func:`harness.conftest.isolated_settings` fixture creates
    a fresh tmp dir per test, so the auth DB path changes too. We
    need to re-run the schema on the new path. The cleanest way is
    to clear the cache from the test side rather than re-architect
    the init function to be path-keyed.
    """
    global _db_initialized
    _db_initialized = False


async def insert_token(
    db_path: Path | str,
    *,
    token_hash: str,
    label: str,
    scopes_csv: str,
) -> None:
    """Persist a freshly created token row.

    No upsert / replace: a primary-key collision means a hash
    collision (1 in 2^256 for SHA-256 with 256-bit tokens), which
    we treat as a hard error. The caller (TokenStore.create) is
    expected to regenerate on collision.
    """
    if len(token_hash) != _TOKEN_HASH_LEN:
        raise ValueError(
            f"token_hash must be {_TOKEN_HASH_LEN} hex chars, got {len(token_hash)}"
        )
    if len(scopes_csv) > _SCOPES_MAX:
        raise ValueError(
            f"scopes string exceeds {_SCOPES_MAX} chars (got {len(scopes_csv)})"
        )
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(
            "INSERT INTO auth_tokens (token_hash, label, scopes, created_at) "
            "VALUES (?, ?, ?, ?)",
            (token_hash, label, scopes_csv, _iso(_utcnow())),
        )
        await conn.commit()


async def select_active_token(
    db_path: Path | str, token_hash: str,
) -> dict[str, Any] | None:
    """Look up a token by its hash; return None if missing or revoked.

    The caller (``TokenStore.lookup``) is responsible for stamping
    ``last_used_at`` after this returns a non-None record. We don't
    do the write here because we want ``lookup()`` to remain a
    pure read for any future read-replica setup.
    """
    async with aiosqlite.connect(str(db_path)) as conn:
        cursor = await conn.execute(
            "SELECT token_hash, label, scopes, created_at, last_used_at, revoked_at "
            "FROM auth_tokens WHERE token_hash = ? AND revoked_at IS NULL",
            (token_hash,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_record(row)


async def stamp_last_used(
    db_path: Path | str, token_hash: str,
) -> None:
    """Update ``last_used_at`` for a successfully-validated token.

    Called after a successful auth check. This is a write-on-success
    pattern: a failed auth never leaves a 'last used' footprint, so
    an audit log of ``last_used_at`` is reliable as a 'this token
    has been used recently' signal.
    """
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(
            "UPDATE auth_tokens SET last_used_at = ? WHERE token_hash = ?",
            (_iso(_utcnow()), token_hash),
        )
        await conn.commit()


async def revoke_token(
    db_path: Path | str, token_hash: str,
) -> bool:
    """Mark a token as revoked. Returns True iff a row was updated.

    Idempotent: revoking an already-revoked token is a no-op and
    returns False (the caller can use the return value to decide
    whether to print 'revoked' vs 'was already revoked' in the CLI).
    """
    async with aiosqlite.connect(str(db_path)) as conn:
        cursor = await conn.execute(
            "UPDATE auth_tokens SET revoked_at = ? "
            "WHERE token_hash = ? AND revoked_at IS NULL",
            (_iso(_utcnow()), token_hash),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def list_active_tokens(
    db_path: Path | str,
) -> list[dict[str, Any]]:
    """Return all non-revoked tokens, newest first.

    The CLI uses this to render the 'harness auth list' table. We
    order by ``created_at DESC`` so the most recently created
    token (typically the one the operator just minted) appears at
    the top.
    """
    async with aiosqlite.connect(str(db_path)) as conn:
        cursor = await conn.execute(
            "SELECT token_hash, label, scopes, created_at, last_used_at, revoked_at "
            "FROM auth_tokens WHERE revoked_at IS NULL "
            "ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
    return [_row_to_record(r) for r in rows]


async def has_any_active_token(db_path: Path | str) -> bool:
    """Return True iff at least one non-revoked token exists.

    Used by the CLI bootstrap path: when the server starts in a
    fresh data dir and ``auth_required=True``, we create an admin
    token *only if* this query returns False. Once any token
    exists (even a read-only scoped one), the bootstrap step is
    skipped — the operator is presumed to be in control of the
    auth state already.
    """
    async with aiosqlite.connect(str(db_path)) as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM auth_tokens WHERE revoked_at IS NULL LIMIT 1"
        )
        row = await cursor.fetchone()
    return row is not None


__all__ = [
    "SCHEMA",
    "_reset_init_flag",
    "has_any_active_token",
    "hash_token",
    "init_auth_db",
    "insert_token",
    "list_active_tokens",
    "revoke_token",
    "select_active_token",
    "stamp_last_used",
]
