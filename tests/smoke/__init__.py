"""Smoke tests package for Solomon Harness release candidates.

Phase 4.14B v1.0.0-rc1: integration smoke tests that exercise the
real production code paths end-to-end (no mocks of harness internals).
Skipped by default in the normal ``pytest`` run — invoke explicitly::

    pytest tests/smoke/test_v100_rc1.py -v -m smoke

All tests carry the ``@pytest.mark.smoke`` marker and are isolated
from one another (each builds its own app / store / fixtures).
"""
