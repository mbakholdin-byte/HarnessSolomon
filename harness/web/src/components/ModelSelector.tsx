import { useEffect, useState } from "react";
import { getModels, type ModelSpec } from "../api/client";

interface ModelSelectorProps {
  value: string;
  onChange: (modelId: string) => void;
  disabled?: boolean;
}

/**
 * Native <select> dropdown grouped by availability.
 * Loads the catalog once on mount; the parent owns the selected value.
 */
export function ModelSelector({
  value,
  onChange,
  disabled = false,
}: ModelSelectorProps): JSX.Element {
  const [models, setModels] = useState<ModelSpec[]>([]);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const m = await getModels();
        if (cancelled) return;
        setModels(m);
        if (!value) {
          const firstAvail = m.find((x) => x.available) ?? m[0];
          if (firstAvail) onChange(firstAvail.id);
        }
      } catch (e: unknown) {
        if (cancelled) return;
        setErr(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (err) {
    return (
      <div className="model-selector model-selector--err" title={err}>
        models: error
      </div>
    );
  }

  const available = models.filter((m) => m.available);
  const unavailable = models.filter((m) => !m.available);

  return (
    <select
      className="model-selector"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      disabled={disabled || models.length === 0}
      aria-label="Model"
    >
      {models.length === 0 && <option value="">(loading models…)</option>}
      {available.length > 0 && (
        <optgroup label="Available">
          {available.map((m) => (
            <option key={m.id} value={m.id}>
              {m.id} · {m.tier}
            </option>
          ))}
        </optgroup>
      )}
      {unavailable.length > 0 && (
        <optgroup label="Unavailable (no API key)">
          {unavailable.map((m) => (
            <option key={m.id} value={m.id} disabled>
              {m.id} · {m.tier}
            </option>
          ))}
        </optgroup>
      )}
    </select>
  );
}
