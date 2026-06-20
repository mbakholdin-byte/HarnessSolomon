"""Phase 6.5 WI-07 v1.31: Ed25519 signature verification — Rust fast path.

Wraps the optional Rust extension ``harness_perf.verify_signature`` /
``harness_perf.generate_keypair`` (ed25519-dalek 2.x) with a pure-Python
fallback backed by ``cryptography``. Used by plugin integrity checks and
the agent runner to verify Ed25519 signatures on tool results, hook
emissions, and plugin manifests.

Trust boundary:
    The Rust module is a leaf dependency — it does NOT import any
    ``harness.*`` code and operates purely on ``bytes``. This wrapper is
    the only place that bridges into the harness package.

Fallback policy:
    On any ``ImportError`` (Rust wheel not built, wrong Python ABI,
    platform without a Rust toolchain) we transparently fall back to the
    ``cryptography`` library's Ed25519 implementation. The observable
    output is identical — both paths return ``bool``.

API:
    * ``verify_signature(public_key, message, signature) -> bool``
    * ``generate_keypair() -> tuple[bytes, bytes]``
    * ``is_rust_active() -> bool`` — probe for tests / observability
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

__all__ = [
    "verify_signature",
    "generate_keypair",
    "is_rust_active",
]

#: Ed25519 public key length in bytes (RFC 8032).
PUBLIC_KEY_LENGTH: int = 32
#: Ed25519 signature length in bytes (RFC 8032).
SIGNATURE_LENGTH: int = 64
#: Ed25519 secret (seed) key length in bytes (RFC 8032).
SECRET_KEY_LENGTH: int = 32


def _rust_available() -> bool:
    """Return ``True`` iff the ``harness_perf`` Rust extension imports.

    Cached so the import probe runs at most once per process. We never
    re-check after a failure — once the wheel is missing it stays missing
    until the process restarts.
    """
    try:
        import harness_perf  # noqa: F401  (import side-effect only)
    except ImportError:
        return False
    return True


@lru_cache(maxsize=1)
def is_rust_active() -> bool:
    """Public probe: is the Rust fast path currently in use?

    Exposed for tests and observability — callers should not branch on
    this (always call :func:`verify_signature`, which picks the right
    backend internally).
    """
    return _rust_available()


# ── Verify ──────────────────────────────────────────────────────────


def _verify_rust(
    public_key: bytes,
    message: bytes,
    signature: bytes,
) -> bool:
    """Rust fast path: delegate to ``harness_perf.verify_signature``."""
    import harness_perf

    return harness_perf.verify_signature(public_key, message, signature)


def _verify_python(
    public_key: bytes,
    message: bytes,
    signature: bytes,
) -> bool:
    """Pure-Python fallback using ``cryptography``.

    Raises ``InvalidSignature`` on failure; we catch and return ``False``
    so both paths have identical observable behaviour.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PublicKey,
    )

    try:
        vk = Ed25519PublicKey.from_public_bytes(public_key)
        vk.verify(signature, message)
        return True
    except (InvalidSignature, ValueError, TypeError):
        # ``ValueError``: bad key length or malformed point encoding.
        # ``TypeError``: wrong argument types (defensive — callers
        # should pass ``bytes``, but we guard regardless).
        return False


def verify_signature(
    public_key: bytes,
    message: bytes,
    signature: bytes,
) -> bool:
    """Verify an Ed25519 signature.

    Args:
        public_key: 32-byte Ed25519 public key.
        message:    Signed message (arbitrary length bytes).
        signature:  64-byte Ed25519 signature.

    Returns:
        ``True`` iff ``signature`` is a valid Ed25519 signature of
        ``message`` under ``public_key``. ``False`` on any failure —
        bad signature, wrong key, malformed inputs. Never raises.

    Notes:
        * Both the Rust and Python backends return ``bool`` and never
          raise — malformed inputs yield ``False``.
        * The Rust path is ~5-10× faster for batch verification (no
          Python exception machinery on each failure).
    """
    if not isinstance(public_key, (bytes, bytearray)):
        return False
    if not isinstance(message, (bytes, bytearray)):
        return False
    if not isinstance(signature, (bytes, bytearray)):
        return False

    # ``bytearray`` is accepted for ergonomic calling (some callers build
    # buffers incrementally). Rust FFI and cryptography both expect
    # ``bytes``; convert once here so the backends get a stable type.
    pk = bytes(public_key)
    msg = bytes(message)
    sig = bytes(signature)

    if _rust_available():
        return _verify_rust(pk, msg, sig)
    return _verify_python(pk, msg, sig)


# ── Generate ────────────────────────────────────────────────────────


def _generate_rust() -> tuple[bytes, bytes]:
    """Rust fast path: delegate to ``harness_perf.generate_keypair``."""
    import harness_perf

    return harness_perf.generate_keypair()


def _generate_python() -> tuple[bytes, bytes]:
    """Pure-Python fallback using ``cryptography``.

    Returns ``(public_bytes, private_seed_bytes)`` where the private
    seed is the 32-byte RFC 8032 encoding (not the 64-byte PKCS#8 form).
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        encoding=Encoding.Raw,
        format=PrivateFormat.Raw,
        encryption_algorithm=NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        encoding=Encoding.Raw,
        format=PublicFormat.Raw,
    )
    return (public_bytes, private_bytes)


def generate_keypair() -> tuple[bytes, bytes]:
    """Generate a fresh Ed25519 keypair using the OS CSPRNG.

    Returns:
        ``(public_bytes, secret_bytes)`` — two ``bytes`` objects.

        * ``public_bytes``: 32-byte Ed25519 verifying (public) key.
        * ``secret_bytes``: 32-byte Ed25519 signing (secret) key seed
          (RFC 8032 encoding, NOT PKCS#8).

    Notes:
        * Uses the OS CSPRNG (``OsRng`` in Rust, ``OsRng`` via
          ``cryptography`` in Python). Safe for production key generation.
        * The returned secret seed can reconstruct a full signing key
          via ``Ed25519PrivateKey.from_private_bytes(secret_bytes)``
          (Python) or ``SigningKey::from_bytes(&secret_bytes)``
          (Rust).
    """
    if _rust_available():
        return _generate_rust()
    return _generate_python()
