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
    """Run a single sub-agent (no merge queue) and print the result.

    Phase 2.1: ``--background`` enqueues the job via
    :class:`MergeQueue.enqueue_async` (requires a merge queue with a
    JobStore) and prints the ``job_id`` immediately, exiting 0 before
    the job completes. ``--cascade`` runs the agent through
    :class:`TierSelector` — useful for cost-aware testing; in mock
    mode the confidence is hardcoded to 0.95 so the cascade picks T1
    (cheap local).
    """
    import asyncio
    from pathlib import Path
    from harness.agents.cascade import select_tier
    from harness.agents.jobs import JobStore
    from harness.agents.merge_queue import MergeJob, MergeQueue
    from harness.agents.registry import load_agent
    from harness.agents.runner import AgentRunner
    from harness.agents.verify import AdversarialVerify
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

    # Phase 2.1: background mode. We use the merge queue for any
    # sub-agent that has access to write_file (i.e. ``full`` or
    # ``scoped-write`` permissions), because background jobs are
    # explicitly designed to be revisable / cancellable. For
    # read-only agents (explore / plan / review), the synchronous
    # path is simpler and the user can re-run with --background if
    # they need it.
    if args.background:
        # Phase 2.2: --pr flag requires --background. Reject the
        # combination in the non-background path (above) so we can
        # safely assume the user opted in.
        pr_mode = "off"
        if args.pr_ready:
            pr_mode = "ready"
        elif args.pr or args.pr_draft:
            pr_mode = "draft"
        pr_target = args.pr_target or settings.pr_default_target_branch

        # In mock environments the ``code`` agent has no review spec
        # — we synthesize a read-only one so the merge queue's
        # code → review → verify path can complete (it will still
        # call the LLM, but our test LLM is a no-op stub).
        from harness.agents.spec import AgentSpec as _AS
        review_spec = _AS(
            name="review-readonly",
            model="MiniMax-M2.7",
            tools=["read_file"],
            permissions="read-only",
            system_prompt="Read-only review.",
            max_iterations=2,
            worktree_required=True,
        )
        # JobStore lives alongside the harness data dir.
        store = JobStore(settings.db_path.parent / "agent-jobs.db")
        # We DO NOT construct the verifier against the live router
        # here — background mode is opt-in for advanced users, who
        # can wire up their own verifier in the FastAPI path.
        # For the CLI we use a minimal stub.
        class _CLIStubVerifier:
            async def run(self, *, prompt: str, answer: str, model: str = "") -> bool:
                return True
        queue = MergeQueue(
            runner=runner, verifier=_CLIStubVerifier(), store=store,  # type: ignore[arg-type]
        )
        worktree_id = args.worktree_id or f"cli-{abs(hash(args.prompt)) % 10000:04d}"
        job = MergeJob(
            code_spec=spec, review_spec=review_spec,
            task=args.prompt, worktree_id=worktree_id,
            pr_mode=pr_mode,
            pr_target_branch=pr_target,
            repo_override=Path(args.repo) if args.repo else None,
        )
        job_id = asyncio.run(queue.enqueue_async(job))
        print(f"job_id={job_id}")
        print(f"  status: use `harness agents jobs {job_id}` to poll")
        if pr_mode != "off":
            print(f"  pr_mode: {pr_mode} (target={pr_target})")
        return 0

    # Phase 2.2: --pr requires --background. Sync path can't
    # ``await`` PR lifecycle events; reject early with a clear error.
    if args.pr or args.pr_draft or args.pr_ready:
        print(
            "error: --pr / --pr-draft / --pr-ready require --background "
            "(the sync path can't await the PR lifecycle)",
            file=sys.stderr,
        )
        return 2

    # Phase 2.1: cascade. Compute a tier decision and override the
    # model on this single run. We use a hardcoded 0.95 confidence
    # because the CLI doesn't have the router in the loop here;
    # real integration with ``LLMRouterClassifier`` happens in the
    # FastAPI path.
    model_override: str | None = None
    if args.cascade:
        # Mock-mode-friendly: assume the router would have said
        # "I'm confident this is an explore task". Real integration
        # would call ``LLMRouterClassifier.classify(prompt)`` first.
        decision = select_tier(0.95)
        model_override = decision.chosen_model
        print(
            f"cascade: tier={decision.tier} confidence=0.95 "
            f"model={model_override} ({decision.reason})",
            file=sys.stderr,
        )

    result = asyncio.run(
        runner.run(
            spec, args.prompt, worktree_id=args.worktree_id,
            model_override=model_override,
        )
    )
    print(f"agent={result.spec.name} iterations={result.iterations} "
          f"cost=${result.total_cost:.4f} worktree={result.worktree.worktree_id}")
    if result.error:
        print(f"error: {result.error}", file=sys.stderr)
        return 1
    print()
    print(result.final_text or "(no final text)")
    return 0


def _cmd_agents_jobs(args: argparse.Namespace) -> int:
    """Inspect background job status. ``agents jobs <id>`` or
    ``agents jobs --recent N`` to list."""
    import asyncio
    from harness.agents.jobs import JobStore
    from harness.config import settings

    store = JobStore(settings.db_path.parent / "agent-jobs.db")

    if args.job_id:
        # Single-job lookup.
        rec = asyncio.run(store.load(args.job_id))
        if rec is None:
            print(f"error: job {args.job_id!r} not found", file=sys.stderr)
            return 1
        print(f"job_id={rec.id}")
        print(f"  worktree_id : {rec.worktree_id}")
        print(f"  status      : {rec.status}")
        print(f"  model       : {rec.model}")
        print(f"  cost        : ${rec.cost:.4f}")
        print(f"  started_at  : {rec.started_at}")
        print(f"  finished_at : {rec.finished_at or '(still running)'}")
        print(f"  error       : {rec.error or '(none)'}")
        prompt = rec.prompt
        if len(prompt) > 200:
            prompt = prompt[:197] + "..."
        print(f"  prompt      : {prompt}")
        # Phase 2.2: PR integration fields (only shown when present).
        if rec.pr_mode != "off" or rec.pr_url or rec.repo:
            print(f"  pr_mode     : {rec.pr_mode}")
            if rec.target_branch:
                print(f"  target_branch: {rec.target_branch}")
            if rec.repo:
                print(f"  repo        : {rec.repo}")
            if rec.pr_url:
                print(f"  pr_url      : {rec.pr_url}")
                print(f"  pr_number   : {rec.pr_number}")
        return 0

    # List recent jobs.
    async def _list() -> int:
        recs = await store.list_recent(args.recent)
        if not recs:
            print("(no jobs)", file=sys.stderr)
            return 0
        print(
            f"{'job_id':18s}  {'status':14s}  {'model':14s}  "
            f"{'cost':>8s}  {'worktree_id':12s}  {'pr_mode':6s}  started_at"
        )
        print("-" * 110)
        for r in recs:
            print(
                f"{r.id:18s}  {r.status:14s}  {r.model:14s}  "
                f"${r.cost:7.4f}  {r.worktree_id:12s}  {r.pr_mode:6s}  {r.started_at}"
            )
        return 0

    return asyncio.run(_list())


def _cmd_agents(args: argparse.Namespace) -> int:
    """Dispatch on ``agents`` sub-subcommand."""
    if args.agents_command is None or args.agents_command == "list":
        return _cmd_agents_list(args)
    if args.agents_command == "run":
        return _cmd_agents_run(args)
    if args.agents_command == "jobs":
        return _cmd_agents_jobs(args)
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
    run.add_argument(
        "--background",
        action="store_true",
        help=(
            "Enqueue as a background job via MergeQueue.enqueue_async. "
            "Prints job_id and exits immediately. Use "
            "`harness agents jobs <id>` to poll."
        ),
    )
    run.add_argument(
        "--cascade",
        action="store_true",
        help=(
            "Route through TierSelector (T1 to T2 to T3 cascade). "
            "CLI mock uses confidence=0.95; the FastAPI path passes "
            "the real router decision."
        ),
    )
    run.add_argument(
        "--pr",
        action="store_true",
        help=(
            "Phase 2.2: open a draft PR instead of local ff-merge "
            "(shorthand for --pr-draft). Requires --background."
        ),
    )
    run.add_argument(
        "--pr-draft",
        action="store_true",
        help=(
            "Phase 2.2: open a draft PR. Requires --background. "
            "Mutually exclusive with --pr-ready (use --pr for shorthand)."
        ),
    )
    run.add_argument(
        "--pr-ready",
        action="store_true",
        help=(
            "Phase 2.2: open a ready-for-review PR (no --draft). "
            "Requires --background."
        ),
    )
    run.add_argument(
        "--pr-target",
        default=None,
        help=(
            "Phase 2.2: target branch for the PR (default: "
            "settings.pr_default_target_branch = 'main'). Only used "
            "with --pr / --pr-draft / --pr-ready."
        ),
    )
    jobs = agents_sub.add_parser("jobs", help="Inspect background jobs (Phase 2.1)")
    jobs.add_argument(
        "job_id", nargs="?", default=None,
        help="Job id to inspect. If omitted, lists recent jobs.",
    )
    jobs.add_argument(
        "--recent", type=int, default=20,
        help="Number of recent jobs to list when no job_id is given (default 20).",
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
