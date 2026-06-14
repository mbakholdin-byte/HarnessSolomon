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


def _cmd_agents_list(_args: argparse.Namespace) -> int:
    """List all available sub-agents (built-ins + project overrides)."""
    from harness.agents.registry import all_specs, has_override, list_agents

    project_root = settings.project_root
    names = list_agents(project_root=project_root)
    if not names:
        print("(no sub-agents installed)", file=sys.stderr)
        return 0
    print(f"Available sub-agents (project root: {project_root}):")
    for name in names:
        marker = " (override)" if has_override(name, project_root=project_root) else ""
        print(f"  - {name}{marker}")
    # Show a short spec line per agent for the user.
    specs = all_specs(project_root=project_root)
    print()
    for name in names:
        s = specs.get(name)
        if s is None:
            continue
        print(
            f"  {name:8s}  model={s.model:20s}  perms={s.permissions:11s}  "
            f"max_iter={s.max_iterations:2d}  tools={s.tools}"
        )
    return 0


def _cmd_agents_run(args: argparse.Namespace) -> int:
    """Run a sub-agent (Step 4+ lands the full implementation)."""
    print(
        f"Sub-agent execution is not implemented yet (will land in Step 4). "
        f"Would run {args.name!r} with prompt {args.prompt!r}.",
        file=sys.stderr,
    )
    return 2


def _cmd_agents(args: argparse.Namespace) -> int:
    """Dispatch on ``agents`` sub-subcommand."""
    if args.agents_command is None or args.agents_command == "list":
        return _cmd_agents_list(args)
    if args.agents_command == "run":
        return _cmd_agents_run(args)
    print(f"unknown agents subcommand: {args.agents_command!r}", file=sys.stderr)
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
