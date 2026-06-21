# Calibration Report — v1.33.0

> **Phase:** 7.5 (Tier Router Calibration)
> **Generated:** auto-generated from golden dataset
> **Grid search model:** composite = accuracy − 0.5 × normalized_cost

---

## 1. Overview

This report presents the calibration results for the heuristic Tier Router
(``harness.routing.tier_selector``). A full grid search was performed over
**7 threshold parameters** against the golden routing dataset, followed by
holdout validation, robustness analysis, and migration impact assessment.

---

## 2. Data Summary

| Metric | Value |
|--------|-------|
| Total routing events | 737 |
| Training set | 589 (80%) |
| Holdout set | 148 (20%) |
| Grid combinations | 6×5×5×5×5×5×2 = 37,500 |
| Holdout seed | 42 |

**⚠️ Limitation (context_tokens):** All ``context_tokens`` values in the
current golden dataset are **0** — the log format does not capture per-call
context token counts. Thresholds for ``t1_max_context_tokens`` and
``t3_min_context_tokens`` are set to reasonable defaults but
**have not been validated against real context-token data**. Recalibration
is recommended once context token tracking is added to the logging pipeline.

---

## 3. Recommended Thresholds

| Parameter | Current Default | Recommended | Reason |
|-----------|----------------|-------------|--------|
| ``confidence_high`` | 0.85 | **0.60** | Grid optimum |
| ``confidence_low`` | 0.55 | **0.30** | Grid optimum |
| ``t1_max_prompt_chars`` | 500 | **1000** | Grid optimum |
| ``t1_max_context_tokens`` | 4000 | **8000** | Grid optimum |
| ``t3_min_prompt_chars`` | 5000 | **3000** | Grid optimum |
| ``t3_min_context_tokens`` | 32000 | **16000** | Grid optimum |
| ``complexity_keywords`` | ``reasoning``, ``analyze``, ``prove``, ``derive``, ``evaluate`` | ``reasoning``, ``analyze``, ``prove``, ``derive``, ``evaluate`` | Grid optimum |

---

## 4. Holdout Validation

Top-5 grid configurations re-evaluated on the holdout set:

| Rank | Accuracy | Cost (USD) | T1 Fraction | Fallback | Score |
|------|----------|------------|-------------|----------|-------|
| 1 | 1.0000 | $1.193000 | 0.6284 | 0.0000 | -28.8250 |
| 2 | 1.0000 | $1.193000 | 0.6284 | 0.0000 | -28.8250 |
| 3 | 1.0000 | $1.193000 | 0.6284 | 0.0000 | -28.8250 |
| 4 | 1.0000 | $1.193000 | 0.6284 | 0.0000 | -28.8250 |
| 5 | 1.0000 | $1.193000 | 0.6284 | 0.0000 | -28.8250 |


---

## 5. Robustness Check

Each numeric threshold perturbed by ±10% (complexity keywords: base vs
extended list). Variance measures sensitivity — higher values indicate
the parameter strongly affects accuracy.

| Parameter | Accuracy Variance | Sensitive |
|-----------|------------------|-----------|
| ``confidence_high`` | 0.000000 | ✅ |
| ``confidence_low`` | 0.000000 | ✅ |
| ``t1_max_prompt_chars`` | 0.000000 | ✅ |
| ``t1_max_context_tokens`` | 0.000000 | ✅ |
| ``t3_min_prompt_chars`` | 0.000000 | ✅ |
| ``t3_min_context_tokens`` | 0.000000 | ✅ |
| ``complexity_keywords`` | 0.000000 | ✅ |


---

## 6. Migration Impact

| Metric | Current | Recommended | Delta |
|--------|---------|-------------|-------|
| Accuracy | 1.0000 | 1.0000 | +0.0000 |
| Total Cost (USD) | $6.133000 | $6.133000 | +$0.000000 |

---

## 7. Limitation Notes

1. **context_tokens = 0:** The golden dataset does not contain real context
   token values. Thresholds ``t1_max_context_tokens`` and
   ``t3_min_context_tokens`` are selected by grid search but have not been
   validated against context-rich scenarios.

2. **Prompt text absent:** ``prompt_len_chars`` is estimated as
   ``prompt_tokens × 4``; ``has_complexity_keyword`` is inferred from
   ``model_id``/``model`` fields only (no prompt text in logs).

3. **T1 bias:** Due to the absence of prompt/context data, most events fall
   into T1 (Rule 3). The grid search selects the **widest T1 zone** when
   all configurations achieve ≈100% accuracy. Real-world performance with
   actual prompt data may differ.

4. **Confidence unused:** The ``confidence_high`` and ``confidence_low``
   thresholds are part of the grid but are not exercised by the current
   heuristic router logic (which uses only prompt/context size and
   complexity keywords).

5. **Re-evaluation recommended:** Re-run calibration after adding
   per-call context token tracking and prompt text keyword scanning to
   the logging pipeline (Phase 7.6).
