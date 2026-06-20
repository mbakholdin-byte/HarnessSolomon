//! Ed25519 signature verification — Rust fast path.
//!
//! Wraps ``ed25519-dalek`` 2.x to expose two operations to Python:
//!   * [`verify_signature`] — verify an Ed25519 signature against a
//!     public key and message. Returns ``true`` on success, ``false`` on
//!     any verification failure (including malformed keys/signatures).
//!   * [`generate_keypair`] — generate a fresh random keypair for
//!     testing / bootstrapping. Returns ``(public_bytes, secret_bytes)``
//!     where each is a fixed-size byte array (32 bytes).
//!
//! # API contract with Python
//!
//! * ``verify_signature(public_key_bytes, message, signature_bytes)``
//!   - ``public_key_bytes``: ``bytes`` (32 bytes, Ed25519 public key).
//!   - ``message``: ``bytes`` (arbitrary length — the signed payload).
//!   - ``signature_bytes``: ``bytes`` (64 bytes, Ed25519 signature).
//!   - Returns: ``bool``. ``True`` iff the signature is valid for
//!     ``(public_key, message)``. Any input length mismatch or
//!     malformed point → ``False`` (never raises).
//!
//! * ``generate_keypair()``
//!   - Returns: ``tuple[bytes, bytes]`` — ``(public_32, secret_32)``.
//!   - Uses ``OsRng`` (OS CSPRNG). Safe for production key generation.
//!
//! # Trust boundary
//!
//! This module does NOT depend on any ``harness.*`` code. All inputs are
//! plain byte slices. See ``harness/plugins/signature.py`` for the Python
//! wrapper that prefers this path and falls back to ``cryptography``
//! when the Rust wheel is not installed.
//!
//! # Why ``false`` instead of raising
//!
//! Signature verification on untrusted network input is a common case.
//! Raising on bad signatures would force every caller to wrap in
//! ``try/except``. Returning ``bool`` keeps the API simple and matches
//! the convention of the Python fallback (``Ed25519PublicKey.verify``
//! raises ``InvalidSignature``; the wrapper catches and returns
//! ``False``). The pure-Rust ``verify_signature_inner`` below returns
//! ``Result<bool, String>`` so unit tests can inspect the error, but the
//! ``#[pyfunction]`` shim discards the error string and returns ``false``.

use ed25519_dalek::{Signature, SigningKey, Verifier, VerifyingKey};
use pyo3::prelude::*;
use pyo3::types::PyBytes;

/// Ed25519 public-key length in bytes (matches ``PUBLIC_KEY_LENGTH``).
const PUB_KEY_LEN: usize = 32;
/// Ed25519 secret-key length in bytes (matches ``SECRET_KEY_LENGTH``).
///
/// Note: ``signing_key.to_bytes()`` returns the 32-byte seed, NOT the
/// 64-byte expanded form. This is the canonical encoding per RFC 8032.
const SECRET_KEY_LEN: usize = 32;

/// Verify an Ed25519 signature.
///
/// Returns ``Ok(true)`` if ``signature_bytes`` is a valid Ed25519
/// signature of ``message`` under ``public_key_bytes``. Returns
/// ``Ok(false)`` if verification fails (bad signature, wrong key). Returns
/// ``Err(message)`` only if the inputs are malformed (wrong length, bad
/// point encoding) — the ``#[pyfunction]`` wrapper collapses both
/// ``Ok(false)`` and ``Err(_)`` into ``false`` so Python callers never
/// see an exception.
pub fn verify_signature_inner(
    public_key_bytes: &[u8],
    message: &[u8],
    signature_bytes: &[u8],
) -> Result<bool, String> {
    // Length-check up front so ``from_slice`` / ``from_bytes`` never
    // panic. ``ed25519-dalek`` 2.x's ``VerifyingKey::from_bytes`` returns
    // ``SignatureError`` for malformed points but expects exactly 32
    // bytes; ``Signature::from_slice`` checks length internally.
    if public_key_bytes.len() != PUB_KEY_LEN {
        return Err(format!(
            "public_key must be {PUB_KEY_LEN} bytes, got {}",
            public_key_bytes.len()
        ));
    }

    // ``VerifyingKey::from_bytes`` takes a fixed-size ``&[u8; 32]``.
    // We checked the length above, so ``try_into`` is infallible here.
    let pk_array: [u8; PUB_KEY_LEN] = public_key_bytes
        .try_into()
        .map_err(|_| "internal: length check passed but try_into failed".to_string())?;
    let verifying_key =
        VerifyingKey::from_bytes(&pk_array).map_err(|e| format!("invalid public key: {e}"))?;

    // ``Signature::from_slice`` accepts any ``&[u8]`` and returns an
    // error if the length is not exactly 64. This is more ergonomic
    // than forcing the caller to provide a fixed-size array.
    let signature =
        Signature::from_slice(signature_bytes).map_err(|e| format!("invalid signature: {e}"))?;

    // ``Verifier::verify`` returns ``Result<(), SignatureError>``. We
    // collapse to a plain ``bool`` — the error details are not useful to
    // callers (a signature is either valid or not).
    Ok(verifying_key.verify(message, &signature).is_ok())
}

/// Generate a fresh Ed25519 keypair using the OS CSPRNG.
///
/// Returns ``(public_bytes, secret_bytes)`` where:
/// * ``public_bytes``: 32-byte Ed25519 verifying (public) key.
/// * ``secret_bytes``: 32-byte Ed25519 signing (secret) key seed.
///
/// The secret key is the 32-byte seed per RFC 8032, NOT the 64-byte
/// expanded form. Callers that need the expanded form should reconstruct
/// a ``SigningKey`` from the seed.
pub fn generate_keypair_inner() -> ([u8; SECRET_KEY_LEN], [u8; SECRET_KEY_LEN]) {
    use rand_core::OsRng;
    let mut csprng = OsRng;
    let signing_key = SigningKey::generate(&mut csprng);
    let verifying_key = signing_key.verifying_key();
    (verifying_key.to_bytes(), signing_key.to_bytes())
}

// ── Python-facing wrappers ─────────────────────────────────────────

/// Verify an Ed25519 signature.
///
/// :param public_key_bytes: 32-byte Ed25519 public key.
/// :param message:          Signed message bytes (arbitrary length).
/// :param signature_bytes:  64-byte Ed25519 signature.
/// :returns: ``True`` iff the signature is valid. ``False`` on any
///           failure (bad signature, wrong key, malformed inputs).
///
/// Note: the function is named ``py_verify_signature`` in Rust to avoid
/// a name collision with the hidden sub-module that ``#[pyfunction]``
/// generates. The Python-visible name is ``verify_signature``.
#[pyfunction(name = "verify_signature")]
pub fn py_verify_signature(
    public_key_bytes: &[u8],
    message: &[u8],
    signature_bytes: &[u8],
) -> bool {
    // Collapse both ``Ok(false)`` and ``Err(_)`` into ``false``. The
    // error string is discarded — Python callers only care about the
    // boolean result.
    verify_signature_inner(public_key_bytes, message, signature_bytes).unwrap_or(false)
}

/// Generate a fresh Ed25519 keypair.
///
/// :returns: ``(public_bytes, secret_bytes)`` — two ``bytes`` objects
///           (32 bytes each). Uses the OS CSPRNG (``OsRng``).
///
/// Note: the function is named ``py_generate_keypair`` in Rust to avoid
/// a name collision with the hidden sub-module that ``#[pyfunction]``
/// generates (which would shadow the function path in ``lib.rs``).
/// The Python-visible name is set to ``generate_keypair`` via the
/// ``name`` parameter.
#[pyfunction(name = "generate_keypair")]
pub fn py_generate_keypair(py: Python<'_>) -> PyResult<(Py<PyBytes>, Py<PyBytes>)> {
    let (public_bytes, secret_bytes) = generate_keypair_inner();
    // PyO3 converts ``Vec<u8>`` to a Python ``list[int]`` by default,
    // NOT to ``bytes``. For cryptographic byte output callers expect
    // ``bytes`` (immutable, C-contiguous), so we wrap explicitly in
    // ``PyBytes``. The ``py`` token binds the allocation to the GIL
    // scope of this call.
    let pub_obj = PyBytes::new_bound(py, &public_bytes).into();
    let sec_obj = PyBytes::new_bound(py, &secret_bytes).into();
    Ok((pub_obj, sec_obj))
}

#[cfg(test)]
mod tests {
    use super::*;
    use ed25519_dalek::{Signer, SigningKey};

    /// Helper: sign ``message`` with a fresh key, return (pub, msg, sig).
    fn make_valid_triple(message: &[u8]) -> (VerifyingKey, Vec<u8>, Signature) {
        use rand_core::OsRng;
        let signing_key = SigningKey::generate(&mut OsRng);
        let sig = signing_key.sign(message);
        (signing_key.verifying_key(), message.to_vec(), sig)
    }

    #[test]
    fn valid_signature_verifies() {
        let (vk, msg, sig) = make_valid_triple(b"hello world");
        let result = verify_signature_inner(&vk.to_bytes(), &msg, &sig.to_bytes());
        assert!(result.unwrap(), "valid signature must verify");
    }

    #[test]
    fn tampered_message_fails() {
        let (vk, msg, sig) = make_valid_triple(b"original message");
        let tampered = b"tampered message";
        assert_ne!(&msg[..], &tampered[..]);
        let result = verify_signature_inner(&vk.to_bytes(), tampered, &sig.to_bytes());
        assert!(!result.unwrap(), "tampered message must not verify");
    }

    #[test]
    fn wrong_public_key_fails() {
        let (_, msg, sig) = make_valid_triple(b"some message");
        // Generate a different keypair — verify against the wrong key.
        use rand_core::OsRng;
        let other = SigningKey::generate(&mut OsRng);
        let wrong_vk = other.verifying_key();
        let result =
            verify_signature_inner(&wrong_vk.to_bytes(), &msg, &sig.to_bytes());
        assert!(
            !result.unwrap(),
            "signature with wrong public key must not verify"
        );
    }

    #[test]
    fn malformed_public_key_length_errors() {
        let (_, msg, sig) = make_valid_triple(b"msg");
        let short_key = [0u8; 16]; // wrong length
        let result = verify_signature_inner(&short_key, &msg, &sig.to_bytes());
        assert!(result.is_err(), "short public key must error");
    }

    #[test]
    fn malformed_signature_length_errors() {
        let (vk, msg, _sig) = make_valid_triple(b"msg");
        let bad_sig = [0u8; 32]; // wrong length (should be 64)
        let result = verify_signature_inner(&vk.to_bytes(), &msg, &bad_sig);
        assert!(result.is_err(), "short signature must error");
    }

    #[test]
    fn empty_message_verifies() {
        let (vk, msg, sig) = make_valid_triple(b"");
        assert!(msg.is_empty());
        let result = verify_signature_inner(&vk.to_bytes(), &msg, &sig.to_bytes());
        assert!(result.unwrap(), "empty message signature must verify");
    }

    #[test]
    fn generated_keypair_is_consistent() {
        // generate_keypair_inner returns (pub, secret). Reconstructing
        // the SigningKey from the secret seed must yield the same
        // public key.
        let (public_bytes, secret_bytes) = generate_keypair_inner();
        assert_eq!(public_bytes.len(), PUB_KEY_LEN);
        assert_eq!(secret_bytes.len(), SECRET_KEY_LEN);
        let signing_key = SigningKey::from_bytes(&secret_bytes);
        let derived_pub = signing_key.verifying_key().to_bytes();
        assert_eq!(
            derived_pub, public_bytes,
            "secret seed must derive the same public key"
        );
    }

    #[test]
    fn generated_keypair_can_sign_and_verify() {
        let (public_bytes, secret_bytes) = generate_keypair_inner();
        let signing_key = SigningKey::from_bytes(&secret_bytes);
        let message = b"round-trip test";
        let signature = signing_key.sign(message);
        let result =
            verify_signature_inner(&public_bytes, message, &signature.to_bytes());
        assert!(result.unwrap(), "generated keypair must sign + verify");
    }
}
