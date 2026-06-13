# Каталог техник harness-инжиниринга

**Дата:** 12.06.2026
**Контекст:** техники для собственного harness поверх open-source LLM
**Источники:** Anthropic Engineering, LangChain/LangGraph, Letta, mem0, GraphRAG, Microsoft Research, open-source сообщество

---

## 1. Anthropic — фундаментальные паттерны

### 1.1 Building Effective Agents (Dec 2024)

**Workflows** (предопределённые пути) vs **Agents** (динамическое принятие решений).

#### Паттерн: Prompt Chaining
- **Описание:** последовательность промптов, где вывод одного идёт в следующий
- **Когда:** задача декомпозируется на фиксированные шаги
- **Пример:** outline → expand sections → fact-check → format
- **Реализация:** последовательные LLM-вызовы в Python

#### Паттерн: Routing
- **Описание:** LLM-классификатор решает, какого специалиста вызвать
- **Когда:** разные типы запросов требуют разной обработки
- **Пример:** тип запроса → customer-support / refund / technical
- **Реализация:** enum/router с явными ветками

#### Паттерн: Parallelization
- **Описание:** fan-out одной задачи на N параллельных, fan-in результатов
- **Когда:** задача секционируема (суммаризация, voting)
- **Пример:** 5 параллельных summarizer'ов → merge
- **Реализация:** `asyncio.gather` + merge prompt

#### Паттерн: Orchestrator-Workers
- **Описание:** центральный LLM декомпозирует, воркеры исполняют
- **Когда:** задача complex, шаги заранее неизвестны
- **Пример:** coding-задача → coder + tester + reviewer

#### Паттерн: Evaluator-Optimizer
- **Описание:** LLM-генератор + LLM-критик в цикле
- **Когда:** качество важнее скорости
- **Пример:** draft → critique → improve → re-critique до порога

### 1.2 Effective Context Engineering (Sep 2025)

**Главная идея:** контекст — конечный ресурс, его нужно **curate**, а не «запихнуть всё».

4 стратегии:

#### A. Write Context
- Агент **сам пишет** заметки в файлы/scratchpad
- Notes переживают compaction, цитируются в будущем
- **Пример:** plan.md, decisions.md, scratchpad.md

#### B. Select Context
- Pull контекста из памяти по запросу (RAG)
- Top-K, а не всё подряд
- **Пример:** mem0_search(query) → top-5 facts

#### C. Compress Context
- Сжатие через summarization, LLMLingua
- Hierarchical summary (L0/L1/L2)
- **Пример:** transcript → 1-страничный summary

#### D. Isolate Context
- Sub-agents с own context
- Multi-agent архитектуры (Claude Research)
- **Пример:** explorer subagent → main видит только summary

---

## 2. LangGraph — orchestration patterns

**Ключевая идея:** графы состояний для stateful-агентов.

### 2.1 State Machines
```
START → planner → [executor ↔ reviewer] → END
       ↑                  ↓
       └── feedback ←──┘
```

### 2.2 Conditional Edges
- LLM-decision → какой узел следующий
- Loop до convergence
- Early-exit по confidence

### 2.3 Human-in-the-Loop
- `interrupt_before` / `interrupt_after`
- Approval gates
- Edit-and-resume

### 2.4 Subgraphs
- Вложенные графы для иерархии
- Каждый subgraph = subagent

### 2.5 Persistence
- Checkpoint в SQLite/Postgres
- Resume после падения
- Time-travel debugging

---

## 3. Memory patterns

### 3.1 Hot/Cold Tiers

| Tier | Хранилище | Что там | Latency |
|------|-----------|---------|---------|
| Hot (контекст) | system prompt | последние 10-20 facts | 0ms |
| Warm (vector) | Qdrant, mem0 | семантика, retrieval | 50-200ms |
| Cold (file) | Obsidian, scratchpad | длинные документы | 1-5s |
| Frozen (graph) | Neo4j | отношения, multi-hop | 100-500ms |

### 3.2 Dual-Write
```
write_fact(fact):
    edit(notes/facts.md, fact)        # human-readable
    mem0.add(fact)                    # semantic
    hmem_add(fact, prefix='D:')       # hierarchical
    qdrant.upsert(fact.embed())       # vector
```

### 3.3 Reflection Loop (end of session)
```
on session end:
    lessons = llm.extract_lessons(transcript)
    for lesson in lessons:
        dual_write(lesson, layer='L:', confidence=0.8)
```

### 3.4 Consolidation (daily cron)
```
daily at 03:00:
    old_episodes = mem0.list(older_than=30days)
    summaries = llm.summarize_batch(old_episodes)
    hmem_add_batch(summaries, prefix='D:')
    mem0.delete(old_episodes)
```

### 3.5 Conflict Resolution
```
if cosine(a, b) > 0.92 and a.conflicts_with(b):
    if a.ts > b.ts:
        keep(a, log_conflict(b))
    else:
        # LLM judge
        verdict = llm_judge(a, b, context)
        keep(verdict.winner)
```

### 3.6 Provenance Schema
```python
class Memory(BaseModel):
    id: UUID
    content: str
    source: Literal['user', 'agent', 'auto', 'extracted']
    ts: datetime
    confidence: float
    session_id: Optional[str]
    model: Optional[str]
    context: Optional[Dict[str, Any]]
    ttl: Optional[int]
    links: List[UUID]
```

---

## 4. Sub-agent patterns

### 4.1 Worktree Isolation (CC-style)
```python
def run_subagent(task, agent_type):
    branch = f"agent/{uuid4()}"
    git_worktree_add(branch)
    try:
        result = execute_in_worktree(task, agent_type)
    finally:
        git_worktree_remove(branch)
    return result
```

### 4.2 Background + Progress
```python
async def run_background(task):
    handle = await start_subagent(task)
    while not handle.done:
        progress = await handle.progress
        emit_event('agent.progress', progress)
        await sleep(5)
    return await handle.result
```

### 4.3 Merge Queue + Reviewer
```python
def subagent_with_review(task, agent_type, reviewer_type):
    branch = subagent_run(task, agent_type)
    review = reviewer_run(branch, reviewer_type)
    if review.approved:
        merge_to_main(branch)
    else:
        return review.feedback
```

### 4.4 Adversarial Verify
```python
def verify_with_panel(claim, n=3):
    judges = [judge_run(claim) for _ in range(n)]
    votes = [j.verdict for j in judges]
    if votes.count('true') >= 2:
        return 'confirmed'
    return 'rejected'
```

---

## 5. Tool-use patterns для open-source LLM

### 5.1 JSON-Mode Enforcement
```python
import instructor
client = instructor.from_provider("ollama/qwen3:8b")
result = client.chat.completions.create(
    response_model=MySchema,
    messages=[...]
)
```

### 5.2 Retry-Loop with Feedback
```python
def call_with_validation(prompt, schema, max_retries=3):
    for i in range(max_retries):
        try:
            result = llm(prompt, format=schema)
            return schema.parse_raw(result)
        except ValidationError as e:
            prompt += f"\n\nПредыдущая попытка невалидна: {e}\nИсправь."
```

### 5.3 Tool Description as Few-Shot
```python
TOOL_DESC = """
Функция: search_wiki(query: str) -> list[Article]
Пример: search_wiki("PLAST регистрация") -> [Article1, Article2]
"""
```

### 5.4 Schema-in-Prompt
```python
SYSTEM = """
Отвечай строго в JSON:
{
  "action": "tool_name" | "final_answer",
  "tool_input": {...},
  "reasoning": "..."
}
"""
```

---

## 6. Compaction и контекст-менеджмент

### 6.1 Hierarchical Summarization
- L0: последние 10 turns (полный текст)
- L1: summary последних 50 turns
- L2: summary всей сессии
- L3: cross-session summary

### 6.2 Working Memory Offload
```
if len(tool_results) > 10:
    file = write_scratchpad(tool_results)
    context = context.replace(tool_results, f"[scratchpad:{file}]")
```

### 6.3 Pre-Compaction Hook
```python
@hook('PreCompact')
def save_state():
    save_to_file('napkin.md', current_decisions())
    save_to_file('todo.md', current_todo())
    hmem_add('Session checkpoint at turn N', prefix='D:')
```

### 6.4 LLMLingua Compression
- Сжимает промпт в 5-10x с минимальной потерей смысла
- Использует маленькую модель для compression

### 6.5 Attention Sinks (StreamLLM)
- Первые 4 токена + скользящее окно
- Поддерживает стабильное внимание на длинных контекстах

---

## 7. Hook patterns

### 7.1 PreToolUse Guardrail (CC-style)
```python
@hook('PreToolUse', matcher='Bash')
def block_dangerous(input):
    if 'rm -rf' in input['command']:
        return {'decision': 'block', 'reason': 'destructive'}
    if 'sudo' in input['command']:
        return {'decision': 'ask'}
```

### 7.2 PostToolUse Logging
```python
@hook('PostToolUse')
def log_all(input, output):
    log.write({
        'ts': now(),
        'tool': input['tool'],
        'input': input,
        'output': output[:500],
        'duration': input.get('_duration')
    })
```

### 7.3 Context Injection
```python
@hook('UserPromptSubmit')
def inject_relevant(input):
    relevant = mem0.search(input['prompt'], top_k=5)
    return {
        'additionalContext': f"\n\n## Из памяти:\n{relevant}"
    }
```

### 7.4 Auto-Compaction
```python
@hook('PreCompact')
def save_pre_compaction(input):
    hmem_add('Compaction triggered at turn N', prefix='M:')
    save_session_checkpoint()
```

---

## 8. Production hardening

### 8.1 Structured Output Everywhere
- Pydantic models для всех tool inputs/outputs
- JSON-schema validation на каждом LLM-вызове
- Reject invalid, retry с error feedback

### 8.2 Idempotency
- Каждый tool-call имеет `idempotency_key`
- Повторный вызов возвращает cached result
- Важно для retry-логик

### 8.3 Rate Limiting
- Per-provider: requests/sec, tokens/min
- Per-model: разные лимиты
- Exponential backoff на 429

### 8.4 Cost Tracking
```python
class CostRecord:
    ts: datetime
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    task_id: str
    agent_id: str
```

### 8.5 Health Checks
- Liveness: процесс отвечает
- Readiness: MCP servers подключены, models загружены
- Deep: retrieval работает, память доступна

---

## 9. Eval patterns

### 9.1 SWE-bench Style
- Набор задач с эталонным решением
- Прогон агента, сравнение через diff
- Per-task metrics: pass/fail, time, cost

### 9.2 A/B Test Models
- Один и тот же task на разных моделях
- Сравнение quality, cost, latency
- Pareto charts

### 9.3 Regression Detection
- Прогон стандартного набора после каждого изменения harness
- Алерт на деградацию >5%

### 9.4 Human Eval Loop
- 20 случайных задач в неделю → ручная оценка
- Calibration автоматических метрик

---

## 10. Hot-reload и dev experience

### 10.1 File Watcher для Skills
```python
@watch('.claude/skills/')
def on_skill_change(path):
    reload_skill(path)
    log('hot-reloaded', path)
```

### 10.2 Live Reload Config
```python
@watch('settings.json')
def on_config_change(path):
    reload_permissions()
    reload_hooks()
    reload_routes()
```

### 10.3 Dev Mode с Debugging
- `harness --dev` → verbose logs, trace export
- `--profile` → per-call timing
- `--mock-llm` → детерминистические ответы для тестов

---

## Приложение: библиотеки и инструменты

| Категория | Инструмент | Назначение |
|-----------|-----------|-----------|
| LLM routing | LiteLLM, OpenRouter, AI SDK | Multi-provider |
| Tool schema | Pydantic, Zod, instructor | Typed outputs |
| Vector DB | Qdrant, Weaviate, Milvus | Semantic search |
| Graph DB | Neo4j, Memgraph, Kuzu | KG-RAG |
| Memory | mem0, Letta, LangMem, Zep | Long-term memory |
| Rerank | BGE-reranker-v2-m3, Cohere | Cross-encoder |
| Embeddings | BGE-M3, E5, FRIDA, OpenAI, **fastembed (ONNX)** | Multilingual |
| Eval | SWE-bench, promptfoo, RAGAS | Quality metrics |
| Tracing | LangSmith, Phoenix, OpenLLMetry | Observability |
| Orchestration | LangGraph, Temporal, Inngest | Workflows |
| File watching | watchfiles, chokidar | Hot-reload |
| CLI | Textual, Rich, Ink | TUI |
| Sandbox | Docker, Firecracker, gVisor | Isolation |
| **Scope-gated API** | **env-tokens + server-side scope check** | **Permissions per token, 403 on miss** |
| **Capabilities discovery** | **`GET /capabilities` endpoint** | **Клиент сначала узнаёт, что разрешено** |

---

## 11. Scope-gated API (из Odysseus, 13.06.2026)

**Источник:** pewdiepie-archdaemon/odysseus (`integrations/claude/`)
**Применение:** интеграция нашего harness с другими оболочками или MCP-обёртками с разными правами

```python
# Серверная сторона
@app.post("/api/codex/todos")
async def add_todo(req: Request, body: TodoCreate):
    token = req.headers.get("Authorization", "").replace("Bearer ", "")
    scopes = token_store.get_scopes(token)
    if "todos.write" not in scopes:
        raise HTTPException(403, "Token lacks todos.write scope")
    return db.todos.add(body.title)
```

```python
# Клиентская сторона — subcommand helper
python3 odysseus_api.py todos list
python3 odysseus_api.py emails read UID
```

**Ключевые идеи**:
- **Env-tokens**, не query-string (безопаснее)
- **Server-side scope check** — клиент не может «обойти» через прямой вызов
- **403 = intentional restriction**, не баг
- **`GET /capabilities`** — клиент узнаёт доступные surface **до** первого вызова

## 12. Capabilities discovery (из Odysseus, 13.06.2026)

**Источник:** pewdiepie-archdaemon/odysseus (`integrations/claude/SKILL.md`)
**Применение:** skill-bundle'ы для агентов с явным списком разрешённых tools

```python
# Сервер
@app.get("/api/codex/capabilities")
async def capabilities(token: str = Depends(auth)):
    return {
        "todos.read": True,
        "todos.write": True,
        "email.read": token.scopes.get("email.read", False),
        "memory.read": True,
        "memory.write": token.scopes.get("memory.write", False),
    }
```

```bash
# Клиент (агент)
python3 odysseus_api.py capabilities
# {"todos.read": true, "email.read": false, ...}

# Агент НЕ пытается вызвать email.read — это предотвращает prompt-injection
```

**Защита от**: prompt-injection в user-editable skills/notes/documents → модель не может «угадать» scope, сервер сам объявляет capabilities

## 13. ONNX embeddings (из Odysseus, 13.06.2026)

**Источник:** pewdiepie-archdaemon/odysseus (`mcp_servers/memory_server.py`)
**Применение:** hardening для embeddings — работает без Ollama-сервиса

```python
from fastembed import TextEmbedding

# ONNX-based, локальный, без GPU
model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
embeddings = list(model.embed(["text 1", "text 2"]))
# embeddings[0] -> 384-dim vector
```

**Преимущества**:
- **Без Ollama** — для hardening (если Ollama упал)
- **Быстро** на CPU (ONNX runtime)
- **Multilingual** (bge-m3 поддерживает русский)

**Где применимо**:
- Fallback для `solomon-mem0` если `MEM0_OLLAMA_URL` недоступен
- CI/CD pipeline (без sidecar Ollama)

---

**Итог:** harness-инжиниринг — это **композиция** проверенных паттернов. Главное — не изобретать велосипед, а правильно сочетать. Anthropic, LangChain, Letta, mem0, **Odysseus** уже сделали 80% работы; наша задача — взять лучшее и заточить под open-source LLM + RU-first + собственный стек.
