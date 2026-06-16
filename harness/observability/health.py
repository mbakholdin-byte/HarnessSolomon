"""Phase 4.1: HealthChecker — liveness / readiness / deep probes.

Three health endpoints, mirror Kubernetes liveness/readiness pattern:
    - ``/health/live`` — process up, returns 200 always.
    - ``/health/ready`` — dependencies (Qdrant/SQLite/Neo4j) reachable.
    - ``/health/deep`` — full diagnostics (queue depth, hook registry, etc.).

Backward compat: ``/api/health`` (Phase 0) returns ``{status, version, project_root}``
as an alias for ``/health/deep?minimal=true``.

Trust boundary: stdlib + asyncio only. Probes are injected via
``app_state`` (DI), not imported from ``harness.agents`` / ``harness.server``
(Plan B1 mirror).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

logger = logging.getLogger(__name__)

HealthStatus = Literal["ok", "degraded", "unhealthy"]


@dataclass(frozen=True)
class HealthReport:
    """Result of a health check.

    Attributes:
        status: Overall status (``ok`` / ``degraded`` / ``unhealthy``).
        checks: Per-probe results, keyed by probe name.
        version: Harness version (set by caller).
        project_root: Project root path (set by caller).
    """

    status: HealthStatus
    checks: dict[str, dict[str, Any]] = field(default_factory=dict)
    version: str = ""
    project_root: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "version": self.version,
            "project_root": self.project_root,
            "checks": self.checks,
        }


# Probe = async callable that returns (status_dict, ok_bool).
Probe = Callable[[], Awaitable[tuple[dict[str, Any], bool]]]


class HealthChecker:
    """Aggregates health probes for /health/{live,ready,deep}.

    Probes are DI'd via ``register_probe(name, probe)``. This module
    does NOT import Qdrant / Neo4j / SQLite directly — that's the
    caller's job (B1 mirror).
    """

    def __init__(
        self,
        version: str = "1.7.0",
        project_root: str = "",
    ) -> None:
        self._version = version
        self._project_root = project_root
        self._probes: dict[str, Probe] = {}
        self._ready_timeout_s: float = 2.0
        self._deep_timeout_s: float = 5.0
        self._require_qdrant: bool = False
        self._require_neo4j: bool = False

    # === Configuration ===
    def configure(
        self,
        *,
        ready_timeout_s: float = 2.0,
        deep_timeout_s: float = 5.0,
        require_qdrant: bool = False,
        require_neo4j: bool = False,
    ) -> None:
        """Set probe timeouts and required-probe policy."""
        self._ready_timeout_s = ready_timeout_s
        self._deep_timeout_s = deep_timeout_s
        self._require_qdrant = require_qdrant
        self._require_neo4j = require_neo4j

    def register_probe(self, name: str, probe: Probe) -> None:
        """Register a probe by name. Replaces if already registered."""
        self._probes[name] = probe

    def unregister_probe(self, name: str) -> bool:
        return self._probes.pop(name, None) is not None

    # === Endpoints ===
    async def liveness(self) -> HealthReport:
        """Liveness = process is up. Always 200 if we get here."""
        return HealthReport(
            status="ok",
            version=self._version,
            project_root=self._project_root,
            checks={"process": {"status": "ok"}},
        )

    async def readiness(self) -> HealthReport:
        """Readiness = critical dependencies reachable.

        Returns ``unhealthy`` if any probe in ``_require_*`` fails.
        Returns ``degraded`` if a non-required probe fails.
        Returns ``ok`` if all pass.
        """
        results = await self._run_all_probes(self._ready_timeout_s)
        return self._aggregate(results, require_mode=True)

    async def deep(self) -> HealthReport:
        """Deep = full diagnostics, all probes, longer timeout."""
        results = await self._run_all_probes(self._deep_timeout_s)
        return self._aggregate(results, require_mode=False)

    # === Internal ===
    async def _run_all_probes(self, timeout_s: float) -> dict[str, tuple[dict[str, Any], bool]]:
        async def _run(name: str, probe: Probe) -> tuple[str, dict[str, Any], bool]:
            try:
                payload, ok = await asyncio.wait_for(probe(), timeout=timeout_s)
            except asyncio.TimeoutError:
                return (
                    name,
                    {"status": "timeout", "timeout_s": timeout_s},
                    False,
                )
            except Exception as e:  # noqa: BLE001
                return (
                    name,
                    {"status": "error", "error": f"{type(e).__name__}: {e}"},
                    False,
                )
            return name, payload, ok

        results_list = await asyncio.gather(
            *(_run(name, probe) for name, probe in self._probes.items()),
            return_exceptions=False,
        )
        return {name: (payload, ok) for name, payload, ok in results_list}

    def _aggregate(
        self,
        results: dict[str, tuple[dict[str, Any], bool]],
        require_mode: bool,
    ) -> HealthReport:
        checks_out: dict[str, dict[str, Any]] = {}
        required_failed: list[str] = []
        any_failed: list[str] = []
        for name, (payload, ok) in results.items():
            checks_out[name] = payload
            if not ok:
                any_failed.append(name)
                if (require_mode and name == "qdrant" and self._require_qdrant) or \
                   (require_mode and name == "neo4j" and self._require_neo4j):
                    required_failed.append(name)
        if required_failed:
            status: HealthStatus = "unhealthy"
        elif any_failed:
            status = "degraded"
        else:
            status = "ok"
        return HealthReport(
            status=status,
            checks=checks_out,
            version=self._version,
            project_root=self._project_root,
        )


__all__ = ["HealthChecker", "HealthReport", "HealthStatus"]
