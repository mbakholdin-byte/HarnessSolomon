//! Harness perf — Rust fast paths for Python harness (PyO3).
//!
//! Four hot paths exposed to Python:
//!   * [`redact::redact_patterns`] — AhoCorasick multi-pattern replace.
//!   * [`bm25::bm25_search`] — in-memory BM25 ranking (k1=1.5, b=0.75).
//!   * [`ed25519_verify::verify_signature`] — Ed25519 signature verification.
//!   * [`ed25519_verify::generate_keypair`] — Ed25519 keypair generation.
//!
//! Trust boundary: this crate does NOT depend on any `harness.*` code.
//! All inputs come through plain Python primitives (`&str`, `Vec<String>`).
//!
//! See `harness/privacy/zones.py` and `harness/memory/retrieval/bm25_fast.py`
//! for the Python wrappers that prefer this module and fall back to pure
//! Python when the Rust wheel is not installed.
#![forbid(unsafe_code)]
#![deny(clippy::all)]
// We do NOT enable ``clippy::pedantic`` globally: several pedantic lints
// (doc_markdown, cast_precision_loss) clash with our intentional choices
// (technical terms without backticks; ``usize -> f32`` casts in BM25 math
// where the corpus fits comfortably in the f32 mantissa). The standard
// ``-D clippy::all`` threshold is what "clippy clean" means for this crate.
#![allow(
    clippy::module_name_repetitions,
    clippy::needless_pass_by_value,
    clippy::must_use_candidate,
    clippy::missing_errors_doc,
    clippy::doc_markdown,
    clippy::cast_precision_loss,
    // PyO3 0.22 ``#[pyfunction]`` macro generates a wrapper that wraps the
    // inner ``PyResult`` in an unnecessary ``.into()`` on the ``Ok`` arm
    // — clippy flags it as ``useless_conversion`` even though the source
    // code is idiomatic. Allow at crate level to keep the public API
    // returning ``PyResult<T>`` (the convention PyO3 expects).
    clippy::useless_conversion
)]

pub mod bm25;
pub mod ed25519_verify;
pub mod redact;

use pyo3::prelude::*;

/// Python-facing module: ``import harness_perf``.
///
/// Exposes four functions. See the per-module docstrings for semantics.
#[pymodule]
fn harness_perf(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(redact::redact_patterns, m)?)?;
    m.add_function(wrap_pyfunction!(bm25::bm25_search, m)?)?;
    m.add_function(wrap_pyfunction!(ed25519_verify::py_verify_signature, m)?)?;
    m.add_function(wrap_pyfunction!(ed25519_verify::py_generate_keypair, m)?)?;
    m.add("__doc__", "Rust fast paths for Harness (PyO3).")?;
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    Ok(())
}
