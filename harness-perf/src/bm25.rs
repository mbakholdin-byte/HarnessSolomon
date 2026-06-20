//! In-memory BM25 ranking (sparse lexical retrieval).
//!
//! Pure-Rust reimplementation of the score loop in
//! `harness/memory/retrieval/bm25.py::_bm25_score`. Same hyper-parameters
//! (k1=1.5, b=0.75), same tokeniser (`\w+` lowercased), same IDF formula
//! (BM25+ variant — never negative). Designed so that for any (query,
//! corpus) pair the Rust ranking agrees with the Python ranking to within
//! ±1 % per score and identical top-k ordering on ties (we break ties on
//! ascending doc index, same as Python).
//!
//! # API contract with Python
//!
//! * Inputs: ``query: str``, ``documents: list[str]``, ``k: int``.
//! * Output: ``list[tuple[int, float]]`` — (doc_index, score), sorted by
//!   score desc then index asc, truncated to ``k``.
//! * Edge cases mirroring Python ``BM25Retriever.retrieve``:
//!     - ``k <= 0``                → empty list.
//!     - ``documents == []``       → empty list.
//!     - query tokenises to ``[]`` → empty list.
//!     - docs that produce score ``<= 0`` are dropped (Python keeps only
//!       ``score > 0``).
//!
//! Rust does NOT know about ``harness.memory.schema.Memory`` — the caller
//! maps ``(doc_index, score)`` back to its own objects. This keeps the
//! trust boundary clean (no ``harness.*`` imports from Rust).
//!
//! See `harness/memory/retrieval/bm25_fast.py` for the Python wrapper.

use std::collections::HashMap;

use pyo3::prelude::*;

/// BM25 term-frequency saturation parameter. Matches Python ``_K1``.
const K1: f32 = 1.5;
/// BM25 length-normalisation parameter. Matches Python ``_B``.
const B: f32 = 0.75;

/// A pre-tokenised corpus plus the statistics needed to score a query.
///
/// Building the index is O(total_tokens); scoring a query is
/// O(q_tokens × avg_postings_per_term). The Python ``BM25Retriever``
/// builds this on every ``retrieve()`` call — the Rust version amortises
/// the cost across multiple queries when callers reuse the struct (see
/// [`Bm25Index::search`]).
pub struct Bm25Index {
    /// Per-document term frequencies: ``doc_term_freq[doc_idx][term] = count``.
    doc_term_freq: Vec<HashMap<String, u32>>,
    /// Document frequency: number of docs containing each term.
    doc_freq: HashMap<String, u32>,
    /// Per-document token count (used for length normalisation).
    doc_len: Vec<usize>,
    /// Mean document length across the corpus.
    avgdl: f32,
    /// Number of documents.
    n_docs: usize,
}

impl Bm25Index {
    /// Build an index over a pre-tokenised corpus.
    ///
    /// ``tokenised_docs`` may be empty; the resulting index will answer
    /// every query with an empty vec (matching Python behaviour).
    pub fn new(tokenised_docs: Vec<Vec<String>>) -> Self {
        let n_docs = tokenised_docs.len();
        let mut doc_term_freq: Vec<HashMap<String, u32>> =
            Vec::with_capacity(n_docs);
        let mut doc_freq: HashMap<String, u32> = HashMap::new();
        let mut doc_len: Vec<usize> = Vec::with_capacity(n_docs);
        let mut total_len: usize = 0;

        for tokens in tokenised_docs {
            let len = tokens.len();
            doc_len.push(len);
            total_len = total_len.saturating_add(len);

            let mut tf: HashMap<String, u32> = HashMap::new();
            for term in &tokens {
                *tf.entry(term.clone()).or_insert(0) += 1;
            }
            // DF counts each term once per doc.
            for term in tf.keys() {
                *doc_freq.entry(term.clone()).or_insert(0) += 1;
            }
            doc_term_freq.push(tf);
        }

        let avgdl = if n_docs == 0 {
            0.0
        } else {
            (total_len as f32) / (n_docs as f32)
        };

        Self {
            doc_term_freq,
            doc_freq,
            doc_len,
            avgdl,
            n_docs,
        }
    }

    /// Score one (query, doc) pair. Mirrors
    /// `BM25Retriever._bm25_score` term-by-term.
    fn score(&self, q_tokens: &[String], doc_idx: usize) -> f32 {
        let Some(tf) = self.doc_term_freq.get(doc_idx) else {
            return 0.0;
        };
        if self.doc_len[doc_idx] == 0 {
            return 0.0;
        }
        let dl = self.doc_len[doc_idx] as f32;
        let n = self.n_docs as f32;
        let mut score: f32 = 0.0;

        for term in q_tokens {
            let Some(&term_freq) = tf.get(term) else {
                continue;
            };
            let df = self
                .doc_freq
                .get(term)
                .copied()
                .unwrap_or(0) as f32;
            // IDF (BM25+ variant — never negative). Exact match of the
            // Python formula: log(((N - df + 0.5) / (df + 0.5)) + 1.0).
            let idf = (((n - df + 0.5) / (df + 0.5)) + 1.0).ln();
            // Term-frequency saturation: (tf * (k1 + 1)) /
            //   (tf + k1 * (1 - b + b * dl / avgdl)).
            let denom = (term_freq as f32) + K1 * (1.0 - B + B * dl / self.avgdl);
            // denom is strictly positive when tf > 0 and K1, B in [0, 1]:
            // 1 - B + B * dl / avgdl >= 0 (B <= 1, dl / avgdl >= 0), so
            // K1 * (…) >= 0 and we add a positive tf → denom > 0.
            let tf_norm = (term_freq as f32 * (K1 + 1.0)) / denom;
            score += idf * tf_norm;
        }
        score
    }

    /// Rank documents by BM25 score for ``q_tokens`` and return up to
    /// ``k`` ``(doc_idx, score)`` tuples.
    ///
    /// Sort order: score desc, then doc_idx asc (matches Python's
    /// ``scores.sort(key=lambda x: (-x[1], x[0]))``). Documents with
    /// score ``<= 0`` are dropped.
    pub fn search(&self, q_tokens: &[String], k: usize) -> Vec<(usize, f32)> {
        if k == 0 || self.n_docs == 0 || q_tokens.is_empty() {
            return Vec::new();
        }
        let mut scored: Vec<(usize, f32)> = (0..self.n_docs)
            .map(|i| (i, self.score(q_tokens, i)))
            .filter(|&(_, s)| s > 0.0)
            .collect();
        // Rust's sort_by is stable; to replicate Python's
        // ``(-score, idx)`` ordering on a stable sort we compare by
        // (score desc, idx asc) explicitly. Using ``sort_by`` (not
        // ``sort_unstable_by``) preserves the doc_idx-ascending tie
        // order even though we already key on idx.
        scored.sort_by(|a, b| {
            b.1.partial_cmp(&a.1)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then(a.0.cmp(&b.0))
        });
        scored.truncate(k);
        scored
    }
}

/// Tokenise a string into lowercase word tokens.
///
/// Mirrors Python ``_tokenise``: split on ``\w+`` (Unicode-aware), then
/// lowercase each token. CJK / Cyrillic characters survive as one token
/// each (a single CJK code point matches ``\w`` and forms a token of
/// length 1).
fn tokenise(text: &str) -> Vec<String> {
    text.split(|c: char| !c.is_alphanumeric() && c != '_')
        .filter(|s| !s.is_empty())
        .map(str::to_lowercase)
        .collect()
}

/// Pure-Rust entry point used by both the Python wrapper and unit tests.
///
/// Builds a one-shot index over ``documents``, tokenises ``query``, and
/// returns the top-``k`` ``(doc_index, score)`` pairs. Callers that issue
/// many queries against the same corpus should construct a [`Bm25Index`]
/// once and call [`Bm25Index::search`] repeatedly instead.
pub fn bm25_search_inner(
    query: &str,
    documents: Vec<String>,
    k: usize,
) -> Vec<(usize, f32)> {
    if k == 0 || documents.is_empty() {
        return Vec::new();
    }
    let q_tokens = tokenise(query);
    if q_tokens.is_empty() {
        return Vec::new();
    }
    let tokenised_docs: Vec<Vec<String>> =
        documents.iter().map(|d| tokenise(d)).collect();
    let index = Bm25Index::new(tokenised_docs);
    index.search(&q_tokens, k)
}

/// Python-facing BM25 search.
///
/// :param query:     Natural-language query (whitespace + punctuation
///                    tokenised on ``\w+`` and lowercased).
/// :param documents: Corpus of document strings.
/// :param k:         Maximum number of results to return.
/// :returns: List of ``(doc_index, score)`` tuples, score desc then
///           doc_index asc, truncated to ``k``. Empty when ``k <= 0``,
///           ``documents`` is empty, or the query has no word tokens.
#[pyfunction]
#[pyo3(signature = (query, documents, k))]
pub fn bm25_search(
    query: &str,
    documents: Vec<String>,
    k: usize,
) -> PyResult<Vec<(usize, f32)>> {
    Ok(bm25_search_inner(query, documents, k))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn approx_eq(a: f32, b: f32) -> bool {
        (a - b).abs() < 1e-5
    }

    #[test]
    fn empty_inputs_return_empty() {
        assert!(bm25_search_inner("x", vec![], 5).is_empty());
        assert!(bm25_search_inner("x", vec!["a".into()], 0).is_empty());
        assert!(bm25_search_inner(
            "   ",
            vec!["a".into()],
            5
        )
        .is_empty());
    }

    #[test]
    fn single_match_returns_one_hit() {
        let out = bm25_search_inner(
            "rust",
            vec!["rust is fast".into(), "python is dynamic".into()],
            5,
        );
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].0, 0);
        assert!(out[0].1 > 0.0);
    }

    #[test]
    fn ranking_agrees_with_intuition() {
        // "rust" appears in doc 0 once and doc 2 twice. With equal
        // lengths and IDF, doc 2 (higher tf) should rank first.
        let docs = vec![
            "rust language".to_string(),
            "python language".to_string(),
            "rust rust rust".to_string(),
        ];
        let out = bm25_search_inner("rust", docs, 3);
        assert_eq!(out.len(), 2);
        assert_eq!(out[0].0, 2, "tf-heavy doc should rank first");
        assert!(out[0].1 > out[1].1);
    }

    #[test]
    fn tie_break_by_doc_index_asc() {
        // Two identical docs → equal score. Lower index must come first.
        let docs = vec![
            "alpha alpha".to_string(),
            "alpha alpha".to_string(),
        ];
        let out = bm25_search_inner("alpha", docs, 2);
        assert_eq!(out.len(), 2);
        assert_eq!(out[0].0, 0);
        assert_eq!(out[1].0, 1);
        assert!(approx_eq(out[0].1, out[1].1));
    }

    #[test]
    fn truncates_to_k() {
        let docs: Vec<String> = (0..10)
            .map(|i| format!("doc {i} rust"))
            .collect();
        let out = bm25_search_inner("rust", docs, 3);
        assert_eq!(out.len(), 3);
    }

    #[test]
    fn unicode_query_supported() {
        let out = bm25_search_inner(
            "мир",
            vec!["привет мир".into(), "hello world".into()],
            5,
        );
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].0, 0);
    }

    #[test]
    fn no_overlap_returns_empty() {
        let out = bm25_search_inner(
            "kotlin",
            vec!["rust rules".into()],
            5,
        );
        assert!(out.is_empty());
    }

    #[test]
    fn hand_computed_score_matches_formula() {
        // Single doc, single-term query. Compute BM25 by hand.
        //   N=1, df=1, dl=1, avgdl=1, tf=1, k1=1.5, b=0.75
        //   idf = log(((1-1+0.5)/(1+0.5)) + 1) = log(1/3 + 1) = log(4/3)
        //   tf_norm = (1*(1.5+1)) / (1 + 1.5*(1-0.75+0.75*1/1))
        //           = 2.5 / (1 + 1.5*1) = 2.5 / 2.5 = 1.0
        //   score = log(4/3) * 1.0
        let out = bm25_search_inner(
            "rust",
            vec!["rust".into()],
            1,
        );
        assert_eq!(out.len(), 1);
        let expected = ((4.0_f32 / 3.0_f32).ln()) * 1.0;
        assert!(
            approx_eq(out[0].1, expected),
            "got {} expected {}",
            out[0].1,
            expected
        );
    }
}
