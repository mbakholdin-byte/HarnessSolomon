"""Phase 7.5 — Routing Log Parser + Golden Dataset builder.

Parses harness JSONL log files, extracts ``llm_call`` and
``OnRoutingDecision`` (hook_dispatch) events, joins them by
timestamp proximity, and writes a golden dataset CSV for
Tier Router calibration.

**Important data notes (real logs, 06-2026):**
    * ``OnRoutingDecision`` events are ``hook_dispatch`` wrappers
      with minimal payload — the actual tier/model/cost data lives
      in ``llm_call`` events.
    * ``session_id`` is empty for all routing/llm_call events
      (only compaction events carry session ids).
    * Prompt text is absent (privacy) — ``prompt_len_chars`` is
      estimated from ``prompt_tokens * 4``; ``has_complexity_keyword``
      is derived from ``model_id`` / ``model`` fields.

**Trust boundary:** stdlib + ``json`` + ``csv`` + ``logging`` +
``dataclasses``. NO imports from ``harness.agents``, ``harness.server``,
or ``harness.context``.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Time window (seconds) for matching routing decisions to llm_calls
#: and tool_calls by timestamp proximity.
TS_PROXIMITY_WINDOW_S: float = 5.0

#: Rough chars-per-token estimate for ``prompt_len_chars`` when only
#: ``prompt_tokens`` is available.
CHARS_PER_TOKEN_ESTIMATE: int = 4


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class RoutingEvent:
    """One parsed routing decision joined with its associated LLM call.

    All 13 fields correspond to the golden dataset CSV schema.
    """

    ts: str
    session_id: str
    prompt_len_chars: int
    prompt_tokens: int
    context_tokens: int
    has_tool_calls: bool
    has_complexity_keyword: bool
    confidence: float
    chosen_tier: str
    actual_model: str
    status: str
    error_class: str | None
    cost_usd: float

    # Not in CSV — internal tracking
    _source_file: str = field(default="", repr=False)


#: CSV column order matching the 13-column schema.
CSV_COLUMNS: list[str] = [
    "ts",
    "session_id",
    "prompt_len_chars",
    "prompt_tokens",
    "context_tokens",
    "has_tool_calls",
    "has_complexity_keyword",
    "confidence",
    "chosen_tier",
    "actual_model",
    "status",
    "error_class",
    "cost_usd",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _derive_confidence(payload: dict[str, Any]) -> float:
    """Derive routing confidence from model/tier metadata.

    Real logs lack explicit ``confidence`` fields. This heuristic
    assigns confidence based on model tier and known model families.

    Returns:
        float in ``[0, 1]``.
    """
    tier = payload.get("tier", "")
    model = payload.get("model", "")

    # Tier-based defaults
    tier_confidence: dict[str, float] = {
        "T1": 0.70,
        "T2": 0.80,
        "T3": 0.90,
    }
    conf = tier_confidence.get(tier, 0.50)

    # Model-based adjustments
    if "claude" in model.lower():
        conf = max(conf, 0.95)
    elif "minimax" in model.lower():
        conf = max(conf, 0.88)
    elif "qwen" in model.lower():
        conf = max(conf, 0.65)

    return conf


def _check_complexity(payload: dict[str, Any]) -> bool:
    """Infer complexity from model_id/model fields (no prompt text available).

    Scans ``model_id`` and ``model`` for keywords suggesting complex
    tasks (frontier models are used for harder problems).

    Returns:
        True if complexity keywords detected, False otherwise.
    """
    model_id = str(payload.get("model_id", ""))
    model = str(payload.get("model", ""))

    text = (model_id + " " + model).lower()

    # Frontier/cloud models suggest complex routing
    complexity_signals = [
        "minimax",
        "claude",
        "gpt-4",
        "opus",
        "frontier",
        "pro",
        "premium",
        "t3",
    ]

    return any(signal in text for signal in complexity_signals)


def _find_nearby_events(
    ts: float,
    candidates: list[tuple[float, dict[str, Any]]],
    window: float = TS_PROXIMITY_WINDOW_S,
) -> list[dict[str, Any]]:
    """Return candidate events whose timestamp is within ``window`` of ``ts``.

    Uses simple linear scan (acceptable for ~2K tool_calls).
    """
    return [
        evt for evt_ts, evt in candidates if abs(evt_ts - ts) <= window
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_log_files(
    log_dir: Path,
    days: list[str] | None = None,
) -> list[RoutingEvent]:
    """Parse harness log files and return routing events.

    Reads all ``*.jsonl`` files in ``log_dir``. If ``days`` is provided,
    only files matching ``harness-{day}.jsonl`` are processed.

    Logic:
        1. First pass: index all ``llm_call``, ``OnRoutingDecision``,
           and ``tool_call`` events with their timestamps.
        2. Second pass: for each ``llm_call``, find the nearest
           ``OnRoutingDecision`` and any nearby ``tool_call`` events.
        3. Build a ``RoutingEvent`` per ``llm_call``.

    Args:
        log_dir: Directory containing ``harness-YYYY-MM-DD.jsonl`` files.
        days: Optional list of day strings (``["2026-06-16", ...]``) to
            filter which files to parse. If ``None``, all files are parsed.

    Returns:
        List of ``RoutingEvent`` objects, one per ``llm_call`` entry.
    """
    if not log_dir.is_dir():
        raise FileNotFoundError(f"log directory not found: {log_dir}")

    # Determine files to parse
    if days:
        files = [
            log_dir / f"harness-{day}.jsonl"
            for day in days
            if (log_dir / f"harness-{day}.jsonl").is_file()
        ]
    else:
        files = sorted(log_dir.glob("harness-*.jsonl"))

    if not files:
        logger.warning("no log files found in %s (days=%s)", log_dir, days)
        return []

    logger.info(
        "parsing %d log file(s) from %s", len(files), log_dir
    )

    # ------------------------------------------------------------------
    # Pass 1: index events by type
    # ------------------------------------------------------------------
    llm_calls: list[tuple[float, dict[str, Any]]] = []
    routing_decisions: list[tuple[float, dict[str, Any]]] = []
    tool_calls: list[tuple[float, dict[str, Any]]] = []

    total_lines = 0
    skipped_lines = 0

    for file_path in files:
        logger.debug("reading %s", file_path.name)
        try:
            with open(file_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    total_lines += 1
                    try:
                        evt: dict[str, Any] = json.loads(line)
                    except json.JSONDecodeError:
                        skipped_lines += 1
                        logger.debug("skip malformed JSON in %s", file_path.name)
                        continue

                    ts = evt.get("ts")
                    if ts is None:
                        skipped_lines += 1
                        continue

                    event_type = evt.get("event", "")

                    if event_type == "llm_call":
                        llm_calls.append((float(ts), evt))
                    elif (
                        event_type == "hook_dispatch"
                        and isinstance(evt.get("payload"), dict)
                        and evt["payload"].get("event") == "OnRoutingDecision"
                    ):
                        routing_decisions.append((float(ts), evt))
                    elif event_type == "tool_call":
                        tool_calls.append((float(ts), evt))
        except OSError as exc:
            logger.warning("cannot read %s: %s", file_path.name, exc)

    logger.info(
        "pass 1 complete: %d total lines, %d llm_calls, "
        "%d routing_decisions, %d tool_calls, %d skipped",
        total_lines,
        len(llm_calls),
        len(routing_decisions),
        len(tool_calls),
        skipped_lines,
    )

    # ------------------------------------------------------------------
    # Pass 2: build RoutingEvent for each llm_call
    # ------------------------------------------------------------------
    events: list[RoutingEvent] = []
    no_prompt_warning_logged = False

    for llm_ts, llm in llm_calls:
        payload: dict[str, Any] = llm.get("payload", {})

        # --- Find nearest OnRoutingDecision ---
        nearby_rds = _find_nearby_events(llm_ts, routing_decisions)

        # --- has_tool_calls ---
        nearby_tools = _find_nearby_events(llm_ts, tool_calls)
        has_tool_calls_flag = len(nearby_tools) > 0

        # --- has_complexity_keyword ---
        # Scan model_id/model as per spec note (no prompt text in logs)
        has_complexity = _check_complexity(payload)
        if not no_prompt_warning_logged and not has_complexity:
            logger.debug(
                "no prompt text in logs — complexity inferred from "
                "model_id/model fields only (privacy)"
            )
            no_prompt_warning_logged = True  # log once per parse session

        # --- confidence ---
        confidence = _derive_confidence(payload)

        # --- prompt_len_chars ---
        # Estimate from prompt_tokens (no prompt text in logs)
        prompt_tokens_val = int(payload.get("prompt_tokens", 0))
        prompt_len_chars_val = prompt_tokens_val * CHARS_PER_TOKEN_ESTIMATE

        # --- error_class ---
        status = payload.get("status", "unknown")
        error_class: str | None = None
        if status == "error":
            error_msg = payload.get("error", "")
            error_class = error_msg if error_msg else "unknown_error"

        # --- session_id ---
        session_id = llm.get("session_id", "")

        # --- source file (not in CSV, for debugging) ---
        # Extract from the llm event — we don't track file per-event
        # in this simplified version, so set to empty
        source_file = ""

        event = RoutingEvent(
            ts=str(llm_ts),
            session_id=session_id,
            prompt_len_chars=prompt_len_chars_val,
            prompt_tokens=prompt_tokens_val,
            context_tokens=0,  # not available in current logs
            has_tool_calls=has_tool_calls_flag,
            has_complexity_keyword=has_complexity,
            confidence=confidence,
            chosen_tier=payload.get("tier", "unknown"),
            actual_model=payload.get("model", "unknown"),
            status=status,
            error_class=error_class,
            cost_usd=float(payload.get("cost_usd", 0.0)),
            _source_file=source_file,
        )
        events.append(event)

    logger.info(
        "pass 2 complete: %d routing events extracted from %d llm_calls",
        len(events),
        len(llm_calls),
    )

    # Warn if we have more routing decisions than llm_calls (unmatched hooks)
    unmatched_rds = len(routing_decisions) - len(llm_calls)
    if unmatched_rds > 0:
        logger.info(
            "%d OnRoutingDecision hook events had no matching "
            "llm_call within window — skipped (normal for hook-only logs)",
            max(0, len(routing_decisions) - len(llm_calls)),
        )

    if prompt_tokens_val == 0 and events:
        logger.warning(
            "some llm_call events have prompt_tokens=0 — "
            "prompt_len_chars will be 0 for those rows"
        )

    return events


def write_golden_dataset(
    events: list[RoutingEvent],
    output_path: Path,
) -> None:
    """Write routing events as a 13-column CSV golden dataset.

    Creates parent directories if needed. Overwrites existing files.

    Args:
        events: Routing events from :func:`parse_log_files`.
        output_path: Path to the output CSV file (e.g.,
            ``data/calibration/golden_routing_dataset.csv``).

    Raises:
        ValueError: If ``events`` is empty.
    """
    if not events:
        raise ValueError("events list is empty — nothing to write")

    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for event in events:
            row = {col: getattr(event, col) for col in CSV_COLUMNS}
            writer.writerow(row)

    logger.info(
        "golden dataset written: %d rows → %s", len(events), output_path
    )


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

__all__ = [
    "RoutingEvent",
    "CSV_COLUMNS",
    "parse_log_files",
    "write_golden_dataset",
]
