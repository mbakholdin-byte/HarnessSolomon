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
    """Run a single sub-agent (no merge queue) and print the result."""
    import asyncio
    from pathlib import Path
    from harness.agents.registry import load_agent
    from harness.agents.runner import AgentRunner
    from harness.config import settings
    from harness.server.llm.router import LLMRouter

    try:
        spec = load_agent(args.name, project_root=settings.project_root)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.no_worktree:
        # Force worktree off — useful for read-only smoke tests.
        spec = spec.model_copy(update={"worktree_required": False})

    repo = Path(args.repo) if args.repo else settings.project_root
    if not (repo / ".git").exists() and not (repo / ".harness").exists():
        print(
            f"warning: {repo} does not look like a git repo with .harness/. "
            f"Sub-agent will create a worktree if needed.",
            file=sys.stderr,
        )

    router = LLMRouter()
    runner = AgentRunner(router=router, repo=repo)
    result = asyncio.run(runner.run(spec, args.prompt, worktree_id=args.worktree_id))
    print(f"agent={result.spec.name} iterations={result.iterations} "
          f"cost=${result.total_cost:.4f} worktree={result.worktree.worktree_id}")
    if result.error:
        print(f"error: {result.error}", file=sys.stderr)
        return 1
    print()
    print(result.final_text or "(no final text)")
    return 0


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
    run.add_argument(
        "--repo",
        help="Override project root (default: settings.project_root)",
    )
    run.add_argument(
        "--worktree-id",
        help="Override the auto-generated worktree id",
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
