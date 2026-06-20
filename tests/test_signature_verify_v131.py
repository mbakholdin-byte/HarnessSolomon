"""Tests for WI-07 v1.31: Ed25519 signature verification (Rust + Python).

Four tests covering the ``harness.plugins.signature`` module:

1. **Valid signature passes** — generate keypair, sign, verify → True.
2. **Invalid signature fails** — tamper with message → False.
3. **Wrong key fails** — verify with a different public key → False.
4. **Python fallback works** — force the fallback path by mocking
   ``_rust_available`` to return ``False``, then verify the
   ``cryptography``-backed path produces identical results.

The tests use ``cryptography`` to sign (both for the Rust-path tests
and the fallback test) so that the signing side is identical — only
the verification backend varies. This isolates the variable under test.

Run::

    python -m pytest tests/test_signature_verify_v131.py -v
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from harness.plugins import signature as sig_mod
from harness.plugins.signature import generate_keypair, verify_signature

# ── Helpers ─────────────────────────────────────────────────────────


def _sign(message: bytes, secret: bytes) -> bytes:
    """Sign ``message`` with a 32-byte Ed25519 secret seed.

    Uses ``cryptography`` — this is the reference signer for all tests.
    """
    sk = Ed25519PrivateKey.from_private_bytes(secret)
    return sk.sign(message)


# ── Tests ───────────────────────────────────────────────────────────


class TestVerifySignature:
    """Grouped tests for ``verify_signature``."""

    def test_valid_signature_passes(self) -> None:
        """A correctly signed message must verify to ``True``.

        Uses whichever backend is active (Rust if the wheel is
        installed, Python fallback otherwise).
        """
        public_key, secret_key = generate_keypair()
        message = b"payload to sign"
        signature = _sign(message, secret_key)

        assert verify_signature(public_key, message, signature) is True

    def test_invalid_signature_fails(self) -> None:
        """Tampering with the message after signing must yield ``False``."""
        public_key, secret_key = generate_keypair()
        original_message = b"original content"
        tampered_message = b"tampered content"
        signature = _sign(original_message, secret_key)

        assert verify_signature(public_key, tampered_message, signature) is False

    def test_wrong_key_fails(self) -> None:
        """A valid signature verified against a different public key → False."""
        # Keypair A signs the message.
        pub_a, sec_a = generate_keypair()
        # Keypair B is unrelated.
        pub_b, _sec_b = generate_keypair()
        message = b"signed by A"
        signature = _sign(message, sec_a)

        # Verify against B's public key — must fail.
        assert verify_signature(pub_b, message, signature) is False

    def test_python_fallback_works(self) -> None:
        """When the Rust wheel is unavailable, the Python fallback works.

        We mock ``_rust_available`` to return ``False`` so the wrapper
        takes the ``cryptography`` path even if the Rust extension is
        installed. This ensures the fallback is always exercised in CI
        regardless of whether ``maturin develop`` was run.
        """
        # Generate a keypair — this call itself uses Rust if available,
        # but we only need the bytes for the fallback test below.
        public_key, secret_key = generate_keypair()
        message = b"fallback test"
        signature = _sign(message, secret_key)

        # Force the Python fallback path.
        with patch.object(sig_mod, "_rust_available", return_value=False):
            # Also patch the cached probe so ``is_rust_active`` does not
            # short-circuit (though ``verify_signature`` calls
            # ``_rust_available`` directly, not the cached wrapper).
            with patch.object(
                sig_mod, "is_rust_active", return_value=False
            ):
                result = verify_signature(public_key, message, signature)

        assert result is True, "Python fallback must verify a valid signature"

        # Tampered message must still fail on the fallback path.
        with patch.object(sig_mod, "_rust_available", return_value=False):
            result_bad = verify_signature(public_key, b"wrong", signature)

        assert result_bad is False, (
            "Python fallback must reject a tampered message"
        )


class TestGenerateKeypair:
    """Smoke tests for ``generate_keypair``."""

    def test_keypair_lengths(self) -> None:
        """Generated keys must be the canonical RFC 8032 lengths."""
        public_key, secret_key = generate_keypair()
        assert len(public_key) == 32, "public key must be 32 bytes"
        assert len(secret_key) == 32, "secret seed must be 32 bytes"

    def test_keypair_is_bytes(self) -> None:
        """Both keys must be ``bytes`` (not ``list[int]``)."""
        public_key, secret_key = generate_keypair()
        assert isinstance(public_key, bytes), (
            "public key must be bytes, not list"
        )
        assert isinstance(secret_key, bytes), (
            "secret key must be bytes, not list"
        )

    def test_generated_secret_derives_public(self) -> None:
        """Reconstructing a signing key from the secret seed must yield
        the same public key. This validates the keypair is internally
        consistent."""
        public_key, secret_key = generate_keypair()
        sk = Ed25519PrivateKey.from_private_bytes(secret_key)
        derived_pub = sk.public_key().public_bytes(
            encoding=__import__(
                "cryptography.hazmat.primitives.serialization",
                fromlist=["Encoding"],
            ).Encoding.Raw,
            format=__import__(
                "cryptography.hazmat.primitives.serialization",
                fromlist=["PublicFormat"],
            ).PublicFormat.Raw,
        )
        assert derived_pub == public_key, (
            "secret seed must derive the same public key"
        )
