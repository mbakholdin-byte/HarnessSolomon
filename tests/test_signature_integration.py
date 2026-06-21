"""Phase 7.4 WI-03: Signature verify integration tests.

End-to-end coverage for
:meth:`harness.security.trust_registry.TrustRegistry.verify_signature`
which now (since v1.32.0) delegates the crypto check to
:mod:`harness.plugins.signature` (Rust ``harness_perf`` when available,
pure-Python ``cryptography`` fallback otherwise).

The integration surface under test:

    TrustRegistry.verify_signature(public_key_hex, signature_hex, data)
        → trust check (registry membership)
        → hex decode (pk + sig)
        → harness.plugins.signature.verify_signature(pk_bytes, data, sig_bytes)
            → Rust (ed25519-dalek) or Python (cryptography)

Tests are backend-agnostic — they pass identically with or without the
Rust wheel built. :func:`test_rust_or_python_backend` additionally
probes :func:`harness.plugins.signature.is_rust_active` and asserts that
the chosen backend produces the correct result either way.
"""
from __future__ import annotations

from pathlib import Path  # noqa: F401  (kept for conftest symmetry / future tmp_path tests)

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

from harness.plugins.signature import (
    generate_keypair,
    is_rust_active,
    verify_signature as raw_verify,
)
from harness.security.trust_registry import TrustRegistry


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_signed_keypair(
    registry: TrustRegistry,
    name: str,
    data: bytes,
) -> tuple[str, str]:
    """Generate a keypair, register its public key, sign ``data``.

    Returns ``(pub_hex, sig_hex)`` where ``pub_hex`` is the
    ``"ed25519:"``-prefixed hex public key and ``sig_hex`` is the hex
    Ed25519 signature over ``data``.
    """
    pub_bytes, sec_bytes = generate_keypair()
    pub_hex = "ed25519:" + pub_bytes.hex()

    # Sign with cryptography — ``sec_bytes`` is the RFC 8032 32-byte seed.
    sk = Ed25519PrivateKey.from_private_bytes(sec_bytes)
    sig_bytes = sk.sign(data)
    sig_hex = sig_bytes.hex()

    registry.add_key(name, pub_hex)
    return pub_hex, sig_hex


@pytest.fixture
def empty_registry() -> TrustRegistry:
    """Fresh in-memory trust registry (no path, no keys)."""
    return TrustRegistry(path=None)


# ---------------------------------------------------------------------------
# Test 1: valid signature + trusted key → True
# ---------------------------------------------------------------------------


def test_valid_signature_passes(empty_registry: TrustRegistry) -> None:
    """A correctly-signed message under a trusted key must verify."""
    data = b"test data - hello world"
    pub_hex, sig_hex = _make_signed_keypair(
        empty_registry, "official-harness-team", data
    )

    assert empty_registry.verify_signature(pub_hex, sig_hex, data) is True


# ---------------------------------------------------------------------------
# Test 2: tampered signature → False
# ---------------------------------------------------------------------------


def test_invalid_signature_fails(empty_registry: TrustRegistry) -> None:
    """Flipping a single byte in the signature must fail verification."""
    data = b"important payload"
    pub_hex, sig_hex = _make_signed_keypair(
        empty_registry, "publisher-a", data
    )

    # Flip the last hex digit. We keep the length (128 hex chars) and
    # validity of the hex string, so the only thing being tested is the
    # crypto verification — not hex decoding.
    tampered = sig_hex[:-1] + ("0" if sig_hex[-1] != "0" else "1")
    assert tampered != sig_hex  # sanity

    assert empty_registry.verify_signature(pub_hex, tampered, data) is False


# ---------------------------------------------------------------------------
# Test 3: unknown / untrusted public key → False (even with a valid sig)
# ---------------------------------------------------------------------------


def test_unknown_key_rejected(empty_registry: TrustRegistry) -> None:
    """A valid signature under a key that is NOT in the registry → False.

    This is the trust check: even a cryptographically-valid signature is
    rejected if the public key was never registered.
    """
    data = b"signed by untrusted party"
    pub_bytes, sec_bytes = generate_keypair()
    pub_hex = "ed25519:" + pub_bytes.hex()

    sk = Ed25519PrivateKey.from_private_bytes(sec_bytes)
    sig_hex = sk.sign(data).hex()

    # Registry is empty — pub_hex is cryptographically valid but untrusted.
    assert empty_registry.verify_signature(pub_hex, sig_hex, data) is False


# ---------------------------------------------------------------------------
# Test 4: malformed hex inputs → False (not exception)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pub_hex, sig_hex, label",
    [
        # Odd-length hex (31 hex chars instead of 64) on the public key.
        ("ed25519:abc", "00" * 64, "bad-pub-hex-length"),
        # Non-hex characters in signature.
        ("ed25519:" + "ab" * 32, "zz" * 64, "non-hex-signature"),
        # Empty signature.
        ("ed25519:" + "ab" * 32, "", "empty-signature"),
        # Missing prefix is fine for the trust check (won't match) but we
        # still want to confirm the hex-decode path doesn't blow up when
        # a non-prefixed key is passed.
        ("not-even-hex-at-all!", "00" * 64, "garbage-pubkey-no-prefix"),
    ],
)
def test_malformed_hex_rejected(
    empty_registry: TrustRegistry,
    pub_hex: str,
    sig_hex: str,
    label: str,
) -> None:
    """Malformed hex must return ``False``, never raise.

    To reach the hex-decode path (Step 2) the trust check (Step 1) has
    to pass first — so we pre-register a key whose value is exactly
    ``pub_hex``. For the garbage-pubkey-no-prefix case the trust check
    will itself reject it (it is registered as-is but does not start
    with ``ed25519:`` — ``add_key`` will raise). That case is replaced
    by direct insertion into ``_keys`` so we exercise the decode path.
    """
    # Manually inject so we bypass ``add_key``'s prefix validation and
    # exercise the decode-error branch in ``verify_signature``.
    empty_registry._keys["test-bad-input"] = pub_hex

    data = b"some data"
    # Must never raise — always returns False.
    assert empty_registry.verify_signature(pub_hex, sig_hex, data) is False, (
        f"case {label!r} should return False"
    )


# ---------------------------------------------------------------------------
# Test 5: backend-agnostic — works under either Rust or Python fallback
# ---------------------------------------------------------------------------


def test_rust_or_python_backend(empty_registry: TrustRegistry) -> None:
    """``verify_signature`` must produce correct results regardless of
    whether the Rust extension (``harness_perf``) or the pure-Python
    fallback is active.

    We record which backend is live, then run the full happy-path and
    a negative case through the registry, asserting the expected
    booleans. This guards against regressions in either code path.
    """
    backend = "rust" if is_rust_active() else "python"

    # Happy path — sign with the active backend's keypair, verify.
    data = b"backend-agnostic payload"
    pub_hex, sig_hex = _make_signed_keypair(
        empty_registry, f"backend-test-{backend}", data
    )

    # The raw module must agree (sanity).
    pk_bytes = bytes.fromhex(pub_hex.removeprefix("ed25519:"))
    sig_bytes = bytes.fromhex(sig_hex)
    assert raw_verify(pk_bytes, data, sig_bytes) is True

    # And the registry path must agree too.
    assert empty_registry.verify_signature(pub_hex, sig_hex, data) is True

    # Negative case — wrong data.
    assert empty_registry.verify_signature(pub_hex, sig_hex, b"other data") is False

    # Record which backend ran. We deliberately use ``print`` (captured by
    # pytest's ``capsys``) rather than ``pytest.mark.backend`` to avoid
    # requiring a custom mark registration in ``pyproject.toml``.
    print(f"\n[backend-in-use] {backend}")
