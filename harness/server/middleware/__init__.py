"""Phase 4.1+ Step 6.2 / Phase 4.12 v1.22.0: HTTP middleware package.

Re-exports the installer functions for backwards compatibility with
the pre-v1.22.0 single-file ``harness/server/middleware.py`` module.

Public API:
    install_observability_middleware  — HTTP request metrics (Phase 4.1)
    install_legacy_gone_middleware    — Legacy /api/* → 410 Gone (Phase 4.12)

Submodules:
    observability  — ObservabilityMiddleware + installer
    legacy_gone    — LegacyApisGoneMiddleware + installer
"""
from __future__ import annotations

from harness.server.middleware.legacy_gone import (
    LegacyApisGoneMiddleware,
    install_legacy_gone_middleware,
)
from harness.server.middleware.observability import (
    ObservabilityMiddleware,
    install_observability_middleware,
)

__all__ = [
    "LegacyApisGoneMiddleware",
    "ObservabilityMiddleware",
    "install_legacy_gone_middleware",
    "install_observability_middleware",
]
