# Multi-Model Support — краткий обзор

> **Полный каталог:** см. `docs/MODEL_REGISTRY.md` (44 KB, 4 бенчмарка, hardware requirements)

## TL;DR

Solomon Harness v1.0 поддерживает **гибридную работу с open-source LLM** через единый LiteLLM-абстрактор:

| Tier | Размер | Назначение | По умолчанию |
|------|--------|-----------|-------------|
| **T1 — Haiku-class** | 8–12B | Простые задачи, tool-use | **Qwen3 8B** (локально, Ollama) |
| **T2 — Sonnet-class** | 30–70B | Coding, refactor, planning | **Qwen3-Coder 30B A3B** (локально, FP8) |
| **T3 — Opus-class** | API | Сложный coding, vibe, long-context | **GLM-4.7** / **Kimi K2.6** / **MiniMax M3** |

## Поддерживаемые провайдеры

| Провайдер | Модели | URL |
|-----------|--------|-----|
| **Ollama** (локально) | Qwen3 8B/32B/235B, Qwen3-Coder 30B, Gemma 4 12B, GLM-4.5 | `http://127.0.0.1:11434` |
| **ZhipuAI / Z.ai** | GLM-4.7, GLM-4.6 | `https://api.z.ai/v1` |
| **Moonshot** | Kimi K2.6, Kimi K2.5 | `https://api.moonshot.cn/v1` |
| **Alibaba DashScope** | Qwen3-Max, Qwen3-Coder | `https://dashscope.aliyuncs.com` |
| **MiniMax** | MiniMax M3 (default), MiniMax M2.7 (legacy) | `https://api.minimax.io/anthropic` |
| **Anthropic** (опционально) | Claude Opus 4.7, Sonnet 4.6, Haiku 4.5 | `https://api.anthropic.com` |

## Почему именно эти модели

**T1 — Qwen3 8B:** лучший tool-use в ≤12B классе (по нашему опыту с mem0), Apache 2.0, идеален для routing-агента.

**T2 — Qwen3-Coder 30B A3B:** SWE-bench 70.2%, HumanEval+ 92.1%, помещается в 24GB VRAM (RTX 4090), MoE с 3B active.

**T3 — GLM-4.7:** SWE-bench 73.8% (лучший open-source для agentic coding), τ²-Bench 78.6%, поддерживает Claude Code/Cline/Roo Code из коробки.

**T3 — Kimi K2.6:** long-context (256K), good for research и больших RAG.

**T3 — MiniMax M3:** лучший vibe-coding (UI/UX, презентации), дешёвый ($0.3/$1.2 per 1M). Это модель самого Соломона.

## DeepSeek — НЕ входит

По решению Марка (13.06.2026) DeepSeek **исключён** из каталога. Причины не уточнялись.

## Gemma 4 12B

Включена в каталог как опция T1 для **multimodal** сценариев (vision), когда Qwen3 8B не подходит. Native function calling, Gemma license (commercial OK).

## Cost-Aware Router (Фаза 1.5)

Автоматический выбор модели по сложности задачи (см. `MODEL_REGISTRY.md` § 4):

```
простая задача (grep, edit, простой вопрос) → Qwen3 8B (бесплатно, локально)
medium (coding 100-300 строк) → Qwen3-Coder 30B (бесплатно) или GLM-4.7 ($0.6/2.2)
сложная (архитектура, multi-file) → GLM-4.7 (cloud)
vibe/UI → MiniMax M3
long-context → **MiniMax M3 (1M)** / Kimi K2.6 (256K)
```

## Hardware

| Tier | Минимум | Рекомендуется |
|------|---------|---------------|
| T1 | 16 GB RAM | 32 GB RAM |
| T2 (FP8) | 32 GB RAM, 24 GB VRAM | 64 GB RAM, 1× RTX 4090 |
| T2 (FP16) | 64 GB RAM | 128 GB RAM |
| T2 MoE (Qwen3-235B) | 96 GB RAM | 256 GB RAM, 2× H100 |
| T3 | — | Любое (облако) |

## Roadmap

- **Фаза 0** (нед. 1–2): Qwen3 8B + MiniMax M3
- **Фаза 0.5** (нед. 3): добавить T2 (Qwen3-Coder 30B) + GLM-4.7 + Kimi K2.6
- **Фаза 1.5** (нед. 7): cost-aware router
- **Фаза 5** (нед. 16–17): eval harness (SWE-bench style на наших сценариях)

## Excluded по решению Марка

- ❌ DeepSeek (без объяснения причин)
- ❌ OpenAI GPT-4/5 (проприетарный, по умолчанию open-source first)
- ❌ Claude Opus/Sonnet (тоже проприетарный; добавим как опцию в v1.1, если понадобится)
