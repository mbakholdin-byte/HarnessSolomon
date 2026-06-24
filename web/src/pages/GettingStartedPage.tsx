import { useEffect, useState } from 'react';
import { APIError } from '../api/types';
import styles from './GettingStartedPage.module.css';

// ============== Types ==============
type Tier = 'T1' | 'T2' | 'T3' | 'T?';

interface ModelInfo {
  id: string;
  provider?: string;
  tier?: Tier;
  context?: number;
  available?: boolean;
  pricing_input?: number;
  pricing_output?: number;
  display_name?: string;
}

interface AgentInfo {
  name: string;
  perms: 'read-only' | 'full';
  tools: string;
  desc: string;
  isCustom?: boolean;
}

interface JobResult {
  // Actual API contract (Phase 2.x JobRecord schema):
  id?: string;
  status?: string;
  cost?: number;
  model?: string;
  worktree_id?: string;
  started_at?: string;
  finished_at?: string | null;
  prompt?: string;
  repo?: string | null;
  error?: string | null;
  // Future (Phase 2.x+ — not persisted yet):
  result_text?: string;
  iterations?: number;
  // Legacy / wizard UI fallbacks (UI-only, not in API):
  job_id?: string;
  cost_usd?: number;
  result?: string;
  final_text?: string;
  output?: string;
  text?: string;
  [k: string]: unknown;
}

const AUTH_TOKEN_KEY = 'auth_token';

// Built-in agents — same list as standalone wizard. Custom agents should
// be added via ``harness agents list`` once an API endpoint exists.
const BUILTIN_AGENTS: AgentInfo[] = [
  {
    name: 'explore',
    perms: 'read-only',
    tools: 'grep, read_file, glob',
    desc: 'Разведка кода. Только чтение.',
  },
  {
    name: 'plan',
    perms: 'read-only',
    tools: 'grep, read_file, glob',
    desc: 'Планирование изменений. Больше итераций (10).',
  },
  {
    name: 'code',
    perms: 'full',
    tools: 'read_file, write_file, edit_file, bash, grep, glob',
    desc: 'Пишет код в git worktree. Меняет файлы.',
  },
  {
    name: 'review',
    perms: 'read-only',
    tools: 'grep, read_file, glob',
    desc: 'Code review. Находит проблемы, не правит.',
  },
  {
    name: 'secretary',
    perms: 'read-only',
    tools: 'grep, read_file, glob',
    desc: 'Custom: помощник на русском, делает summary файлов.',
    isCustom: true,
  },
];

// ============== Helpers ==============
function classifyTier(m: ModelInfo): Tier {
  if (m.tier) return m.tier;
  const id = (m.id || '').toLowerCase();
  if (id.includes('qwen3:8b') || id.includes('local')) return 'T1';
  if (id.includes('glm') || id.includes('moonshot') || id.includes('kimi')) return 'T2';
  if (id.includes('minimax') || id.includes('opus') || id.includes('claude-3')) return 'T3';
  return 'T?';
}

function tierLabel(t: Tier): string {
  if (t === 'T1') return 'Локальная, бесплатно';
  if (t === 'T2') return 'Cloud mid-tier';
  if (t === 'T3') return 'Cloud premium';
  return '?';
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms} мс`;
  return `${(ms / 1000).toFixed(1)} сек`;
}

function formatCtx(n: number | undefined): string {
  if (!n) return '';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${Math.round(n / 1000)}K`;
  return String(n);
}

function formatPrice(p: number | undefined): string {
  if (p === undefined || p === null) return '';
  // pricing_input stored per-1M in catalog; show as $X.XX
  return `$${p.toFixed(2)}`;
}

// Direct fetch — bypasses the api singleton's 401-redirect behaviour, so the
// wizard can recover from 401 by showing a token input instead of forcing
// the user back to /login mid-onboarding.
async function directFetch<T>(
  path: string,
  opts: RequestInit = {},
): Promise<{ ok: boolean; status: number; data: T | null }> {
  const token = localStorage.getItem(AUTH_TOKEN_KEY);
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (token) headers['Authorization'] = `Bearer ${token}`;
  const res = await fetch(`/api/v1${path}`, { ...opts, headers: { ...headers, ...(opts.headers || {}) } });
  let data: T | null = null;
  try {
    data = (await res.json()) as T;
  } catch {
    data = null;
  }
  return { ok: res.ok, status: res.status, data };
}

// ============== Component ==============
type StepId = 1 | 2 | 3 | 4 | 5;

export function GettingStartedPage(): JSX.Element {
  // Wizard state
  const [step, setStep] = useState<StepId>(1);

  // Step 1 — connection
  const [tokenInput, setTokenInput] = useState<string>('');
  const [baseUrl, setBaseUrl] = useState<string>(window.location.origin);
  const [connectStatus, setConnectStatus] = useState<{
    kind: 'ok' | 'err' | 'warn' | null;
    text: string;
  }>({ kind: null, text: '' });

  // Step 2 — model
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [selectedModel, setSelectedModel] = useState<string>('');
  const [manualModel, setManualModel] = useState<string>('');

  // Step 3 — agent
  const [agents] = useState<AgentInfo[]>(BUILTIN_AGENTS);
  const [selectedAgent, setSelectedAgent] = useState<string>('');

  // Step 4 — prompt + run
  const [prompt, setPrompt] = useState<string>('');
  const [running, setRunning] = useState<boolean>(false);
  const [runStatus, setRunStatus] = useState<{
    kind: 'ok' | 'err' | 'warn' | null;
    text: string;
  }>({ kind: null, text: '' });

  // Step 5 — result
  const [result, setResult] = useState<JobResult | null>(null);
  const [runDuration, setRunDuration] = useState<number>(0);

  // Persist token input into localStorage so api singleton picks it up
  useEffect(() => {
    if (tokenInput) {
      localStorage.setItem(AUTH_TOKEN_KEY, tokenInput);
    }
  }, [tokenInput]);

  // ============== Step 1: Connection ==============
  async function handleConnect(): Promise<void> {
    setConnectStatus({ kind: 'warn', text: 'Подключаюсь к серверу...' });
    try {
      // /api/v1/health is public — no auth required
      const h = await directFetch<{ status: string; version?: string }>('/health');
      if (!h.ok) {
        setConnectStatus({
          kind: 'err',
          text: `Сервер недоступен (HTTP ${h.status}). Проверьте что harness serve запущен.`,
        });
        return;
      }

      // /api/v1/models may require auth — 401 means we need a token
      const m = await directFetch<ModelInfo[] | { models: ModelInfo[] }>('/models');
      if (!m.ok) {
        if (m.status === 401 || m.status === 403) {
          setConnectStatus({
            kind: 'err',
            text: `Сервер живой, но /api/v1/models вернул ${m.status}. Введите Bearer token ниже и нажмите "Проверить подключение" ещё раз.`,
          });
          return;
        }
        setConnectStatus({
          kind: 'err',
          text: `Ошибка /api/v1/models: HTTP ${m.status}`,
        });
        return;
      }

      const list = Array.isArray(m.data) ? m.data : m.data?.models ?? [];
      setModels(list);
      setConnectStatus({
        kind: 'ok',
        text: `Подключение успешно. Сервер: ${h.data?.version ?? '?'}. Доступно моделей: ${list.length}.`,
      });
      setStep(2);
    } catch (err) {
      setConnectStatus({
        kind: 'err',
        text: `Сетевая ошибка: ${err instanceof Error ? err.message : String(err)}`,
      });
    }
  }

  // ============== Step 4: Run ==============
  async function handleRun(): Promise<void> {
    if (!prompt.trim()) {
      setRunStatus({ kind: 'err', text: 'Напишите промпт.' });
      return;
    }
    setRunning(true);
    setRunStatus({ kind: 'warn', text: 'Запускаю агента...' });
    const t0 = Date.now();

    try {
      // Phase 2.x contract: field is `agent` (not `agent_name`); worktree is
      // decided by the spec's ``worktree_required``; ``background=false`` runs
      // synchronously and returns the result inline (no polling needed).
      const r = await directFetch<JobResult>('/agents/jobs', {
        method: 'POST',
        body: JSON.stringify({
          agent: selectedAgent,
          model: selectedModel || undefined,
          prompt,
          background: false,  // sync: result inline, no polling
          pr_mode: 'off',
        }),
      });

      if (!r.ok) {
        const detail = (r.data as { detail?: string })?.detail ?? `HTTP ${r.status}`;
        throw new APIError(r.status, r.data, detail);
      }

      const jobId = r.data?.id ?? r.data?.job_id;
      if (!jobId) {
        // Inline result — no job id means the response IS the result.
        setResult(r.data);
        setRunDuration(Date.now() - t0);
        setRunStatus({ kind: 'ok', text: 'Готово.' });
        setStep(5);
        return;
      }

      // background=false should return inline; if we got a job_id, the job
      // is still queued. Poll for completion (max 60 * 2s = 2 min).
      setRunStatus({ kind: 'warn', text: `Job ${jobId.slice(0, 8)}… в очереди. Жду завершения...` });
      let attempts = 0;
      while (attempts < 60) {
        await new Promise<void>((res) => setTimeout(res, 2000));
        attempts++;
        const poll = await directFetch<JobResult>(`/agents/jobs/${jobId}`);
        if (!poll.ok || !poll.data) continue;
        const st = poll.data.status ?? '';
        setRunStatus({
          kind: 'warn',
          text: `Job ${jobId.slice(0, 8)}… · статус: ${st} · прошло ${formatDuration(Date.now() - t0)}`,
        });
        if (['completed', 'failed', 'cancelled', 'done'].includes(st)) {
          setResult(poll.data);
          setRunDuration(Date.now() - t0);
          setRunStatus({ kind: 'ok', text: 'Готово.' });
          setStep(5);
          return;
        }
      }
      throw new Error('Timeout: job не завершился за 2 минуты. Проверьте /agents/jobs/' + jobId);
    } catch (err) {
      setRunStatus({
        kind: 'err',
        text: 'Ошибка запуска: ' + (err instanceof Error ? err.message : String(err)),
      });
    } finally {
      setRunning(false);
    }
  }

  function resetWizard(): void {
    setPrompt('');
    setResult(null);
    setRunDuration(0);
    setRunStatus({ kind: null, text: '' });
    setStep(4);
  }

  // ============== Render ==============
  return (
    <div className={styles.page} data-testid="getting-started-page">
      {/* Header */}
      <div className={styles.header}>
        <h1 className={styles.title}>Solomon Harness — Начало работы</h1>
        <p className={styles.subtitle}>Запусти своего агента за 4 шага</p>
      </div>

      {/* Progress */}
      <div className={styles.progress}>
        {([1, 2, 3, 4] as StepId[]).map((n) => {
          let cls = styles.step;
          if (step > n || (n === 4 && step === 5)) cls += ' ' + styles.done;
          else if (step === n) cls += ' ' + styles.active;
          return (
            <div key={n} className={cls} data-testid={`step-${n}`}>
              {n} · {n === 1 ? 'Подключение' : n === 2 ? 'Модель' : n === 3 ? 'Агент' : 'Задача'}
            </div>
          );
        })}
      </div>

      {/* STEP 1: Connection */}
      {step === 1 && (
        <div className={styles.card} data-testid="card-step-1">
          <h2 className={styles.cardTitle}>Шаг 1 · Подключение к серверу</h2>
          <p className={styles.cardSubtitle}>
            Проверяем что Harness запущен и доступен.
          </p>

          {connectStatus.kind && (
            <div className={`${styles.status} ${styles[`status${connectStatus.kind === 'ok' ? 'Ok' : connectStatus.kind === 'err' ? 'Err' : 'Warn'}`]}`}>
              <span className={styles.dot} />
              <span>{connectStatus.text}</span>
            </div>
          )}

          <label className={styles.label} htmlFor="gs-base">Адрес сервера</label>
          <input
            id="gs-base"
            className={styles.input}
            type="text"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
            placeholder="http://127.0.0.1:8765"
            data-testid="base-url"
          />

          <label className={styles.label} htmlFor="gs-token">Bearer token (если требуется)</label>
          <input
            id="gs-token"
            className={styles.input}
            type="password"
            value={tokenInput}
            onChange={(e) => setTokenInput(e.target.value)}
            placeholder="Введите токен, если auth_required=True в settings"
            data-testid="token-input"
          />
          <p className={styles.helpText}>
            Получить токен: в терминале из корня <code>06_Harness</code> выполните
          </p>
          <pre className={styles.codeBlock}>
            <code>
              harness auth create --label "my-token" --scopes "agents.read,agents.write,sessions.read,sessions.write,memory.read,memory.write,observability.read"
            </code>
          </pre>

          <div className={styles.btnRow}>
            <button
              className={styles.btn}
              onClick={handleConnect}
              data-testid="btn-connect"
            >
              Проверить подключение
            </button>
          </div>
        </div>
      )}

      {/* STEP 2: Model */}
      {step === 2 && (
        <div className={styles.card} data-testid="card-step-2">
          <h2 className={styles.cardTitle}>Шаг 2 · Выберите модель</h2>
          <p className={styles.cardSubtitle}>
            Какая LLM будет думать за вашего агента.
          </p>

          <div className={styles.choiceGrid} data-testid="model-grid">
            {models.map((m) => {
              const tier = classifyTier(m);
              const isSel = selectedModel === m.id;
              let cls = styles.choice;
              if (isSel) cls += ' ' + styles.choiceSelected;
              const ctxTxt = m.context ? ` · ctx=${formatCtx(m.context)}` : '';
              const priceTxt =
                m.pricing_input !== undefined && m.pricing_output !== undefined
                  ? ` · ${formatPrice(m.pricing_input)}/${formatPrice(m.pricing_output)} per 1M`
                  : '';
              const unavailable = m.available === false;
              return (
                <div
                  key={m.id}
                  className={cls}
                  onClick={() => {
                    setSelectedModel(m.id);
                    setManualModel(m.id);
                  }}
                  data-testid={`model-${m.id}`}
                  role="button"
                  tabIndex={0}
                >
                  <span
                    className={`${styles.badge} ${
                      unavailable
                        ? styles.badgeUnavailable
                        : tier === 'T1'
                          ? styles.badgeT1
                          : tier === 'T2'
                            ? styles.badgeT2
                            : styles.badgeT3
                    }`}
                  >
                    {tier}
                  </span>
                  <div className={styles.choiceTitle}>{m.display_name ?? m.id}</div>
                  <div className={styles.choiceId}>
                    {m.id}
                    {ctxTxt}
                  </div>
                  <div className={styles.choiceMeta}>
                    {tierLabel(tier)}
                    {m.provider ? ` · ${m.provider}` : ''}
                    {priceTxt}
                    {unavailable ? ' · ⚠️ недоступна (нужен ключ)' : ''}
                  </div>
                </div>
              );
            })}
          </div>

          <label className={styles.label} htmlFor="gs-manual">
            Или введите model id вручную
          </label>
          <input
            id="gs-manual"
            className={styles.input}
            type="text"
            value={manualModel}
            onChange={(e) => {
              setManualModel(e.target.value);
              setSelectedModel(e.target.value);
            }}
            placeholder="например minimax/MiniMax-M2.7"
            data-testid="manual-model"
          />

          <div className={styles.btnRow}>
            <button
              className={`${styles.btn} ${styles.btnSecondary}`}
              onClick={() => setStep(1)}
              data-testid="btn-back-2"
            >
              ← Назад
            </button>
            <button
              className={styles.btn}
              onClick={() => setStep(3)}
              disabled={!selectedModel.trim()}
              data-testid="btn-next-2"
            >
              Далее →
            </button>
          </div>
        </div>
      )}

      {/* STEP 3: Agent */}
      {step === 3 && (
        <div className={styles.card} data-testid="card-step-3">
          <h2 className={styles.cardTitle}>Шаг 3 · Выберите агента</h2>
          <p className={styles.cardSubtitle}>
            Агент — это роль + набор tools. Встроено 4 штуки.
          </p>

          <div className={styles.choiceGrid} data-testid="agent-grid">
            {agents.map((a) => {
              const isSel = selectedAgent === a.name;
              let cls = styles.choice;
              if (isSel) cls += ' ' + styles.choiceSelected;
              return (
                <div
                  key={a.name}
                  className={cls}
                  onClick={() => setSelectedAgent(a.name)}
                  data-testid={`agent-${a.name}`}
                  role="button"
                  tabIndex={0}
                >
                  <span
                    className={a.perms === 'read-only' ? styles.permReadOnly : styles.permFull}
                  >
                    {a.perms}
                  </span>
                  <div className={styles.choiceTitle}>{a.name}</div>
                  <div className={styles.choiceMeta}>{a.desc}</div>
                  <div className={styles.choiceMeta}>
                    <strong style={{ color: '#1976d2' }}>tools:</strong> {a.tools}
                  </div>
                </div>
              );
            })}
          </div>

          <details style={{ marginTop: 18 }}>
            <summary
              style={{ cursor: 'pointer', color: '#1976d2', fontWeight: 600 }}
              data-testid="custom-agent-details"
            >
              Хочу создать своего агента
            </summary>
            <div style={{ marginTop: 12, padding: 16, background: '#fafafa', borderRadius: 4 }}>
              <p style={{ marginTop: 0 }}>
                Создайте файл <code>C:/MyAI/.harness/agents/my-agent.md</code> с YAML frontmatter:
              </p>
              <pre className={styles.codeBlock}>
                <code>
{`---
name: my-agent
model: MiniMax-M2.7
tools: [read_file, grep, glob]
permissions: read-only
max_iterations: 8
worktree_required: false
---

You are my custom agent. Your job: ...`}
                </code>
              </pre>
              <p style={{ marginBottom: 0 }}>
                Затем в CLI: <code>harness reload</code>. Custom агент появится в{' '}
                <code>harness agents list</code>. В UI-списке выше он отобразится после
                обновления страницы.
              </p>
            </div>
          </details>

          <div className={styles.btnRow}>
            <button
              className={`${styles.btn} ${styles.btnSecondary}`}
              onClick={() => setStep(2)}
              data-testid="btn-back-3"
            >
              ← Назад
            </button>
            <button
              className={styles.btn}
              onClick={() => setStep(4)}
              disabled={!selectedAgent}
              data-testid="btn-next-3"
            >
              Далее →
            </button>
          </div>
        </div>
      )}

      {/* STEP 4: Prompt + Run */}
      {step === 4 && (
        <div className={styles.card} data-testid="card-step-4">
          <h2 className={styles.cardTitle}>Шаг 4 · Поставьте задачу</h2>
          <p className={styles.cardSubtitle}>Опишите что агент должен сделать.</p>

          <div className={styles.metaRow}>
            <div className={styles.metaItem}>
              <strong>Модель:</strong> {selectedModel || '—'}
            </div>
            <div className={styles.metaItem}>
              <strong>Агент:</strong> {selectedAgent || '—'}
            </div>
          </div>

          <label className={styles.label} htmlFor="gs-prompt">
            Prompt (на русском или английском)
          </label>
          <textarea
            id="gs-prompt"
            className={styles.textarea}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder={`Например:\nПрочитай README.md и сделай summary в 3 пункта.`}
            data-testid="prompt-textarea"
          />

          {runStatus.kind && (
            <div className={`${styles.status} ${styles[`status${runStatus.kind === 'ok' ? 'Ok' : runStatus.kind === 'err' ? 'Err' : 'Warn'}`]}`}>
              <span className={styles.dot} />
              <span>{runStatus.text}</span>
            </div>
          )}

          <div className={styles.btnRow}>
            <button
              className={`${styles.btn} ${styles.btnSecondary}`}
              onClick={() => setStep(3)}
              disabled={running}
              data-testid="btn-back-4"
            >
              ← Назад
            </button>
            <button
              className={styles.btn}
              onClick={handleRun}
              disabled={running}
              data-testid="btn-run"
            >
              {running ? (
                <>
                  <span className={styles.spinner} /> Запускаю...
                </>
              ) : (
                <>▶ Запустить</>
              )}
            </button>
          </div>
        </div>
      )}

      {/* STEP 5: Result */}
      {step === 5 && result && (
        <div className={styles.card} data-testid="card-step-5">
          <h2 className={styles.cardTitle}>✓ Готово — результат агента</h2>
          <p className={styles.cardSubtitle}>
            Вот что вернул <strong>{selectedAgent}</strong> за {formatDuration(runDuration)}.
          </p>

          <div className={styles.metaRow}>
            <div className={styles.metaItem}>
              <strong>status:</strong> {result.status ?? '—'}
            </div>
            <div className={styles.metaItem}>
              <strong>cost:</strong>{' '}
              {result.cost !== undefined ? `$${Number(result.cost).toFixed(4)}` : '—'}
            </div>
            {result.iterations !== undefined && (
              <div className={styles.metaItem}>
                <strong>iterations:</strong> {result.iterations}
              </div>
            )}
            {result.model && (
              <div className={styles.metaItem}>
                <strong>model:</strong> {result.model}
              </div>
            )}
            {(result.id ?? result.job_id) && (
              <div className={styles.metaItem}>
                <strong>job_id:</strong> {String(result.id ?? result.job_id).slice(0, 16)}…
              </div>
            )}
            {result.worktree_id && (
              <div className={styles.metaItem}>
                <strong>worktree:</strong> {result.worktree_id}
              </div>
            )}
            {result.started_at && (
              <div className={styles.metaItem}>
                <strong>started:</strong> {result.started_at}
              </div>
            )}
            {result.finished_at && (
              <div className={styles.metaItem}>
                <strong>finished:</strong> {result.finished_at}
              </div>
            )}
          </div>

          {result.error && (
            <div className={`${styles.status} ${styles.statusErr}`}>
              <span className={styles.dot} />
              <span>Error: {result.error}</span>
            </div>
          )}

          <label className={styles.label}>Ответ агента</label>
          <div className={styles.resultBox} data-testid="result-box">
            {result.result_text ??
              result.result ??
              result.final_text ??
              result.output ??
              result.text ??
              `Готово. Финальный ответ пока не сохраняется в JobRecord (Phase 2.x).\n\nJob ID: ${result.id ?? result.job_id ?? '—'}\nStatus: ${result.status ?? '—'}\n\nОткройте "Agents jobs" в меню для просмотра деталей и логов.`}
          </div>

          <div className={styles.btnRow}>
            <button
              className={styles.btn}
              onClick={resetWizard}
              data-testid="btn-restart"
            >
              ↻ Новая задача
            </button>
            <button
              className={`${styles.btn} ${styles.btnSecondary}`}
              onClick={() => {
                window.location.href = '/observability';
              }}
              data-testid="btn-open-jobs"
            >
              Открыть Observability ↗
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export default GettingStartedPage;
