# Calibration Report — v1.34.0

> **Phase:** 7.6 (Synthetic Benchmark + Golden Dataset v2)
> **Dataset:** ``data\calibration\golden_routing_dataset_v2.csv``
> **Generated:** auto-generated from synthetic golden dataset
> **Grid search model:** composite = accuracy − 0.5 × normalized_cost

---

## 1. Overview

This report presents the calibration results for the heuristic Tier Router
(``harness.routing.tier_selector``). A full grid search was performed over
**7 threshold parameters** against the synthetic golden routing dataset (v2,
2,000 events with realistic prompt and context token distributions), followed
by holdout validation, robustness analysis, and migration impact assessment.

---

## 2. Data Summary

| Metric | Value |
|--------|-------|
| Total routing events | 2000 |
| Training set | 1600 (80%) |
| Holdout set | 400 (20%) |
| Grid combinations | 6×5×5×5×5×5×2 = 37,500 |
| Holdout seed | 42 |
| Source | Synthetic benchmark (Phase 7.6) |

**✅ Synthetic dataset:** All ``prompt_tokens`` and ``context_tokens`` are
nonzero with realistic multi-turn distributions (T1: 25–375 prompt tokens,
T2: 125–750, T3: 500–2500). Context tokens simulate cumulative multi-turn
usage via ``context_tokens >= prompt_tokens``.

---

## 3. Recommended Thresholds

| Parameter | Current Default | Recommended | Reason |
|-----------|----------------|-------------|--------|
| ``confidence_high`` | 0.85 | **0.60** | Grid optimum |
| ``confidence_low`` | 0.55 | **0.30** | Grid optimum |
| ``t1_max_prompt_chars`` | 500 | **1000** | Grid optimum |
| ``t1_max_context_tokens`` | 4000 | **2000** | Grid optimum |
| ``t3_min_prompt_chars`` | 5000 | **10000** | Grid optimum |
| ``t3_min_context_tokens`` | 32000 | **16000** | Grid optimum |
| ``complexity_keywords`` | ``reasoning``, ``analyze``, ``prove``, ``derive``, ``evaluate`` | ``reasoning``, ``analyze``, ``prove``, ``derive``, ``evaluate`` | Grid optimum |

---

## 4. Holdout Validation

Top-5 grid configurations re-evaluated on the holdout set:

| Rank | Accuracy | Cost (USD) | T1 Fraction | Fallback | Score |
|------|----------|------------|-------------|----------|-------|
| 1 | 0.7081 | $2.585000 | 0.3375 | 0.3000 | -63.9169 |
| 2 | 0.7081 | $2.585000 | 0.3375 | 0.3000 | -63.9169 |
| 3 | 0.7081 | $2.585000 | 0.3375 | 0.3000 | -63.9169 |
| 4 | 0.7081 | $2.585000 | 0.3375 | 0.3000 | -63.9169 |
| 5 | 0.7081 | $2.585000 | 0.3375 | 0.3000 | -63.9169 |


---

## 5. Robustness Check

Each numeric threshold perturbed by ±10% (complexity keywords: base vs
extended list). Variance measures sensitivity — higher values indicate
the parameter strongly affects accuracy.

| Parameter | Accuracy Variance | Sensitive |
|-----------|------------------|-----------|
| ``t1_max_prompt_chars`` | 0.000421 | ✅ |
| ``t3_min_prompt_chars`` | 0.000010 | ✅ |
| ``confidence_high`` | 0.000000 | ✅ |
| ``confidence_low`` | 0.000000 | ✅ |
| ``t1_max_context_tokens`` | 0.000000 | ✅ |
| ``t3_min_context_tokens`` | 0.000000 | ✅ |
| ``complexity_keywords`` | 0.000000 | ✅ |


---

## 6. Migration Impact

| Metric | Current | Recommended | Delta |
|--------|---------|-------------|-------|
| Accuracy | 0.6138 | 0.7115 | +0.0976 |
| Total Cost (USD) | $15.396000 | $12.754000 | $-2.642000 |

---

## 7. Limitation Notes

1. **Synthetic data:** The golden dataset v2 is synthetically generated with
   heuristic confidence scores and tier assignments. Real-world distributions
   of prompt lengths, context tokens, and complexity keywords may differ.

2. **Prompt text absent:** ``prompt_len_chars`` is derived from
   ``prompt_tokens × 4 ± 20%``; ``has_complexity_keyword`` is assigned
   per-tier (80% for T3). Real prompt text keyword scanning is not performed.

3. **Confidence unused:** The ``confidence_high`` and ``confidence_low``
   thresholds are part of the grid but are not exercised by the current
   heuristic router logic (which uses only prompt/context size and
   complexity keywords).

4. **Cost model is flat:** The grid search uses a flat per-call cost model
   (T1=$0.001, T2=$0.005, T3=$0.020). Token-based pricing is not simulated
   in the grid search, only in the synthetic data generator.

5. **Re-evaluation recommended:** Re-run calibration after replacing the
   synthetic dataset with real production logs that include per-call
   context token tracking and prompt text keyword scanning.
