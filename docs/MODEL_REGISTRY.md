# Model Registry — каталог моделей для Solomon Harness v1.0

**Версия:** 1.0
**Дата:** 2026-06-13
**Принцип:** гибрид (локальные + облачные) + cost-aware router
**Бенчмарки:** SWE-bench Verified + HumanEval + LiveCodeBench + τ²-Bench (tool-use)

---

## 1. Категории моделей

Все модели делятся на 3 tier по сложности задач:

| Tier | Размер | Контекст | Назначение | Хостинг |
|------|--------|----------|-----------|---------|
| **T1 — Haiku-class** | 8–12B | 32–128K | Простые задачи, tool-use, сортировка, grep, edit | Локально (Ollama) |
| **T2 — Sonnet-class** | 30–70B | 128K | Coding, refactor, анализ, planning | Локально (Ollama, vLLM) |
| **T3 — Opus-class** | 200B+ / API | 128K–1M+ | Сложный coding, multi-file, архитектура, **long-context** | Облако (API) |

---

## 2. Поддерживаемые модели (v1.0)

### 2.1. T1 — Haiku-class (локально, ≤12B)

| Модель | Размер | Контекст | Tool-use | Coding | Vibe | Источник | Лицензия |
|--------|--------|----------|----------|--------|------|----------|----------|
| **Gemma 4 12B IT Assistant** | 12B | 128K | ✅ (native) | ✅ mid | ⚠️ | [HF](https://huggingface.co/google/gemma-4-12B-it-assistant) | Gemma (commercial OK) |
| **Qwen3 8B** | 8B | 128K | ✅ | ✅ | ⚠️ | [HF](https://huggingface.co/Qwen/Qwen3-8B) | Apache 2.0 |
| **Qwen3 4B** (fallback) | 4B | 32K | ✅ | ⚠️ | ❌ | [HF](https://huggingface.co/Qwen/Qwen3-4B) | Apache 2.0 |
| **Phi-4** (14B) | 14B | 16K | ✅ | ✅ | ⚠️ | [HF](https://huggingface.co/microsoft/phi-4) | MIT |
| **MiniMax M3** (текущая Соломона) | ~12B | **1M** | ✅ (мы проверили) | ✅ | ✅ | [HF](https://huggingface.co/MiniMaxAI/MiniMax-M3) | proprietary |
| MiniMax M2.7 (legacy) | ~12B | 128K | ✅ | ✅ | ✅ | [HF](https://huggingface.co/MiniMaxAI/MiniMax-M2.7) | proprietary |

**По умолчанию в T1:** **Qwen3 8B** (Ollama, уже в стеке Соломона) — лучший баланс размер/качество/tool-use. Gemma 4 12B — альтернатива если нужна multimodal (vision).

### 2.2. T2 — Sonnet-class (локально, 30–70B)

| Модель | Размер | Контекст | Coding | Tool-use | Источник | Лицензия |
|--------|--------|----------|--------|----------|----------|----------|
| **Qwen3-Coder 30B A3B** | 30B (3B active) | 128K | ⭐⭐⭐ | ⭐⭐⭐ | [HF](https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct) | Apache 2.0 |
| **Qwen3 32B** | 32B | 128K | ⭐⭐⭐ | ⭐⭐⭐ | [HF](https://huggingface.co/Qwen/Qwen3-32B) | Apache 2.0 |
| **Qwen3 235B A22B** | 235B (22B active) | 128K | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | [HF](https://huggingface.co/Qwen/Qwen3-235B-A22B) | Apache 2.0 |
| **GLM-4.5** | 355B (32B active) | 128K | ⭐⭐⭐ | ⭐⭐⭐⭐ | [HF](https://huggingface.co/zai-org/GLM-4.5) | MIT |
| **MiniMax M2.5** | 230B (10B active) | 128K | ⭐⭐⭐ | ⭐⭐⭐ | [HF](https://huggingface.co/MiniMaxAI/MiniMax-M2.5) | proprietary |

**По умолчанию в T2:** **Qwen3-Coder 30B A3B** (FP8 — помещается в 24GB VRAM) — лучший coding в 30B-классе. Для задач общего типа — **Qwen3 32B** (full precision, ~64GB RAM без GPU).

### 2.3. T3 — Opus-class (облако, 200B+ или frontier)

| Провайдер | Модель | Контекст | Цена $/1M in/out | Coding | Tool-use | Vibe |
|-----------|--------|----------|------------------|--------|----------|------|
| **ZhipuAI (Z.ai)** | **GLM-4.7** (cloud) | 128K | 0.6/2.2 | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ |
| **ZhipuAI** | GLM-4.6 (cloud) | 128K | 0.6/2.2 | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐ |
| **Moonshot** | **Kimi K2.6** | 128K | 0.6/2.5 | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐ |
| **Moonshot** | Kimi K2.5 | 128K | 0.6/2.5 | ⭐⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐⭐ |
| **Alibaba** | Qwen3-Max | 256K+ | 0.7/2.6 | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐ |
| **MiniMax** | MiniMax-M3 (cloud) | 128K | 0.3/1.2 | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |
| **MiniMax** | MiniMax-M2.7 (legacy) | 128K | 0.3/1.2 | ⭐⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |

**По умолчанию в T3:**
- **Coding-задачи:** GLM-4.7 (SWE-bench 73.8%, agentic coding +5.8% vs GLM-4.6) — лучший open-source для Claude Code / Cline / Roo Code
- **Long-context / research:** Kimi K2.6 (256K+)
- **Vibe-coding (UI/UX):** MiniMax M2.7 (по нашему опыту — отлично делает UI)
- **Fallback:** Qwen3-Max (256K)

---

## 3. Конфигурация (config/models.yaml)

```yaml
providers:
  local:
    type: ollama
    base_url: http://127.0.0.1:11434
    default_timeout: 600
    models:
      - id: qwen3:8b
        tier: T1
        role: ["haiku", "simple-tasks", "tool-routing"]
        context: 128000
        cost_per_1m: 0.0
      - id: gemma4:12b
        tier: T1
        role: ["haiku", "multimodal"]
        context: 128000
        cost_per_1m: 0.0
      - id: qwen3-coder:30b
        tier: T2
        role: ["coding", "refactor", "agent"]
        context: 128000
        cost_per_1m: 0.0
        vram_required: 24  # GB, FP8
      - id: qwen3:32b
        tier: T2
        role: ["general", "planning", "analysis"]
        context: 128000
        cost_per_1m: 0.0
        ram_required: 64  # GB, full precision

  cloud:
    default_timeout: 300
    fallback_chain: [glm-4.7, kimi-k2.6, qwen3-max, minimax-m2.7]
    providers:
      - name: zhipu
        base_url: https://api.z.ai/v1
        api_key: ${ZHIPU_API_KEY}
        models:
          - id: glm-4.7
            tier: T3
            role: ["coding", "agentic"]
            cost_per_1m_in: 0.6
            cost_per_1m_out: 2.2
          - id: glm-4.6
            tier: T3
            role: ["coding", "fallback"]
            cost_per_1m_in: 0.6
            cost_per_1m_out: 2.2
      - name: moonshot
        base_url: https://api.moonshot.cn/v1
        api_key: ${MOONSHOT_API_KEY}
        models:
          - id: kimi-k2.6
            tier: T3
            role: ["long-context", "research", "coding"]
            cost_per_1m_in: 0.6
            cost_per_1m_out: 2.5
          - id: kimi-k2.5
            tier: T3
            role: ["long-context", "fallback"]
            cost_per_1m_in: 0.6
            cost_per_1m_out: 2.5
      - name: alibaba
        base_url: https://dashscope.aliyuncs.com/compatible-mode/v1
        api_key: ${DASHSCOPE_API_KEY}
        models:
          - id: qwen3-max
            tier: T3
            role: ["general", "fallback"]
            cost_per_1m_in: 0.7
            cost_per_1m_out: 2.6
      - name: minimax
        base_url: https://api.minimax.io/anthropic
        api_key: ${MINIMAX_API_KEY}
        models:
          - id: MiniMax-M3
            tier: T3
            role: ["vibe-coding", "default"]
            cost_per_1m_in: 0.3
            cost_per_1m_out: 1.2
          - id: MiniMax-M2.7
            tier: T3
            role: ["vibe-coding", "legacy"]
            cost_per_1m_in: 0.3
            cost_per_1m_out: 1.2
```

---

## 4. Cost-Aware Router (Phase 2)

**Логика маршрутизации:**

```python
def route_task(task):
    complexity = llm_classify_complexity(task)  # 0..1
    
    if complexity < 0.3:
        # Простые: T1 локально
        return "qwen3:8b"
    
    if complexity < 0.7:
        # Средние: T2 локально (если есть) или T3 облако
        if has_local_t2_available():
            return "qwen3-coder:30b" if task.is_coding else "qwen3:32b"
        return "glm-4.7"  # cheap T3
    
    # Сложные: T3 облако
    if task.is_coding:
        return "glm-4.7"  # SWE-bench 73.8%
    if task.needs_long_context:
        return "kimi-k2.6"  # 256K
    if task.is_ui:
        return "MiniMax-M3"  # vibe
    return "qwen3-max"  # fallback
```

**Эвристики для классификации сложности:**
- Длина задачи < 200 токенов → T1
- Наличие слов «рефактор», «архитектура», «multi-file» → T3
- Tool count > 5 → T2 минимум
- Coding + plan = >300 строк → T3
- Vision input → зависит от модели

---

## 5. Fallback Chain

Если основная модель не отвечает (429, 5xx, timeout):
```
1. Retry with exponential backoff (1s, 2s, 4s)
2. Fallback to same-tier alternative
3. Fallback to lower tier
4. Final: local model (всегда работает)
```

---

## 6. Бенчмарки (для справки)

### 6.1. Coding (SWE-bench Verified)

| Модель | Score | Date |
|--------|-------|------|
| Claude Sonnet 4.5 | 77.2% | 2025-09 |
| Claude Opus 4.7 | 80.1% | 2026-03 |
| GPT-5.1 | 76.8% | 2026-02 |
| **GLM-4.7** | **73.8%** | 2026-05 |
| Qwen3-Coder 30B A3B | 70.2% | 2026-04 |
| Qwen3 235B A22B | 71.4% | 2026-04 |
| Kimi K2.6 | 72.1% | 2026-05 |
| GLM-4.6 | 68.0% | 2026-04 |
| GLM-4.5 | 64.2% | 2026-04 |
| MiniMax M3 | ~65% (estimated) | 2026-06 |
| MiniMax M2.7 | ~58% (estimated) | 2026-05 |
| Qwen3 8B | 42.5% | 2025-12 |
| Gemma 4 12B | 48.3% | 2026-04 |

### 6.2. HumanEval+

| Модель | Score |
|--------|-------|
| Qwen3-Coder 30B A3B | 92.1% |
| GLM-4.7 | 91.8% |
| Qwen3 235B A22B | 90.5% |
| Qwen3 32B | 88.7% |
| Kimi K2.6 | 89.2% |
| MiniMax M3 | ~89% (estimated) | 2026-06 |
| MiniMax M2.7 | 87.4% |
| Qwen3 8B | 76.8% |
| Gemma 4 12B | 78.2% |

### 6.3. LiveCodeBench v5 (coding contest)

| Модель | Score |
|--------|-------|
| GLM-4.7 | 68.4% |
| Kimi K2.6 | 65.7% |
| Qwen3-Coder 30B A3B | 64.1% |
| Qwen3 235B | 62.3% |
| Qwen3 8B | 41.2% |

### 6.4. τ²-Bench (tool-use)

| Модель | Score |
|--------|-------|
| GLM-4.7 | 78.6% |
| Qwen3 235B | 72.4% |
| Qwen3-Coder 30B A3B | 70.1% |
| Kimi K2.6 | 68.9% |
| Qwen3 8B | 55.4% |

### 6.5. Vibe coding (UI/UX качество, нет формального бенчмарка)

| Модель | Качество |
|--------|----------|
| MiniMax M3 | ⭐⭐⭐⭐⭐ (по нашему опыту, + 1M context) |
| MiniMax M2.7 | ⭐⭐⭐⭐ (legacy) |
| GLM-4.7 | ⭐⭐⭐⭐ |
| Kimi K2.6 | ⭐⭐⭐ |
| Qwen3-Coder | ⭐⭐⭐ |

---

## 7. Требования к железу

### 7.1. Для T1 (≤12B)

| Параметр | Минимум | Рекомендуется |
|----------|---------|---------------|
| RAM | 16 GB | 32 GB |
| VRAM | — | 8 GB (опционально, для ускорения) |
| Disk | 30 GB | 50 GB (для нескольких моделей) |
| CPU | 8 cores | 16 cores |

### 7.2. Для T2 (30–70B)

| Параметр | FP8 | FP16 |
|----------|-----|------|
| RAM | 32 GB | 64 GB |
| VRAM | 24 GB (1× RTX 4090) | 48 GB (2× RTX 4090 / A6000) |
| Disk | 60 GB | 120 GB |

### 7.3. Для T2 (235B+ MoE)

| Параметр | Минимум | Рекомендуется |
|----------|---------|---------------|
| RAM | 96 GB | 256 GB |
| VRAM | 48 GB (2× A100) | 80 GB (2× H100) |
| Disk | 250 GB | 500 GB |

> **Прагматичная рекомендация:** для T2 (30B) держать **Qwen3-Coder 30B FP8** (24GB VRAM, помещается в RTX 4090), для T3 — облако.

---

## 8. Конфигурация для разных сценариев Марка

### 8.1. Coding (PLAST, ЕПУТС, общие проекты)

**Локально:** Qwen3-Coder 30B A3B (FP8)
**Облако:** GLM-4.7 (по умолчанию), Kimi K2.6 (для больших контекстов)

### 8.2. UI/UX (вибе-кодинг, презентации, визуал)

**Облако:** MiniMax M2.7 (по опыту Марка — лучший vibe)
**Локально:** нет хороших open-source для vibe (пока)

### 8.3. Long-context (большие документы, RAG)

**Облако:** **MiniMax M3 (1M!)**, Kimi K2.6 (256K), Qwen3-Max (256K)
**Локально:** Qwen3 32B (128K, 64GB RAM)

### 8.4. Tool-use-heavy (MCP, авто-агенты)

**Локально:** Qwen3 8B для простых, Qwen3-Coder 30B для сложных
**Облако:** GLM-4.7 (τ²-Bench 78.6%)

---

## 9. Известные ограничения моделей

### 9.1. Все open-source ≤12B

- ⚠️ **Слабый reasoning на длинных цепочках** — теряют контекст после 5+ turns
- ⚠️ **Hallucination в tool-use** — могут вызвать несуществующие tools
- ⚠️ **JSON-mode не гарантирован** — нужны retries
- **Workaround:** instructor + Pydantic + retry-loop

### 9.2. Qwen3-Coder 30B

- ⚠️ **Long context degrades after 64K** — нужен retrieval
- ⚠️ **Не умеет vision** (есть qwen3-vl вариант)
- ✅ **Лучший tool-use в своём классе**

### 9.3. GLM-4.7

- ⚠️ **Стоимость** — $0.6/2.2 per 1M (не самый дешёвый)
- ✅ **Лучший agentic coding** в open-source
- ✅ **Tool-use 78.6%** на τ²-Bench

### 9.4. MiniMax M2.7

- ⚠️ **Closed-source** — нет доступа к весам
- ✅ **Дешёвый** ($0.3/1.2)
- ✅ **Отличный vibe-coding**

### 9.5. Gemma 4 12B

- ⚠️ **Меньше community-контента** чем у Qwen
- ✅ **Gemma license** — commercial OK
- ✅ **Native function calling** (по docs Google)

---

## 10. Версионирование

- v1.0 — 2026-06-13 — первая версия по запросу Марка
  - Включены: Gemma 4 12B, Qwen3 8B/32B/235B, Qwen3-Coder 30B, GLM-4.5/4.6/4.7, Kimi K2.5/2.6, MiniMax M2.7
  - Excluded: DeepSeek (по запросу Марка)
  - Бенчмарки: SWE-bench + HumanEval + LiveCodeBench + τ²-Bench

---

*Список будет обновляться по мере выхода новых моделей. Главный ориентир — наши сценарии (PLAST/ЕПУТС/тендеры/общие coding), а не только бенчмарки.*
