"""Solomon Harness — command-line entry point.

Usage:
    python -m harness                     # start the FastAPI server (default)
    harness serve [--host H] [--port P]   # same, explicit
    harness agents list                   # list built-in + overridden sub-agents (Step 2+)
    harness agents run <name> "..."       # run a sub-agent (Step 2+)

This module is the target of the ``harness`` and ``solomon-harness`` console
scripts declared in ``pyproject.toml``. Until Phase 2 Step 2 lands, only the
``serve`` subcommand is fully implemented; ``agents`` prints a help message.
"""
from __future__ import annotations

import argparse
import sys

import uvicorn

from harness.config import settings


def _cmd_serve(args: argparse.Namespace) -> int:
    """Start the FastAPI server (default behaviour of ``python -m harness``)."""
    uvicorn.run(
        "harness.server.app:app",
        host=args.host or settings.host,
        port=args.port or settings.port,
        log_level=settings.log_level.lower(),
        reload=False,
    )
    return 0


def _cmd_agents(_args: argparse.Namespace) -> int:
    """Placeholder for the sub-agent CLI (Step 2 lands this)."""
    print(
        "Sub-agent CLI is not implemented yet. "
        "It will arrive in Phase 2.0 Step 2 (built-in agents + registry).",
        file=sys.stderr,
    )
    print("For now, start the server with: python -m harness", file=sys.stderr)
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness",
        description="Solomon Harness — open-source agentic runtime for local & cloud LLMs.",
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Start the FastAPI server (default).")
    serve.add_argument("--host", default=settings.host, help="Bind host")
    serve.add_argument("--port", type=int, default=settings.port, help="Bind port")
    serve.set_defaults(func=_cmd_serve)

    agents = sub.add_parser("agents", help="Sub-agent management (Phase 2).")
    agents_sub = agents.add_subparsers(dest="agents_command")
    agents_sub.add_parser("list", help="List available sub-agents")
    run = agents_sub.add_parser("run", help="Run a sub-agent")
    run.add_argument("name", help="Sub-agent name")
    run.add_argument("prompt", help="Prompt text")
    run.add_argument(
        "--no-worktree",
        action="store_true",
        help="Run in the current directory (skip worktree isolation)",
    )
    agents.set_defaults(func=_cmd_agents)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``harness`` and ``solomon-harness`` console scripts."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    # No subcommand → default to ``serve`` (preserves ``python -m harness`` behaviour).
    if args.command is None:
        args.command = "serve"
        args.host = settings.host
        args.port = settings.port
        args.func = _cmd_serve

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
