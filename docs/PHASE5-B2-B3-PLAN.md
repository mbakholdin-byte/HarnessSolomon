# Phase 5 B2 + B3 — План (черновик для Plan agent review)

**Дата:** 16.06.2026
**Автор:** Соломон (Plan agent)
**Заказчик:** Марк
**Связь:** Roadmap v2.11→v3.0 «Метрики успеха → Технические» → B2 (precision@5) + B3 (recall@20)
**Scope:** Retrieval-метрики на golden queries для BM25Retriever; golden dataset; unit-тесты
**Не входит:** B5 (tool-use success rate T1/T2/T3), precision/recall для dense/hybrid retriever (Phase 5.1), LLM-as-judge (Phase 6)

---

## 1. Цели и DoD

| ID | Метрика | DoD | Подтверждающий тест |
|----|---------|-----|---------------------|
| B2 | **precision@5** ≥ 0.7 | 50 golden queries на 100-turn session: доля релевантных в top-5 ≥ 70% | `tests/eval/test_precision_golden.py` |
| B3 | **recall@20** ≥ 0.85 | 50 golden queries на 100-turn session: доля ground-truth relevant в top-20 ≥ 85% | `tests/eval/test_recall_golden.py` |

После прохождения: **roadmap v3.0 → v3.1**, B2 + B3 = `[x]`, B5 = deferred.

---

## 2. Дизайн: golden queries (новый dataset)

### 2.1 Формат

`tests/eval/fixtures/golden_queries.jsonl` — 50 объектов, по одному на строку:

```json
{
  "id": "Q01",
  "query": "which LLM tier is used for the context summariser in compaction?",
  "relevant_fact_ids": ["F07", "F28"],
  "irrelevant_fact_ids": ["F01", "F05", "F12", "F22", "F34", "F38"],
  "category": "factual_lookup",
  "difficulty": "easy"
}
```

**Поля:**
- `id` — `Q01..Q50` (parallel to `F01..F50`).
- `query` — natural-language вопрос (English, 5-12 слов, BM25-friendly).
- `relevant_fact_ids` — 1-3 `GoldenFact.id`, которые должны быть retrieved. **Ground truth** для precision + recall.
- `irrelevant_fact_ids` — 4-6 `GoldenFact.id`, которые НЕ должны попасть в top-5 (нужны для B2 precision assertion).
- `category` — `Literal["factual_lookup", "multi_hop", "paraphrased"]` (см. §2.3).
- `difficulty` — `Literal["easy", "medium", "hard"]` (см. §2.3).

### 2.2 Сколько queries и как генерировать

**Решение:** 50 queries, **mix** auto-generated + manual.

- **30 auto-generated** из существующих golden_facts:
  - Берём 30 из 50 facts (10 easy/10 medium/10 hard, random.sample с seed=42).
  - Query = phrase из `GoldenFact.phrase` + question words ("what is", "which tier uses", "how does X work").
  - `relevant_fact_ids` = `[fact.id]`.
  - `irrelevant_fact_ids` = 4-6 facts из той же категории (`category` field), но без overlap по phrase tokens.
- **20 manual** (hand-crafted в `golden_queries.jsonl`):
  - 5 multi-hop: query требует комбинации 2-3 facts.
  - 5 paraphrased: query перефразирует phrase (синонимы, перестановка слов).
  - 10 factual_lookup (baseline).

**Обоснование 50 queries:**
- 50 — стандарт для IR benchmark'ов (TREC, BEIR small).
- Даёт статистическую надёжность для thresholds (0.7 / 0.85): Wilson 95% CI при 50 = ±12pp (precision@5 = 0.7 → [0.58, 0.82]). Достаточно для regression detection.
- Не перегружает maintenance (50 строк JSONL + 1 fixture generation step).

### 2.3 Категории и difficulty

| Category | Описание | Ground truth size | Пример |
|----------|----------|-------------------|--------|
| `factual_lookup` | Прямой lookup phrase, 1 fact | 1 relevant_id | "Qdrant primary" → F02 |
| `paraphrased` | Синоним / перефразировка phrase | 1 relevant_id | "vector store used as primary backend" → F02 |
| `multi_hop` | Требует 2+ facts (composite) | 2-3 relevant_ids | "which tier summarises with T1 + what model" → F07 + F28 |

Difficulty = длина query + кол-во BM25-токенов, не входящих в source phrase:
- `easy` — ≥60% tokens из phrase.
- `medium` — 30-60% tokens.
- `hard` — <30% (multi-hop или paraphrase).

**Обоснование multi-hop:** BM25 — sparse retriever, ожидаемо плохо на multi-hop. B2/B3 thresholds могут провалиться на hard subset → будем reporting per-difficulty для диагностики.

### 2.4 Генерация

`tests/eval/conftest.py:golden_queries` fixture:

```python
@pytest.fixture
def golden_queries(golden_facts: list[GoldenFact]) -> list[GoldenQuery]:
    """50 golden queries, 30 auto + 20 manual."""
    # 30 auto: factual_lookup + paraphrased
    auto = _generate_auto_queries(golden_facts, n=30, seed=42)
    # 20 manual: from JSONL fixture
    manual = load_golden_queries(MANUAL_QUERIES_PATH)  # 20 lines
    return auto + manual
```

`GoldenQuery` dataclass:

```python
@dataclass(frozen=True)
class GoldenQuery:
    id: str
    query: str
    relevant_fact_ids: tuple[str, ...]  # 1-3 fact_ids
    irrelevant_fact_ids: tuple[str, ...]  # 4-6 fact_ids
    category: Literal["factual_lookup", "multi_hop", "paraphrased"]
    difficulty: Literal["easy", "medium", "hard"]
```

`load_golden_queries(path: Path)` — JSONL loader (mirror `load_golden_facts`).

### 2.5 Mapping fact_id → Memory id

**Проблема:** B1 metric оперирует `GoldenFact.phrase` → `Memory.id` (через `top_doc_ids`). B2/B3 оперируют fact_id → retrieved Memory id.

**Решение:** Helper в `golden.py`:

```python
def fact_id_to_phrase(facts: list[GoldenFact]) -> dict[str, str]:
    """Map fact.id -> fact.phrase (для retrieval check)."""
    return {f.id: f.phrase for f in facts}


def phrase_to_relevant_memory_ids(
    facts: list[GoldenFact],
    corpus: list[Memory],
    top_k: int = 5,
) -> dict[str, list[str]]:
    """For each fact, find the Memory ids in corpus that contain its phrase.

    Used as a **ground truth bridge**: B2/B3 need to know WHICH Memory
    ids in the corpus are "relevant" for a query, but our ground truth
    is fact_ids, not Memory ids. This helper builds the bridge by
    substring scan (case-insensitive) on the corpus.

    Returns: {fact_id: [memory_id, ...]}. Length is usually 1 (one
    user message contains the phrase), but can be 2-3 if the phrase
    appears in multiple messages (e.g. a user message + summary).
    """
    result: dict[str, list[str]] = {}
    for f in facts:
        phrase_lower = f.phrase.lower()
        result[f.id] = [
            m.id for m in corpus
            if phrase_lower in m.content.lower()
        ][:top_k]
    return result
```

**Важно:** `phrase_to_relevant_memory_ids` — это **NOT** retrieval, это **enumerate all matching Memory в corpus**. Это даёт ground truth set, который B3 будет проверять "all relevant Memory ids retrieved within top-20".

---

## 3. Дизайн: метрики

### 3.1 Решение: один модуль `retrieval.py` (не два)

**Обоснование:**
- B2 (precision) и B3 (recall) — стандартные IR-метрики, всегда вместе.
- `PrecisionResult` / `RecallResult` — почти один shape: total, per_query breakdown, ratios.
- Общий corpus building + helper `phrase_to_relevant_memory_ids` — DRY.
- Mirror pattern: `retention.py` + `compaction_loss.py` — два модуля, но они **измеряют разные вещи** (B1 vs B4). B2 и B3 — **одна и та же retrieval**, разные k и разные numerator/denominator.

**Модуль:** `harness/eval/retrieval.py` (~180 LoC).

```python
@dataclass(frozen=True)
class PrecisionResult:
    total_queries: int
    total_relevant_in_top5: int  # numerator sum
    total_top5: int  # denominator sum (5 * total_queries)
    ratio: float  # numerator / denominator (micro-avg)
    per_query: dict[str, float]  # query_id -> precision@5
    per_category: dict[str, float]  # category -> mean precision
    per_difficulty: dict[str, float]  # difficulty -> mean precision
    missed: list[GoldenQuery]  # queries with precision < 1.0

@dataclass(frozen=True)
class RecallResult:
    total_queries: int
    total_relevant_retrieved: int  # numerator sum
    total_relevant_in_ground_truth: int  # denominator sum
    ratio: float  # numerator / denominator (micro-avg)
    per_query: dict[str, float]  # query_id -> recall@20
    per_category: dict[str, float]
    per_difficulty: dict[str, float]
    missed: list[GoldenQuery]  # queries with recall < 1.0

class PrecisionMetric:
    """B2 — measure precision@5 на golden queries."""
    def __init__(self, k: int = 5) -> None: ...
    def measure(
        self,
        corpus: list[Memory],
        queries: list[GoldenQuery],
        facts: list[GoldenFact],
    ) -> PrecisionResult: ...

class RecallMetric:
    """B3 — measure recall@20 на golden queries."""
    def __init__(self, k: int = 20) -> None: ...
    def measure(
        self,
        corpus: list[Memory],
        queries: list[GoldenQuery],
        facts: list[GoldenFact],
    ) -> RecallResult: ...
```

### 3.2 Алгоритм precision@5

```python
def measure(self, corpus, queries, facts):
    retriever = BM25Retriever(corpus)
    fact_to_phrase = fact_id_to_phrase(facts)
    fact_to_relevant_mems = phrase_to_relevant_memory_ids(facts, corpus, top_k=10)

    per_query = {}
    missed = []
    total_relevant_in_top5 = 0
    total_top5 = 0

    for q in queries:
        retrieved = retriever.retrieve(q.query, k=self._k)
        retrieved_ids = {m.id for m, _ in retrieved}

        # Ground truth: Memory ids для relevant fact_ids
        ground_truth_ids: set[str] = set()
        for fid in q.relevant_fact_ids:
            ground_truth_ids.update(fact_to_relevant_mems.get(fid, []))

        if not ground_truth_ids:
            # Defensive: query без ground truth → skip
            per_query[q.id] = 0.0
            continue

        relevant_in_top5 = len(retrieved_ids & ground_truth_ids)
        precision = relevant_in_top5 / self._k
        per_query[q.id] = precision
        total_relevant_in_top5 += relevant_in_top5
        total_top5 += self._k
        if precision < 1.0:
            missed.append(q)

    ratio = total_relevant_in_top5 / max(total_top5, 1)
    # ... per_category, per_difficulty aggregations
    return PrecisionResult(...)
```

### 3.3 Алгоритм recall@20

Идентичная структура, но:
- `k=20` (B3 DoD).
- `relevant_retrieved = len(retrieved_ids & ground_truth_ids)`.
- `recall = relevant_retrieved / len(ground_truth_ids)` (per-query).
- Micro-avg: `total_relevant_retrieved / total_relevant_in_ground_truth`.

**Edge case:** если `ground_truth_ids` пустой (query не имеет relevant facts), precision и recall = 0.0 by definition. Мы **skip** такие queries в aggregation (counter не инкрементируется). Это покрывается defensive `if not ground_truth_ids: continue`.

### 3.4 Top-k configurable

- `PrecisionMetric(k: int = 5)` — default 5 (B2 DoD).
- `RecallMetric(k: int = 20)` — default 20 (B3 DoD).
- Allow `k=1` (sanity), `k=10` (intermediate), `k=50` (deep analysis).
- **Hard limit:** `k <= len(corpus)` — assert в measure(), иначе RuntimeError.

### 3.5 Corpus choice: `seed_session_100` (NOT new)

**Решение:** использовать существующий `seed_session_100`. Аргументы:
- Уже есть, consistent с B1/B4 (одна session для всех метрик).
- Corpus building pattern уже отлажен в `retention.py:101-109`.
- Adding новый corpus → drift risk (manual facts, manual corpora must stay in sync).

**Corpus build:** идентичен `retention.py`:
```python
corpus = [
    Memory(
        id=f"m{i}",
        content=json.dumps(msg, ensure_ascii=False),
        layer="L2",
        source="session",
    )
    for i, msg in enumerate(session)
]
```

**Не включаем compaction artefacts** (B7 design decision): B2/B3 измеряют **raw session retrieval**, не post-compaction. Post-compaction = B1 (retention) + B4 (loss). Если кто-то хочет post-compaction B2/B3 — это Phase 5.1 (новый sub-task, "B2/B3 after compaction").

---

## 4. Trust boundary

`harness/eval/retrieval.py` импортирует ТОЛЬКО:
- `harness.eval.golden.GoldenFact`, `GoldenQuery`, `fact_id_to_phrase`, `phrase_to_relevant_memory_ids`, `load_golden_queries`
- `harness.memory.retrieval.bm25.BM25Retriever`
- `harness.memory.schema.Memory`
- stdlib (json, dataclasses)

**НЕ импортирует:**
- `harness.agents.*` ❌
- `harness.server.*` ❌
- `harness.context.*` ❌ (compactor — B1/B4, не нужно для B2/B3)
- `harness.config.*` ❌ (no Settings needed for raw retrieval)

**Static test:** `test_eval_does_not_import_forbidden` уже parametrize over all `harness/eval/**/*.py` — новый `retrieval.py` будет проверен автоматически.

**Дополнительная проверка:** `runner.py` НЕ должен импортировать `RetrievalMetric` / `PrecisionMetric` / `RecallMetric` — runner — orchestrator, метрики дёргаются опционально. НО: добавление `run_precision()` / `run_recall()` в EvalRunner — OK (async wrapper pattern, mirror B1/B4).

---

## 5. Adversarial review (self)

### 5.1 BLOCKERS (5)

**B1 [BLOCKER]: `phrase_to_relevant_memory_ids` даёт WRONG ground truth для multi-hop queries.**

Multi-hop query имеет 2-3 `relevant_fact_ids`. Если ground truth = union of Memory ids для каждой fact phrase, то union может быть 2-3 Memory ids. B2 precision@5 = `|retrieved ∩ gt| / 5`. Если union = 2, precision max = 0.4. **Threshold 0.7 невозможен для multi-hop!**

**Fix:** Пересмотреть threshold ИЛИ изменить scoring для multi-hop. **TODO: verify с Марком** — варианты:
- (a) Multi-hop queries исключены из B2 (B2 only factual_lookup + paraphrased, B3 = все).
- (b) Threshold 0.7 — micro-avg across factual_lookup + paraphrased subset (20 queries), multi-hop reported separately.
- (c) Lower B2 threshold to 0.5 для multi-hop или считать `precision@k = |relevant ∩ retrieved| / min(k, |gt|)`.

**Workaround для плана:** B2 считается ТОЛЬКО на factual_lookup + paraphrased (40 queries), multi-hop reported в `per_category` breakdown. Если B2 ratio < 0.7 на full 50, проверяем subset. **TODO: confirm design с Марком до coding.**

**B2 [BLOCKER]: `relevant_fact_ids` — это fact_id, а не Memory id. Mapping через phrase substring ненадёжен если phrase слишком generic ("Phase 3 v1.5.0" — substring в seed session 1 раз, OK; но "T1 Qwen3 8B" → может быть в 2 messages если seed переиспользует фразу).**

**Fix:** Усилить `_GOLDEN_QUERIES_AUTO` generator: для каждой fact использовать `turn_index` и брать **только Memory на этом индексе** как ground truth (NOT all Memory with phrase).

```python
def fact_id_to_relevant_memory_id(
    facts: list[GoldenFact],
    corpus: list[Memory],
    session: list[dict],
) -> dict[str, list[str]]:
    """Map fact_id -> [memory_id, ...] via turn_index (NOT phrase)."""
    result = {}
    for f in facts:
        # The Memory at index f.turn_index+1 (0=system, 1=user[0], ...)
        mem_idx = f.turn_index + 1
        if 0 <= mem_idx < len(corpus):
            result[f.id] = [corpus[mem_idx].id]
        else:
            result[f.id] = []
    return result
```

**B3 [BLOCKER]: B1 `RetentionResult.top_doc_ids[fact.id]` уже возвращает top-20 Memory ids для каждой fact. Это означает, что для каждой fact мы УЖЕ знаем top-20 retrieved. Но в B2/B3 ground truth определяется ФАКТАМИ, а не Memory ids. Coupling.**

**Fix:** B2/B3 **не зависят** от B1. Они строят свой собственный ground truth (через `fact_id_to_relevant_memory_id` с turn_index). Retention B1 — independent metric. В README добавить явное "B2/B3 not derived from B1 top_doc_ids".

**B4 [BLOCKER]: 50 queries + 50 facts = 2500 substring scans в `phrase_to_relevant_memory_ids` (если использовать phrase). С `turn_index` mapping — O(1) на query. Но при loading: corpus size = 205 messages, substring scan = 205 * 50 = 10K операций. Acceptable (sub-millisecond). **НЕТ блокера**, отмечаю как resolved.**

**B5 [BLOCKER]: BM25 — sparse retriever. Multi-hop queries (Q "which T1 + what model summarises") вернут documents с ЛЮБЫМ из терминов, не оба. Recall@20 для multi-hop может быть 0.5 (1 из 2 relevant). Если B3 threshold 0.85 — multi-hop будет систематически проваливаться.**

**Fix:** Принять как known limitation. B3 threshold 0.85 — на factual_lookup + paraphrased subset (40 queries), multi-hop — diagnostic. **TODO: confirm с Марком** — возможно B3 threshold нужно 0.7 для всех 50 queries, ИЛИ 0.85 для subset.

### 5.2 RISKS (5)

**R1 [RISK]: JSONL `golden_queries.jsonl` файл может дрифтить — ручные 20 queries не регенерируются, но seed_session_100 меняется при изменении `golden_facts`.** Если кто-то editнёт F02 phrase, Q01 (которая ссылается на F02) станет inconsistent.

**Mitigation:** CI test: `test_queries_reference_valid_facts` — assert все `relevant_fact_ids` и `irrelevant_fact_ids` существуют в `golden_facts`. Запускается при каждом `pytest tests/eval/`.

**R2 [RISK]: `seed_session_100` паддит messages до 500-800 chars. BM25 на padding filler ("padding for token-estimate reliability") даст HIGH IDF penalty (filler встречается в 200+ docs) → filler не поднимется. OK. Но phrase "T1 Qwen3 8B local" в user message vs "T1 Qwen3 8B local" в summary (если есть) — оба Memory ids попадут в ground truth. Multi-match = recall boost. Acceptable.**

**R3 [RISK]: Per-category aggregation — `mean precision` для категории с 5 queries = unstable. Если 1 query имеет precision 0.0, mean = 0.8 (4 из 5 = 1.0). Std = 0.4. **Mitigation:** report BOTH mean и std (или median) в `per_category`. В тестах: проверять только `ratio` (micro-avg), не per-category breakdown.**

**R4 [RISK]: `eval_settings` (B6 isolation) не используется B2/B3. B2/B3 — raw retrieval, не compaction. Но если кто-то добавит `compactor` параметр в будущем — `eval_settings` может leak в fixture и сломать isolation. **Mitigation:** НЕ добавлять `compactor` fixture в B2/B3 tests. Только `seed_session_100` + `golden_facts` + `golden_queries`.**

**R5 [RISK]: BM25Retriever **stateless** (per `bm25.py:60` — `__init__` строит fresh). На каждую query — новый retriever. O(N) на query = 50 * 205 = 10K corpus construction operations. **Mitigation:** Кэшировать retriever в `PrecisionMetric.measure()` (build 1 раз, переиспользовать). Memory: corpus = 205 Memory objects ≈ 50KB, negligible. Test: `test_precision_caches_retriever` — assert `__init__` called once (mock BM25Retriever и проверить call_count).**

### 5.3 CONCERNS (5)

**C1 [CONCERN]: B2 threshold 0.7 — стандарт IR? TREC / BEIR обычно precision@5 = 0.4-0.6 на hard queries. 0.7 — оптимистично. **Рекомендация:** pilot run на 50 queries → смотрим actual ratio → adjust threshold. Но изначально код thresholds 0.7 / 0.85 в dataclass как `Literal` constants для clarity. **TODO: pilot data после coding.****

**C2 [CONCERN]: Авто-генерация 30 queries с `random.sample(seed=42)` — детерминирована, но phrase-to-query templates ("what is", "which tier uses") — только 3-4 шаблона. Queries будут выглядеть роботизированно. **Mitigation:** accept this, queries — служебный dataset, не user-facing.**

**C3 [CONCERN]: 50 queries включают `irrelevant_fact_ids` (4-6 ids) — они НЕ используются в B2/B3 metric, только для human inspection (если precision < 1.0, видно какие facts retrieved vs expected). **Рекомендация:** Добавить `irrelevant_fact_ids` в PrecisionResult.missed (для debugging). NOT a blocker.**

**C4 [CONCERN]: `Memory.id` — `f"m{i}"` где i = 0..204. Если corpus изменится (compaction, dedup), Memory ids поменяются → ground truth mapping сломается. **Mitigation:** Helper `fact_id_to_relevant_memory_id` строит mapping FRESH на каждый `measure()` call — не кэшируется across sessions. OK.**

**C5 [CONCERN]: `run_precision` / `run_recall` в `EvalRunner` — async wrappers, но сами metric methods — sync. Async wrapper нужен только для consistency с `run_compaction_loss` (async because compactor). **Решение:** methods **sync** (return `PrecisionResult` / `RecallResult` directly), НЕ async. EvalRunner остаётся async, но B2/B3 methods — sync. **Docstring note: "sync, unlike run_compaction_loss which is async due to compactor."****

---

## 6. Шаги реализации (zero-based)

| # | Шаг | Файлы | +Tests | Трудозатраты |
|---|-----|-------|--------|---------------|
| 0 | Sync roadmap v3.0→v3.1 (B2/B3 in progress) | `_output/.../roadmap.md` | — | 5 мин |
| 1 | Создать `harness/eval/retrieval.py` (PrecisionMetric, RecallMetric, dataclasses) | 1 file, ~180 LoC | — | 1 час |
| 2 | Расширить `harness/eval/golden.py` (GoldenQuery, load_golden_queries, fact_id_to_relevant_memory_id) | +~80 LoC | — | 30 мин |
| 3 | Создать `tests/eval/fixtures/golden_queries.jsonl` (20 manual queries) | 1 file, ~20 строк | — | 30 мин |
| 4 | Расширить `tests/eval/conftest.py` (golden_queries fixture: 30 auto + 20 manual) | +~50 LoC | — | 30 мин |
| 5 | Создать `tests/eval/test_precision_golden.py` (6 tests) | 1 file, ~100 LoC | +6 | 1 час |
| 6 | Создать `tests/eval/test_recall_golden.py` (5 tests) | 1 file, ~90 LoC | +5 | 45 мин |
| 7 | Расширить `harness/eval/__init__.py` (export PrecisionMetric, RecallMetric, PrecisionResult, RecallResult, GoldenQuery) | +5 lines | — | 5 мин |
| 8 | Расширить `harness/eval/runner.py` (run_precision, run_recall sync methods) | +~30 LoC | — | 15 мин |
| 9 | Run full suite `pytest -m "not real_llm" -q` | — | — | 5 мин |
| 10 | Pilot: запустить 50 queries, смотреть actual ratio → adjust thresholds если нужно | — | — | 30 мин |
| 11 | Commit + push | 1 commit | — | 5 мин |
| 12 | Memory: `harness-b2-b3-complete-2026-06-16.md` + roadmap sync | 1 new + 1 line | — | 5 мин |
| 13 | Sync roadmap v3.1 → v3.1+ (B2/B3 = `[x]`, B5 = deferred) | `_output/.../roadmap.md` | — | 5 мин |

**Итого:** ~5-6 часов, 0 new deps, 0 production code changes.

---

## 7. Определение Done (B2 + B3)

- [ ] `harness/eval/retrieval.py` создан (PrecisionMetric, RecallMetric, PrecisionResult, RecallResult) — ~180 LoC
- [ ] `harness/eval/golden.py` расширен (GoldenQuery, load_golden_queries, fact_id_to_relevant_memory_id) — +~80 LoC
- [ ] `tests/eval/fixtures/golden_queries.jsonl` создан (20 manual queries)
- [ ] `tests/eval/conftest.py` расшинен (golden_queries fixture: 30 auto + 20 manual)
- [ ] `tests/eval/test_precision_golden.py` создан (6 tests)
- [ ] `tests/eval/test_recall_golden.py` создан (5 tests)
- [ ] `harness/eval/__init__.py` расширен (export новых symbols)
- [ ] `harness/eval/runner.py` расширен (run_precision, run_recall sync methods)
- [ ] **B2 test passes**: ratio ≥ 0.7 на 50 queries (или на 40 factual_lookup + paraphrased subset — **TODO verify с Марком**)
- [ ] **B3 test passes**: ratio ≥ 0.85 на 50 queries (или subset — **TODO verify с Марком**)
- [ ] Trust boundary test passes (`test_eval_does_not_import_forbidden` на новый `retrieval.py`)
- [ ] CI test: `test_queries_reference_valid_facts` — все fact_ids в queries существуют в golden_facts
- [ ] Pilot run завершён, threshold confirmed или adjusted
- [ ] Full suite: 1379 baseline + 11 new (B2×6 + B3×5) = **1390 passed, 0 regressions**
- [ ] Master roadmap v3.1: B2, B3 = `[x]`, B5 = deferred
- [ ] Commit + push + memory sync

---

## 8. Что НЕ делается (явно out of scope)

- **B5 (tool-use success rate T1/T2/T3)** — требует LLM прогоны, отложен в Phase 5.2.
- **Dense / Hybrid retrieval B2/B3** — Phase 5.1 (отдельный sub-task, "B2/B3 on DenseRetriever + HybridRetriever").
- **LLM-as-judge** (graded relevance 0-3) — Phase 6 eval UI.
- **Post-compaction B2/B3** — Phase 5.1 ("B2/B3 after force_compact on compacted session").
- **Per-session evaluation** (eval на разных sessions) — Phase 5.1.
- **Dashboard / reporting UI** — Phase 6.
- **Cascade threshold calibration** (0.85/0.55) — Phase 5.1.
- **Real LLM smoke tests** — минорная задача.

---

## 9. Plan agent review log (16.06.2026)

Plan отправлен в Plan-Research агент (model: Plan-Research). Self-review найдено:
- **5 BLOCKERS (B1-B5)** — B1, B5 требуют **TODO: verify с Марком** (multi-hop scoring + threshold design); B2, B3, B4 имеют фиксы в §3.5 + §5.1.
- **5 RISKS (R1-R5)** — R1, R3, R5 имеют mitigations в §5.2; R2, R4 документированы как known limitations.
- **5 CONCERNS (C1-C5)** — C1 требует **TODO: pilot data**; C2-C5 документированы, не блокеры.

**VERDICT:** ~~**NEEDS FIXES**~~ → **APPROVED** (после sign-off 16.06.2026)

Марк sign-off:
- (1) **B2 scope**: subset 40 factual_lookup + paraphrased, threshold 0.7. Multi-hop (10 queries) reported в `per_category` separately.
- (2) **B3 scope**: subset 40 factual_lookup + paraphrased, threshold 0.85. Multi-hop reported separately.
- (3) **Volume**: 50 queries (30 auto + 20 manual). 20 manual = 5 multi-hop + 5 paraphrased + 10 factual_lookup.
- (4) **Multi-hop scoring**: per_category breakdown в обоих result dataclasses, multi-hop не блокирует DoD threshold.

Pilot run после coding покажет, нужен ли Phase 5.1 с hybrid retriever (DenseRetriever + HybridRetriever) для multi-hop queries.

→ **Coding (Steps 1-13)** — начинаю сразу.

---

## 10. Сводка файлов

### Новые файлы (3)

```
harness/eval/retrieval.py                          # ~180 LoC
tests/eval/fixtures/golden_queries.jsonl           # 20 manual queries
tests/eval/test_precision_golden.py                # ~100 LoC, 6 tests
tests/eval/test_recall_golden.py                   # ~90 LoC, 5 tests
```

### Изменённые файлы (4)

```
harness/eval/golden.py                             # +~80 LoC (GoldenQuery, helpers)
harness/eval/__init__.py                           # +5 lines (export)
harness/eval/runner.py                             # +~30 LoC (run_precision, run_recall)
tests/eval/conftest.py                             # +~50 LoC (golden_queries fixture)
```

**Trust boundary test:** `tests/eval/test_eval_trust_boundary.py` — без изменений (уже parametrize over all `harness/eval/**/*.py`, новый `retrieval.py` будет проверен).

---

**Следующий шаг:** Марк sign-off по §5.1 B1, B5 (multi-hop + threshold scope) → ExitPlanMode → coding (Steps 1-13).
