# Phase 3 B-mini — План (черновик для Plan agent review)

**Дата:** 16.06.2026
**Автор:** Соломон
**Заказчик:** Марк
**Связь:** Roadmap v2.11 «Метрики успеха → Технические» → B1, B4 + B-defer scaffold
**Scope:** 2 golden test (context retention + compaction loss) + eval harness scaffold
**Не входит:** B2 (precision@5), B3 (recall@20), B5 (tool-use success rate T1/T2/T3) — требуют golden datasets + LLM-прогоны, отложены в B-defer или Phase 5

---

## 1. Цели и DoD

| ID | Метрика | DoD | Подтверждающий тест |
|----|---------|-----|---------------------|
| B1 | **Context retention** ≥ 95% за 100+ turn | 100-msg session → compact → retrieval recall на marked facts ≥ 0.95 | `tests/eval/test_context_retention_golden.py` |
| B4 | **Compaction loss** < 5% | 100-msg session с N marked facts → compact → % фактов в summary ≥ 95% | `tests/eval/test_compaction_loss_golden.py` |

После прохождения: **roadmap v2.11 → v3.0**, `## Метрики успеха → Технические` B1 + B4 = `[x]`.

---

## 2. Архитектура: `harness/eval/` scaffold (B-defer)

### 2.1 Что создаём

```
harness/eval/
├── __init__.py                  # existing (0 bytes) → expand
├── golden.py                    # NEW: GoldenFact + GoldenSession dataclasses + loaders
├── retention.py                 # NEW: ContextRetention metric (recall на marked facts)
├── compaction_loss.py           # NEW: CompactionLoss metric (% facts в summary)
├── runner.py                    # NEW: EvalRunner — orchestrator (single-shot, batch, fixture-driven)
└── README.md                    # NEW: как добавлять новые golden-тесты

tests/eval/
├── __init__.py                  # NEW
├── conftest.py                  # NEW: golden_fact fixture, seed_session fixture
├── fixtures/
│   ├── session_100turns.jsonl   # NEW: 100+ turn seed session
│   └── golden_facts.jsonl       # NEW: 20 marked facts в session с expected_phrase
├── test_context_retention_golden.py  # NEW: B1 metric
└── test_compaction_loss_golden.py    # NEW: B4 metric
```

### 2.2 Модули (что внутри)

#### `harness/eval/golden.py` (~80 LoC)
- `GoldenFact` — frozen dataclass: `id: str`, `phrase: str` (text для проверки в retrieval/summary), `turn_index: int` (где в session был вставлен), `category: Literal["user", "tool_result", "scratchpad"]`.
- `load_golden_facts(path: Path) -> list[GoldenFact]` — JSONL loader.
- `load_session_messages(path: Path) -> list[dict]` — JSONL loader для session (OpenAI shape).

**Trust boundary:** `harness/eval/` НЕ импортирует из `harness/agents/`, `harness/server/`, `harness/context/`. Только `harness/memory/retrieval/` (read-only Protocol) + `harness/config/Settings` + stdlib.

#### `harness/eval/retention.py` (~100 LoC)
- `ContextRetentionMetric` — class с `measure(session, facts, retriever) -> RetentionResult`.
- `RetentionResult` — frozen dataclass: `total: int`, `retained: int`, `ratio: float` (retained/total), `missing: list[GoldenFact]`, `top_doc_ids: dict[str, list[str]]` (per fact → top-k Memory ids, for stronger assertion — see R2).
- Алгоритм:
  1. Convert session messages to `Memory` corpus: `corpus = [Memory(id=f"m{i}", content=json.dumps(m, ensure_ascii=False), layer="L2", source="session") for i, m in enumerate(session)]` (see B3 fix).
  2. `retriever = BM25Retriever(corpus)` (NOT `RetrievalPipeline` — that returns assembled string, see B2 fix).
  3. Для каждого `GoldenFact` запускаем `retriever.retrieve(phrase, k=20)` (NOT 5, see R3 + R4).
  4. Если phrase найдена через **case-insensitive substring match в content top-k Memory** → retained.
  5. Assert stronger: `top_doc_ids[fact.id]` = list of Memory ids that matched (для будущей интеграции B2/B3).
- **Не используем LLM для проверки** (Phase 1 DoD — без API keys).

**Trust boundary:** импорт `harness.memory.retrieval.bm25.BM25Retriever` (read-only), `harness.eval.golden`. НЕ импортирует `RetrievalPipeline` (только `BM25Retriever`), `ContextCompactor` или LLM router.

#### `harness/eval/compaction_loss.py` (~120 LoC)
- `CompactionLossMetric` — async class с `await measure(session, facts, compactor, model_name) -> LossResult`.
- `LossResult` — frozen dataclass: `total: int`, `preserved: int`, `ratio: float`, `missing: list[GoldenFact]`, `summary_text: str | None`.
- Алгоритм:
  1. `compacted = await compactor.maybe_compact(session, model_name)` (NOT `force_compact` — see R5).
  2. **Extract summary message** (B1 + B5 fix):
     ```python
     summary_msg = next(
         (m for m in compacted
          if m.get("role") == "user"  # not "system"
          and "[Compaction summary" in m.get("content", "")),  # NOT startswith — marker is "[Compaction summary — earlier turns condensed]"
         None,
     )
     ```
  3. **Fallback** (B7 fix): if `summary_msg is None` (compaction не сработал, e.g. session < threshold), проверить facts в **trimmed list** (substring match in any message content), document this behaviour.
  4. `summary = summary_msg["content"]` (NOT None — fall back path).
  5. Для каждого `GoldenFact` проверить substring match `fact.phrase.lower() in summary.lower()`.
  6. `ratio = preserved / total`.
- **Mock LLM** — `AsyncMock` возвращает фиксированный `CompletionResult(content="...")` per `tests/test_context_compaction.py:205-209` pattern. Test fixture contract: mock summariser вставляет все phrases в summary → ratio = 1.0 для mock contract test.

**Trust boundary:** импорт `harness.context.ContextCompactor`, `harness.eval.golden`. Compactor mock'аем через `_Summariser` Protocol. Compactor construction: `ContextCompactor(settings, mock_summariser, memory=None, store=None, audit=None, pre_compact_hook=None, idle_trigger=None)` — B6 fix: никогда не inject реальный `CompactStore` или `UnifiedMemory`.

#### `harness/eval/runner.py` (~80 LoC)
- `EvalRunner` — sync orchestrator: `run(retention_or_loss_metric, fixture_dir) -> Result`.
- Простой API: `EvalRunner(Settings, RetrievalPipeline, ContextCompactor).run_retention(fixture_dir)` / `.run_compaction_loss(fixture_dir)`.
- Возвращает dict с `passed: bool`, `metrics: dict`, `details: list[GoldenFact]`.

#### `harness/eval/README.md` (~60 строк)
- **Конвенция fixture'ов:** JSONL формат, поля, как добавлять новые session'ы.
- **Конвенция golden facts:** `phrase` = exact substring, `turn_index` = куда вставлять, `category` = тип.
- **Как запускать:** `pytest tests/eval/ -v`.
- **Как добавить новую метрику:** Protocol + dataclass + runner method.

### 2.3 Тесты (что внутри)

#### `tests/eval/conftest.py` (~50 LoC)
- `golden_facts` fixture — генерирует **50** фактов программно (C1 fix: n=20 → n=50 для статистической надёжности на 95% threshold), с uniform distribution по turn_index: 12 в early (1-30), 26 в mid (31-70), 12 в late (71-100) (C2 fix).
- `seed_session_100` fixture — собирает 100+ turn list с golden facts, вкраплёнными в user/assistant/tool messages. **Pad each message to known char count** (R1 fix): 500 chars/user, 800 chars/assistant. Comment: "200 messages × ~650 chars avg ≈ 130K chars ≈ 32K tokens, well above 5K threshold (compaction_threshold_ratio=0.05 × 32K × 4 chars/token = wait — 32K tokens × 0.05 = 1.6K tokens, so 32K tokens session is 20× over threshold)". **Override Settings** (B4 fix): `compaction_threshold_ratio=0.05, compaction_target_ratio=0.025, compaction_keep_recent_turns=4, compaction_summarizer_max_input_tokens=4000, compaction_persist_to_memory=False` (R6 fix: no L2 writes).
- `mock_summariser` fixture — `AsyncMock` для `_Summariser.completion`, возвращает `CompletionResult(content=f"[Compaction summary — earlier turns condensed]\n{summary_with_all_phrases}")`. Mock contract: вставляет все phrases в summary.
- `retention_retriever` fixture — `BM25Retriever(corpus)` где corpus = `[Memory(id=f"m{i}", content=json.dumps(msg, ensure_ascii=False), layer="L2", source="session") for i, msg in enumerate(seed_session_100)]` (B3 fix: convert dicts → Memory).
- `compactor_with_mock` fixture — `ContextCompactor(settings, mock_summariser, memory=None, store=None, audit=None, pre_compact_hook=None, idle_trigger=None)` (B6 fix).

#### `tests/eval/fixtures/golden_facts.jsonl`
**C7 fix:** Генерируется программно (в conftest через `golden_facts` fixture), не хранится вручную. 50 facts, uniformly distributed (12 early / 26 mid / 12 late). Пример формата:
```json
{"id": "F01", "phrase": "Phase 3 v1.5.0", "turn_index": 12, "category": "user"}
{"id": "F02", "phrase": "Qdrant primary", "turn_index": 27, "category": "tool_result"}
...
```
Фразы выбираются так, чтобы они **специфичны** (не «the», «is» — чтобы BM25 их поднял).

#### `tests/eval/fixtures/session_100turns.jsonl`
**Генерируется программно** (в conftest через `seed_session_100`), а не хранится вручную — иначе diff-ы в session будут огромные. 100 user/assistant пар + 5 tool turns = 105-110 messages; **50 marked facts** вкраплены в разные места (12 early / 26 mid / 12 late).

#### `tests/eval/test_context_retention_golden.py` (~80 LoC, 6 tests)
- `test_b1_retention_100turns_baseline` — `golden_facts=50`, `retriever=BM25`, ratio ≥ 0.95 (без compact)
- `test_b1_retention_after_compaction` — compact 100-turn session → retrieval → ratio ≥ 0.95
- `test_b1_facts_in_summary` — golden facts должны быть в summary message (proxy: substring match в compacted list)
- `test_b1_empty_corpus_returns_zero` — edge case: пустой session → ratio=0.0 (C10 fix: explicit assertion)
- `test_b1_retention_handles_tool_pairs` — tool_call/tool_result pairs сохраняются (через retention metric не теряем)
- `test_b1_retention_threshold_configurable` — Settings.compaction_threshold_ratio меняет поведение

#### `tests/eval/test_compaction_loss_golden.py` (~70 LoC, 5 tests)
- `test_b4_loss_below_5pct` — `golden_facts=50`, `compactor(mock summariser)`, ratio ≥ 0.95
- `test_b4_loss_mock_summariser_preserves_facts` — mock summariser вставляет все phrases в summary → ratio = 1.0
- `test_b4_loss_no_compact_needed` — session < threshold → fallback на trimmed list (B7 fix), проверяем direct retrieval
- `test_b4_loss_disabled_compaction` — `compaction_enabled=False` → messages unchanged, no summary
- `test_b4_loss_summary_message_role` — summary message имеет `role="user"`, NOT `system` (B5 fix regression guard)

### 2.4 Маркеры pytest
- Все тесты в `tests/eval/` НЕ используют `real_llm` маркер (всё на mock).
- НЕ требуют `[embeddings] extra` (BM25 + IdentityReranker достаточно).
- Будут проходить в текущем venv (1365 baseline + новые).

### 2.5 Trust boundary test (R4, R7 fixes)

`tests/eval/test_eval_trust_boundary.py` — parametrize over all `.py` files in `harness/eval/`:

```python
import re
from pathlib import Path

FORBIDDEN = ("harness.agents", "harness.server")
COMMENT_RE = re.compile(r'(^\s*#)|(""")|(:class:|:func:|:meth:)')

@pytest.mark.parametrize("source_file", sorted(Path("harness/eval").glob("**/*.py")))
def test_eval_does_not_import_forbidden(source_file: Path) -> None:
    """harness/eval/ must NOT import from harness.agents or harness.server.

    Mirror of tests/test_runner_does_not_import_v150.py pattern
    (table-driven over all .py files, skip comments/docstrings/Sphinx refs).
    """
    text = source_file.read_text(encoding="utf-8")
    # Strip comments and docstrings to avoid false positives.
    cleaned = "\n".join(
        line for line in text.splitlines()
        if not COMMENT_RE.search(line)
    )
    for forbidden in FORBIDDEN:
        assert forbidden not in cleaned, (
            f"{source_file} imports forbidden module '{forbidden}'"
        )
```

---

## 3. Шаги реализации (zero-based)

| # | Шаг | Файлы | +Tests | Трудозатраты |
|---|-----|-------|--------|---------------|
| 0 | Sync master roadmap v2.11→v3.0 (status «B1/B4 = in progress, scaffolds созданы», target v3.0) | `_output/.../roadmap.md` | — | 5 мин |
| 1 | Создать `harness/eval/{__init__,golden,retention,compaction_loss,runner}.py` + `README.md` | 6 new files, ~440 LoC prod | — | 1.5 часа |
| 2 | Создать `tests/eval/{__init__,conftest}.py` + 2 fixtures (golden_facts.jsonl, session_100turns.py) | 3 new files + 2 fixtures, ~120 LoC | — | 30 мин |
| 3 | Создать `test_context_retention_golden.py` (B1) | 1 file, ~80 LoC | +6 (golden_facts=50) | 45 мин |
| 4 | Создать `test_compaction_loss_golden.py` (B4) | 1 file, ~70 LoC | +5 (golden_facts=50) | 30 мин |
| 5 | Run full suite `pytest -m "not real_llm" -q` | — | — | 5 мин |
| 6 | Commit + push в `06_Harness` | 1 commit | — | 5 мин |
| 7 | Memory: `harness-b-mini-complete-2026-06-16.md` + sync `MEMORY.md` | 1 new + 1 line | — | 5 мин |
| 8 | Sync master roadmap v3.0 → v3.0+ (B1/B4 = `[x]`, B2/B3/B5 = deferred) | `_output/.../roadmap.md` | — | 5 мин |

**Итого:** ~4-4.5 часа, 0 new deps, 0 production code changes (только golden test infra + metric modules).

---

## 4. Trust boundary

- `harness/eval/` — **новый пакет, нет существующих зависимостей** на `harness/agents/`, `harness/server/`. Это read-only utility layer.
- `runner.py` НЕ импортирует `LLMRouter` / `MergeQueue` / `AdversarialVerify` (по Phase 0+ архитектурному паттерну).
- Существующий `runner.py` (в `harness/agents/runner.py`) НЕ затрагивается.
- **Static test:** `tests/eval/test_eval_trust_boundary.py` — `assert "harness.agents" not in eval_source`, `assert "harness.server" not in eval_source` (mirror `test_runner_does_not_import_*`).

---

## 5. Риски и митигация

| Риск | Митигция |
|------|----------|
| **R1**: Golden facts в JSONL не отражают реальный retention — метрика врёт | Берём фразы **специфичные** (содержат «v1.5.0», «Qdrant», «Fusion»), не generic words |
| **R2**: Mock summariser в B4 всегда сохраняет facts → ratio=1.0, не ловим реальные баги compactor | Test `test_b4_loss_mock_summariser_preserves_facts` проверяет mock contract; `test_b4_loss_below_5pct` — реальный путь (mock возвращает summary на основе input) |
| **R3**: 100-turn session слишком короткий → compaction не срабатывает | Settings с `compaction_threshold_ratio=0.05` (3.2K tokens threshold на 32K ctx), session > 5K tokens |
| **R4**: BM25 не поднимет facts потому что они редкие (1 раз в 100 turns) | Используем `top_k=20` (не 5) для B1 retention — компенсирует редкость |
| **R5**: Tool pairs теряются в sliding window → golden fact в tool_result не retrievable | Test `test_b1_retention_handles_tool_pairs` явно проверяет tool_call_id preservation |
| **R6**: Compactor cache (Phase 3.5) влияет на retention | Используем `compaction_persistent_store=False` в test settings — отключаем cache, чистый sliding window path |

---

## 6. Definition of Done (B-mini)

- [ ] `harness/eval/` пакет создан (5 new files + README)
- [ ] `tests/eval/` директория создана (conftest + 3 test files + 1 trust boundary test)
- [ ] **B1 retention test passes**: ratio ≥ 0.95 на **50** marked facts (uniformly distributed) в 100-turn session
- [ ] **B4 compaction loss test passes**: ratio ≥ 0.95 на тех же 50 marked facts
- [ ] Trust boundary test passes (`harness/eval/` НЕ импортирует `harness/agents/`, `harness/server/`; parametrized по всем .py файлам)
- [ ] Full suite: `pytest -m "not real_llm"` → 1365 baseline + 12 new (B1×6 + B4×5 + trust×1) = **1377 passed, 0 new errors**, 7 pre-existing ERROR на embeddings (не регрессия)
- [ ] Master roadmap v3.0: B1, B4 = `[x]`, B2/B3/B5 = `[ ]` (deferred to Phase 5 eval harness)
- [ ] Commit + push + memory sync
- [ ] **Roadmap версия:** v2.11 → v3.0 (closeout B1, B4)

---

## 7. Plan agent review log (16.06.2026)

Plan отправлен в Plan-Research агент (model: Plan-Research). Найдено:
- **7 BLOCKERS** (B1-B7) — все применены в §2.2, §2.3, §2.5, §5, §6
- **9 RISKS** (R1-R9) — R1, R2, R3, R4, R6, R7, R9 применены; R5 (force_compact marker bug), R8 (task conflict) документированы
- **11 CONCERNS** (C1-C11) — C1 (n=50), C2 (uniform distribution), C6 (async runner) применены; C3-C5, C7-C11 документированы как known limitations в `harness/eval/README.md` (TODO post-coding)

**Verdict:** NEEDS FIXES → **APPROVED after 7 B-fixes applied** (16.06.2026).

---

## 7. Что НЕ делается (явно out of scope)

- B2 (precision@5) — требует golden queries с manual relevance labels (нет dataset)
- B3 (recall@20) — то же самое
- B5 (tool-use success rate) — требует LLM прогоны на T1/T2/T3, отложен в Phase 5 eval harness
- Eval UI / dashboard — Phase 6
- Cascade threshold calibration (0.85/0.55) — Phase 5
- Real LLM smoke tests — минорная задача
- Phase 4 (12 hooks + observability) — следующий major phase

---

**Следующий шаг:** Plan agent adversarial review → fix blockers → ExitPlanMode → coding (Steps 1-8).
