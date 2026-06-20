"""WI-04: MetricsCollector — background task that polls metrics + health.

Publishes to a :class:`MetricsBroker` at a configurable interval.
Uses existing ``PrometheusMetrics.snapshot()`` and ``HealthChecker``
when available.

Trust boundary: this module imports from harness.observability (metrics,
health) and the broker itself. No imports from harness.agents or
harness.server.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from harness.config import settings

logger = logging.getLogger(__name__)


async def start_metrics_collector(
    broker: Any,  # MetricsBroker — duck-typed to avoid import loop
    *,
    interval_s: float = 1.0,
    health_checker: Any = None,  # HealthChecker — duck-typed
    metrics_obj: Any = None,  # PrometheusMetrics — duck-typed
) -> asyncio.Task[None]:
    """Start the background metrics collection loop.

    Publishes two message types to the broker every ``interval_s``:

    * ``{type: "metrics", data: {...}}`` — PrometheusMetrics snapshot.
    * ``{type: "health", data: {...}}`` — HealthChecker.liveness() report.

    Args:
        broker: :class:`MetricsBroker` instance (or duck-typed equivalent).
        interval_s: Seconds between collection cycles. Default from settings.
        health_checker: Optional :class:`HealthChecker` for liveness.
        metrics_obj: Optional :class:`PrometheusMetrics` for snapshot.

    Returns:
        The asyncio Task running the loop (for cancellation at shutdown).

    The returned task runs forever until cancelled. Failures in a single
    cycle are logged and skipped (fail-open — a single bad snapshot should
    not stop the collector).
    """
    async def _loop() -> None:
        logger.info(
            "metrics_collector: started (interval=%.1fs)", interval_s,
        )
        cycle = 0
        while True:
            try:
                # --- metrics snapshot ---
                if metrics_obj is not None and hasattr(metrics_obj, "snapshot"):
                    snapshot = metrics_obj.snapshot()
                    if snapshot:
                        await broker.publish("metrics", {
                            "snapshot": snapshot,
                            "cycle": cycle,
                        })

                # --- health liveness ---
                if health_checker is not None and hasattr(health_checker, "liveness"):
                    report = await health_checker.liveness()
                    await broker.publish("health", {
                        "report": report.to_dict() if hasattr(report, "to_dict") else report,
                        "cycle": cycle,
                    })

                cycle += 1
            except asyncio.CancelledError:
                logger.info("metrics_collector: cancelled after %d cycles", cycle)
                return
            except Exception:  # noqa: BLE001 — fail-open
                logger.exception(
                    "metrics_collector: error in cycle %d — skipping", cycle,
                )
                cycle += 1

            await asyncio.sleep(interval_s)

    task = asyncio.create_task(_loop())
    return task


async def _get_settings_interval() -> float:
    """Read the collector interval from settings (safe default)."""
    try:
        val = getattr(settings, "ws_metrics_interval_s", 1.0)
        return float(val)
    except (TypeError, ValueError):
        return 1.0


# Convenience: start with settings-derived defaults.
async def start_default_collector(broker: Any) -> asyncio.Task[None]:
    """Start the collector with settings-derived defaults.

    Pulls the health checker and metrics singleton from
    :mod:`harness.observability` (``get_observability()``).
    """
    from harness.observability import get_observability

    obs = get_observability()
    return await start_metrics_collector(
        broker=broker,
        interval_s=await _get_settings_interval(),
        health_checker=obs.health,
        metrics_obj=obs.metrics,
    )


__all__ = ["start_metrics_collector", "start_default_collector"]
