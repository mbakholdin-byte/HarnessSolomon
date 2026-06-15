"""Phase 3 v1.5.0: Privacy zones (path-based).

Single source of truth для glob-семантики (``match_glob``) живёт в
:mod:`harness.privacy.path_match` и переиспользуется:

* :mod:`harness.agents.pr_templating` — для CODEOWNERS pattern matching
  (изначально жил в ``_match_codeowners_pattern`` в ``pr_templating.py``,
  извлечён в v1.5.0 Step 1 для устранения дрейфа glob-семантики).
* :mod:`harness.privacy.zone_filter` — для PrivacyZoneFilter в v1.5.0.

Принцип: ``PrivacyZoneFilter`` и ``parse_codeowners_for_diff`` должны
использовать одну и ту же ``match_glob`` функцию. Это устраняет риск
дрейфа (когда одни и те же glob-патерны по-разному интерпретируются в
двух местах) и снижает surface area тестов.

Public API:
    match_glob(path: str, pattern: str) -> bool
        Test one file path against one glob pattern.
    parse_zones(patterns_str: str, per_action_str: str, default_action: str) -> list[ZoneRule]
        Parse Settings strings into structured zone rules.
    PrivacyZoneFilter
        Path-based privacy filter with audit integration.
    ZoneRule
        Frozen dataclass: (pattern, action).
"""
from __future__ import annotations

from harness.privacy.path_match import match_glob
# Phase 3 v1.5.0 Step 2 will add:
#     from harness.privacy.zone_config import ZoneRule, parse_zones
#     from harness.privacy.zone_filter import PrivacyZoneFilter

__all__ = [
    "match_glob",
]
