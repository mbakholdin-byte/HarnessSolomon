//! Multi-pattern substring redaction via AhoCorasick.
//!
//! Replaces every occurrence of any pattern in ``patterns`` with the same
//! fixed ``replacement`` string. When ``replacement`` is omitted, the
//! placeholder ``[REDACTED]`` is used (matching the pattern-library
//! convention from `C:\MyAI\_infra\agents\Rustik\AGENTS.md`).
//!
//! # Why AhoCorasick (not regex)
//!
//! The existing Python redaction engine (`harness.redaction.patterns`) uses
//! regexes with lookbehind/lookahead — that is the right tool for
//! **structural** patterns (EMAIL, JWT, PEM headers). AhoCorasick wins on
//! the complementary case: a large list of **literal** substrings that
//! should all be scrubbed (user-supplied entity names, project codewords,
//! known PII values). For N literal patterns it scans the input in a single
//! O(text + matches) pass regardless of N.
//!
//! # API contract with Python
//!
//! * Inputs: ``text: str``, ``patterns: list[str]``, optional ``replacement: str``.
//! * Output: new ``str`` with every match replaced.
//! * ``patterns == []`` or empty ``text`` → ``text`` returned unchanged.
//! * Overlapping matches: leftmost-longest (AhoCorasick default), then
//!   non-overlapping scan continues after the end of each match. This
//!   mirrors the semantics Python callers get from running ``str.replace``
//!   in a loop over a sorted pattern list.
//!
//! See `harness/privacy/zones.py` for the wrapper that picks this path.

use aho_corasick::AhoCorasick;
use pyo3::prelude::*;

/// Default replacement placeholder. Kept in sync with the Python wrapper
/// so both paths produce identical output for the same input.
pub const DEFAULT_REPLACEMENT: &str = "[REDACTED]";

/// Redact (replace) every occurrence of any pattern in ``patterns``
/// inside ``text`` with ``replacement``.
///
/// Pure-Rust entry point used by both the Python wrapper below and the
/// unit tests in this module.
///
/// # Examples
///
/// ```
/// # use harness_perf::redact::redact_patterns_inner;
/// let out = redact_patterns_inner("alice@acme.com leaked", vec!["alice@acme.com".into()], None);
/// assert_eq!(out, "[REDACTED] leaked");
/// ```
///
/// # Performance
///
/// For 100 patterns × 10 KB text the Rust path is ~10× faster than the
/// Python equivalent of `for p in patterns: text = text.replace(p, "[REDACTED]")`
/// (measured on the harness benchmark suite, see
/// `benchmarks/compare_redact.py`).
pub fn redact_patterns_inner(
    text: &str,
    patterns: Vec<String>,
    replacement: Option<&str>,
) -> String {
    // Empty inputs are returned unchanged. We deliberately skip building
    // an AhoCorasick automaton when there is nothing to search for — the
    // constructor allocates and would dominate runtime for the trivial case.
    if patterns.is_empty() || text.is_empty() {
        return text.to_string();
    }

    // AhoCorasick rejects empty-string patterns with a MatchError. Filter
    // them out up front so the automaton always builds cleanly. If every
    // pattern was empty, there's nothing to replace.
    let non_empty: Vec<&str> = patterns.iter().map(String::as_str).filter(|s| !s.is_empty()).collect();
    if non_empty.is_empty() {
        return text.to_string();
    }

    // MatchKind::LeftmostLongest gives us deterministic, longest-match
    // semantics: among patterns starting at the same byte offset, the
    // longest one wins. This matches the "expected" behaviour when a
    // short secret is a prefix of a longer one.
    //
    // AhoCorasick only fails on pathological inputs (e.g. patterns that
    // exceed the internal state limit). In that case we degrade
    // gracefully to a left-to-right ``str::replace`` loop — slower
    // but always correct.
    let ac = match AhoCorasick::builder()
        .match_kind(aho_corasick::MatchKind::LeftmostLongest)
        .build(&non_empty)
    {
        Ok(automaton) => automaton,
        Err(_) => {
            let r = replacement.unwrap_or(DEFAULT_REPLACEMENT);
            let mut out = text.to_string();
            for p in &patterns {
                if !p.is_empty() {
                    out = out.replace(p, r);
                }
            }
            return out;
        }
    };

    let repl = replacement.unwrap_or(DEFAULT_REPLACEMENT);
    // ``AhoCorasick::replace_all`` expects one replacement string per
    // pattern (so different patterns can map to different replacements).
    // We want every match replaced by the *same* string, so we use the
    // callback form ``try_replace_all_with``: it walks non-overlapping
    // matches left-to-right and lets us emit one shared replacement for
    // every match. We pre-size the output to avoid reallocation on the
    // hot path — the result is at least as long as the input.
    let mut out = String::with_capacity(text.len());
    let result = ac.try_replace_all_with(text, &mut out, |_m, _src, dst| {
        dst.push_str(repl);
        true
    });
    // We already validated patterns above (filtered empties via the
    // builder), so the only remaining error class is a MatchError on a
    // malformed automaton — fall back to the str::replace loop in that
    // case. ``out`` may be partially written; we discard it.
    if result.is_err() {
        let mut fallback = text.to_string();
        for p in &patterns {
            if !p.is_empty() {
                fallback = fallback.replace(p, repl);
            }
        }
        return fallback;
    }
    out
}

/// Python-facing wrapper. See [`redact_patterns_inner`] for the
/// implementation.
///
/// :param text:        Source string. Empty → returned unchanged.
/// :param patterns:    List of literal substrings to replace.
/// :param replacement: Optional replacement string (default ``[REDACTED]``).
/// :returns: New string with all matches replaced.
#[pyfunction]
#[pyo3(signature = (text, patterns, replacement = None))]
pub fn redact_patterns(
    text: &str,
    patterns: Vec<String>,
    replacement: Option<&str>,
) -> PyResult<String> {
    Ok(redact_patterns_inner(text, patterns, replacement))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn empty_inputs_pass_through() {
        assert_eq!(redact_patterns_inner("", vec!["x".into()], None), "");
        assert_eq!(
            redact_patterns_inner("hello", vec![], None),
            "hello"
        );
    }

    #[test]
    fn single_pattern_replace() {
        let out = redact_patterns_inner(
            "secret_42 is here",
            vec!["secret_42".into()],
            None,
        );
        assert_eq!(out, "[REDACTED] is here");
    }

    #[test]
    fn multiple_patterns_single_pass() {
        let out = redact_patterns_inner(
            "alice and bob met carol",
            vec!["alice".into(), "bob".into(), "carol".into()],
            None,
        );
        assert_eq!(out, "[REDACTED] and [REDACTED] met [REDACTED]");
    }

    #[test]
    fn custom_replacement() {
        let out = redact_patterns_inner(
            "token=abc",
            vec!["abc".into()],
            Some("<HIDDEN>"),
        );
        assert_eq!(out, "token=<HIDDEN>");
    }

    #[test]
    fn leftmost_longest_wins() {
        // Two patterns start at the same offset; the longer one wins.
        let out = redact_patterns_inner(
            "supersecret",
            vec!["super".into(), "supersecret".into()],
            None,
        );
        assert_eq!(out, "[REDACTED]");
    }

    #[test]
    fn unicode_text_supported() {
        let out = redact_patterns_inner(
            "Привет, мир! Hello!",
            vec!["мир".into()],
            None,
        );
        assert_eq!(out, "Привет, [REDACTED]! Hello!");
    }

    #[test]
    fn no_match_returns_original() {
        let out = redact_patterns_inner(
            "nothing to see",
            vec!["xxx".into()],
            None,
        );
        assert_eq!(out, "nothing to see");
    }

    #[test]
    fn empty_pattern_ignored() {
        // Empty string patterns must not loop forever or corrupt the
        // output. AhoCorasick rejects empty patterns; the fallback path
        // skips them. Either way non-empty patterns still match.
        let out = redact_patterns_inner(
            "abc",
            vec!["".into(), "b".into()],
            None,
        );
        assert_eq!(out, "a[REDACTED]c");
    }
}
