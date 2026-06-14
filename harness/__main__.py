"""Solomon Harness — ``python -m harness`` entry point.

Forwards to :func:`harness.cli.main`, which dispatches to ``serve`` (default)
or the ``agents`` subcommand. See ``harness.cli`` for the full CLI surface.
"""
from __future__ import annotations

from harness.cli import main


if __name__ == "__main__":
    raise SystemExit(main())

