"""Solomon Harness — command-line entry point.

Usage:
    python -m harness                     # start the FastAPI server (default)
    harness serve [--host H] [--port P]   # same, explicit
    harness agents list                   # list built-in + overridden sub-agents (Step 2+)
    harness agents run <name> "..."       # run a sub-agent (Step 2+)
    harness plugins install <name>        # install a plugin from the marketplace
    harness plugins uninstall <name>      # uninstall a loaded plugin

This module is the target of the ``harness`` and ``solomon-harness`` console
scripts declared in ``pyproject.toml``.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Phase 1.6: ensure UTF-8 stdout on Windows. The default
# encoding is cp1251 in some Russian Windows installs, which
# would mangle the ``...`` ellipsis in our table output. We
# reconfigure lazily (the attribute may not exist on older
# Python, hence the guard).
for _stream in (sys.stdout, sys.stderr):
    reconfigure = getattr(_stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8")
        except Exception:  # noqa: BLE001 — non-TTY streams may refuse
            pass

import uvicorn  # noqa: E402  (import after stdout reconfigure)

from harness.config import settings  # noqa: E402


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
        # Phase 2.3: ``--pr-auto-merge`` is shorthand for
        # ``--pr --auto-merge`` — apply it BEFORE resolving the
        # pr_mode / auto_merge flags.
        if args.pr_auto_merge:
            args.pr = True
            args.auto_merge = True
        pr_mode = "off"
        if args.pr_ready:
            pr_mode = "ready"
        elif args.pr or args.pr_draft:
            pr_mode = "draft"
        pr_target = args.pr_target or settings.pr_default_target_branch

        # Phase 2.4: --split-into + --stack-files are independent
        # of --pr in the parser, but the stack orchestrator needs
        # pr_mode != "off" (stacks REQUIRE gh). Reject early if the
        # user forgot to combine them.
        if args.split_into and args.split_into > 1 and pr_mode == "off":
            print(
                "error: --split-into > 1 requires --pr / --pr-draft / "
                "--pr-ready (stacks require gh for create_pr)",
                file=sys.stderr,
            )
            return 2
        # Phase 2.5: --stack-repos (cross-repo stacks). Comma-
        # separated absolute paths, one per slice. The list
        # length must match --split-into. Default: None (single-
        # repo, Phase 2.4 behaviour).
        stack_repos: list[Path] | None = None
        if args.stack_repos is not None:
            stack_repos = [
                Path(p.strip()) for p in args.stack_repos.split(",")
                if p.strip()
            ]
            if len(stack_repos) != (args.split_into or 0):
                print(
                    f"error: --stack-repos has {len(stack_repos)} entries "
                    f"but --split-into is {args.split_into or 0}; "
                    f"they must match",
                    file=sys.stderr,
                )
                return 2
            for p in stack_repos:
                if not p.exists():
                    print(
                        f"error: --stack-repos path does not exist: {p}",
                        file=sys.stderr,
                    )
                    return 2
        # Read --stack-files (path list override) into a list. The
        # planner uses this in place of the auto-computed diff.
        slice_files_list: list[str] | None = None
        if args.stack_files is not None:
            slice_files_list = [
                line.strip() for line in args.stack_files
                if line.strip()
            ]
            args.stack_files.close()

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
        # Phase 3: if the user supplied --worktree-id explicitly,
        # it flows into the git branch name. Redact defensively.
        if args.worktree_id:
            from harness.redaction import redact
            worktree_id = redact(worktree_id) or worktree_id  # never empty
        # Phase 2.3: --auto-merge implies --pr (or --pr-draft /
        # --pr-ready). Reject if the user asked for auto-merge
        # without a PR mode.
        if args.auto_merge and pr_mode == "off":
            print(
                "error: --auto-merge requires --pr / --pr-draft / --pr-ready "
                "(or use --pr-auto-merge shorthand)",
                file=sys.stderr,
            )
            return 2
        job = MergeJob(
            code_spec=spec, review_spec=review_spec,
            task=args.prompt, worktree_id=worktree_id,
            pr_mode=pr_mode,
            pr_target_branch=pr_target,
            repo_override=Path(args.repo) if args.repo else None,
            auto_merge=args.auto_merge,
            auto_merge_method=args.auto_merge_method,
            auto_merge_label=args.auto_merge_label,
            split_into=args.split_into,
            stack_id=args.stack_id,
            stack_position=args.stack_position,
            stack_size=args.stack_size,
            depends_on_pr_number=args.depends_on_pr_number,
            slice_files=slice_files_list,
            stack_repos=stack_repos,
        )
        job_id = asyncio.run(queue.enqueue_async(job))
        print(f"job_id={job_id}")
        print(f"  status: use `harness agents jobs {job_id}` to poll")
        if pr_mode != "off":
            print(f"  pr_mode: {pr_mode} (target={pr_target})")
            if args.auto_merge:
                print("  auto_merge: enabled (waiting for branch-protection)")
        return 0

    # Phase 2.2: --pr requires --background. Sync path can't
    # ``await`` PR lifecycle events; reject early with a clear error.
    # Phase 2.3: ``--pr-auto-merge`` is also a PR flag — same
    # constraint. We check the unshorthanded flag here too, so
    # the user gets the same error whether they wrote ``--pr``
    # or ``--pr-auto-merge`` (the shorthand is only resolved
    # inside the ``if args.background:`` block above).
    if args.pr or args.pr_draft or args.pr_ready or args.pr_auto_merge:
        print(
            "error: --pr / --pr-draft / --pr-ready / --pr-auto-merge "
            "require --background "
            "(the sync path can't await the PR lifecycle)",
            file=sys.stderr,
        )
        return 2
    # Phase 2.4: stacked PRs also require --background.
    if args.split_into and args.split_into > 1:
        print(
            "error: --split-into > 1 requires --background "
            "(the stack orchestrator awaits per-slice create_pr)",
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


# === Phase 3 v1.2.0: scratchpad (notes + plan) inspector ===

def _cmd_context_read(args: argparse.Namespace) -> int:
    """``harness context read`` — list notes for a session/agent."""
    import asyncio
    from harness.agents.scratchpad import NoteLevel
    from harness.agents.scratchpad_store import ScratchpadStore
    from harness.config import settings

    store = ScratchpadStore(
        settings.db_path.parent / "agent-jobs.db",
        session_id=args.session,
        agent_id=args.agent,
    )

    level_enum: NoteLevel | None = None
    if args.level is not None:
        level_enum = NoteLevel(args.level)

    async def _run() -> int:
        notes = await store.read_notes(level_enum, limit=50)
        if not notes:
            print("(no notes)", file=sys.stderr)
            return 0
        print(
            f"{'id':>4}  {'level':4s}  {'tags':20s}  "
            f"{'created_at':>10s}  content"
        )
        print("-" * 100)
        for n in notes:
            tags_short = ",".join(n.tags)[:18]
            content_short = n.content.replace("\n", " ")[:60]
            print(
                f"{n.id:>4}  {n.level.value:4s}  {tags_short:20s}  "
                f"{n.created_at:>10.0f}  {content_short}"
            )
        return 0

    try:
        return asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 — surface to operator, don't crash CLI
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def _cmd_context_write(args: argparse.Namespace) -> int:
    """``harness context write`` — append a note to the scratchpad."""
    import asyncio
    from harness.agents.scratchpad import NoteLevel
    from harness.agents.scratchpad_store import ScratchpadStore
    from harness.config import settings

    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else None

    store = ScratchpadStore(
        settings.db_path.parent / "agent-jobs.db",
        session_id=args.session,
        agent_id=args.agent,
    )

    async def _run() -> int:
        note = await store.write_note(
            NoteLevel(args.level), args.content, tags=tags,
        )
        print(f"wrote note id={note.id} level={note.level.value} "
              f"size={len(note.content.encode('utf-8'))}B")
        return 0

    try:
        return asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def _cmd_context_plan(args: argparse.Namespace) -> int:
    """``harness context plan`` — list plan steps or mark one done."""
    import asyncio
    from harness.agents.scratchpad import PlanStatus
    from harness.agents.scratchpad_store import ScratchpadStore
    from harness.config import settings

    store = ScratchpadStore(
        settings.db_path.parent / "agent-jobs.db",
        session_id=args.session,
        agent_id=args.agent,
    )

    async def _list() -> int:
        status_enum = PlanStatus(args.status) if args.status else None
        steps = await store.list_plan_steps(status=status_enum)
        if not steps:
            print("(no plan steps)", file=sys.stderr)
            return 0
        print(
            f"{'id':>4}  {'status':12s}  {'deps':10s}  "
            f"{'updated_at':>10s}  description"
        )
        print("-" * 100)
        for s in steps:
            deps_short = ",".join(str(d) for d in s.deps)[:8]
            desc_short = s.description.replace("\n", " ")[:60]
            print(
                f"{s.id:>4}  {s.status.value:12s}  {deps_short:10s}  "
                f"{s.updated_at:>10.0f}  {desc_short}"
            )
        return 0

    async def _mark() -> int:
        if args.step_id is None:
            print("error: --step-id is required for --mark-done", file=sys.stderr)
            return 1
        status_enum = PlanStatus(args.status) if args.status else PlanStatus.DONE
        updated = await store.mark_done(args.step_id, status=status_enum)
        if updated is None:
            print(f"error: no plan_step with id={args.step_id}", file=sys.stderr)
            return 1
        print(f"marked step {updated.id} → {updated.status.value}")
        return 0

    try:
        return asyncio.run(_mark() if args.mark_done else _list())
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def _cmd_context(args: argparse.Namespace) -> int:
    """Dispatcher for the ``context`` subcommand (Phase 3 v1.2.0)."""
    if args.context_command == "read":
        return _cmd_context_read(args)
    if args.context_command == "write":
        return _cmd_context_write(args)
    if args.context_command == "plan":
        return _cmd_context_plan(args)
    print("error: 'harness context' requires a subcommand "
          "(read | write | plan). See `harness context --help`.",
          file=sys.stderr)
    return 1


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
    if args.agents_command == "split-plan":
        return _cmd_agents_split_plan(args)
    print(f"unknown agents subcommand: {args.agents_command!r}", file=sys.stderr)
    return 2


# === Phase 4.8 v1.18.0: ``harness elicitation`` subcommand ===

def _cmd_elicitation_history(args: argparse.Namespace) -> int:
    """``harness elicitation history`` — read the decision log.

    Reads directly from the SQLite file (no HTTP), so the operator can
    inspect state even when the server is down. Mirrors the
    ``harness context read`` / ``harness agents jobs`` pattern.

    Exit codes:
        0 — success (may print 0 rows)
        1 — store open / query error
        2 — invalid arguments (handled by argparse before we get here)
    """
    import json as _json

    from harness.config import Settings
    from harness.elicitation import ElicitationDecisionStore

    # Resolve the DB path. ``--project-root`` overrides settings, which
    # in turn override the C:/MyAI default.
    if args.project_root is not None:
        # The settings object caches on construction; rather than fight
        # that, we just re-resolve the default relative path under the
        # operator-supplied project root.
        project_root = Path(args.project_root).resolve()
        db_path = project_root / "data" / "agent-jobs.db"
    else:
        db_path = Settings().db_path.parent / "agent-jobs.db"

    if not db_path.exists():
        # No DB yet — the broker hasn't recorded anything. Print a
        # friendly message and exit 0 (matches the "no history" case).
        if args.json:
            print("[]")
        else:
            print("(no decisions)")
        return 0

    try:
        store = ElicitationDecisionStore(db_path)
        records = store.query_history(
            session_id=args.session,
            limit=args.limit,
        )
        store.close()
    except Exception as exc:  # noqa: BLE001 — surface to operator
        print(
            f"error: cannot read elicitation history from {db_path}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1

    if not records:
        if args.json:
            print("[]")
        else:
            print("(no decisions)")
        return 0

    if args.json:
        out = [
            {
                "decision_id": r.decision_id,
                "session_id": r.session_id,
                "request_id": r.request_id,
                "question_id": r.question_id,
                "question_preview": r.question_preview,
                "options": list(r.options or []),
                "default_answer": r.default_answer,
                "decision": r.decision,
                "answer": r.answer,
                "source": r.source,
                "latency_ms": r.latency_ms,
                "ts": r.ts,
            }
            for r in records
        ]
        print(_json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    # Pretty table. Columns: ts | session | decision | answer | source | latency_ms
    # ``ts`` is rendered as ISO-8601 UTC for human consumption.
    import datetime as _dt

    print(
        f"{'ts':24s}  {'session':16s}  {'decision':10s}  "
        f"{'answer':14s}  {'source':8s}  {'latency_ms':>10s}"
    )
    print("-" * 92)
    for r in records:
        try:
            ts_iso = _dt.datetime.fromtimestamp(
                r.ts, tz=_dt.timezone.utc,
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (OSError, ValueError, OverflowError):
            ts_iso = f"{r.ts:.3f}"
        sess = (r.session_id or "")[:16]
        decision = r.decision[:10]
        answer = (r.answer or "")[:14]
        source = r.source or "-"
        print(
            f"{ts_iso:24s}  {sess:16s}  {decision:10s}  "
            f"{answer:14s}  {source:8s}  {int(r.latency_ms):>10d}"
        )
    return 0


def _cmd_elicitation(args: argparse.Namespace) -> int:
    """Dispatcher for the ``elicitation`` subcommand (Phase 4.8 v1.18.0)."""
    if args.elicitation_command == "history":
        return _cmd_elicitation_history(args)
    print(
        "error: 'harness elicitation' requires a subcommand "
        "(history). See `harness elicitation --help`.",
        file=sys.stderr,
    )
    return 2


def _cmd_agents_split_plan(args: argparse.Namespace) -> int:
    """Phase 2.4: preview a split plan without enqueuing.

    Runs the same planner that ``_run_stack_phase`` uses, but
    in a read-only way: no gh, no git mutations, no JobStore
    writes. Operators can verify the split before committing
    to ``--split-into N --pr --background``.

    Exit codes:
      - 0: success (plan printed)
      - 2: invalid input (bad path, empty diff, git error)
      - 3: planner error (rare; only on bad settings)
    """
    from harness.agents.pr_split import plan_splits
    from harness.config import settings

    strategy = args.strategy or settings.pr_split_strategy
    base = args.base or settings.pr_default_target_branch

    # 1. Resolve file list.
    if args.files is not None:
        diff_files = [
            line.strip() for line in args.files
            if line.strip()
        ]
        args.files.close()
    else:
        # Run git diff to get the file list.
        repo = args.worktree_path or "."
        try:
            import subprocess
            out = subprocess.run(
                ["git", "-C", repo, "diff", "--name-only", base],
                capture_output=True, text=True, timeout=30,
            )
        except FileNotFoundError:
            print(
                "error: git not found in PATH; pass --files <list> "
                "or install git",
                file=sys.stderr,
            )
            return 3
        except subprocess.TimeoutExpired:
            print("error: git diff timed out", file=sys.stderr)
            return 2
        if out.returncode != 0:
            print(
                f"error: git diff failed (rc={out.returncode}): "
                f"{out.stderr.strip()}",
                file=sys.stderr,
            )
            return 2
        diff_files = [
            line for line in out.stdout.splitlines() if line.strip()
        ]
    if not diff_files:
        print(
            f"no files in diff vs {base!r} (or --files was empty)",
            file=sys.stderr,
        )
        return 0  # not an error — caller can decide

    # 2. Plan.
    plan = plan_splits(
        diff_files=diff_files,
        strategy=strategy,
        worktree_id="wt-dryrun",  # not used for branches; cosmetic
        task="(split-plan dry-run)",
        n_slices=args.split_into,
        max_files_per_slice=settings.pr_split_max_files_per_slice,
        min_slices=settings.pr_split_min_slices,
        max_slices=settings.pr_split_max_slices,
    )

    # 3. Print.
    print(f"plan: {len(plan)} slice(s) via {strategy!r} strategy")
    print(f"  base ref: {base}")
    print(f"  total files: {len(diff_files)}")
    print()
    if len(plan) == 1:
        # Single-slice plan — would use the legacy single-PR path.
        print("  (planner collapsed to 1 slice; the run would "
              "use the single-PR path, no stack)")
        return 0
    for i, slice in enumerate(plan):
        print(f"slice {i + 1}/{len(plan)}: {slice.branch_name}")
        for f in slice.files:
            print(f"    {f}")
    return 0


# === Phase 1.6: scope-gated API auth subcommand ===

async def _bootstrap_admin_token_if_needed(store) -> None:
    """Mint a bootstrap-admin token if no active token exists.

    The bootstrap path runs at every CLI invocation that touches
    the auth store (read-only commands only — see
    :func:`_dispatch_auth`). It is a no-op when at least one
    non-revoked token already exists, so manual revokes don't
    trigger re-bootstrap. The bootstrap token always gets
    ALL_SCOPES and is labelled ``bootstrap-admin``.
    """
    from harness.server.auth.scopes import ALL_SCOPES
    if await store.has_any_active():
        return
    plaintext, _ = await store.create("bootstrap-admin", scopes=set(ALL_SCOPES))
    print(
        f"[harness] bootstrap-admin token created (label=bootstrap-admin).",
        file=sys.stderr,
    )
    print(
        f"[harness] SAVE THIS — it will not be shown again:\n  {plaintext}",
        file=sys.stderr,
    )
    print(
        f"[harness] verify with: harness auth test {plaintext}",
        file=sys.stderr,
    )


# === Phase 4.2+ v1.9.0: ``harness reload`` ===

_RELOAD_KINDS = ("agents", "hooks", "privacy")


def _reload_agents(project_root: Path) -> dict[str, object]:
    """Re-parse all ``.harness/agents/*.md`` project overrides.

    Returns a dict suitable for ``--json`` output:
    ``{"kind": "agents", "loaded": [...], "errors": [...]}``.

    Built-in agents are NOT re-parsed here — they live in the
    package and are read lazily on every ``all_specs()`` call.
    This command is about validating the user's project overrides.
    """
    from harness.agents.registry import _read_override

    agents_dir = project_root / ".harness" / "agents"
    loaded: list[str] = []
    errors: list[dict[str, str]] = []
    if not agents_dir.is_dir():
        return {
            "kind": "agents",
            "loaded": loaded,
            "errors": errors,
            "dir": str(agents_dir),
            "note": "directory does not exist",
        }
    for path in sorted(agents_dir.glob("*.md")):
        if path.name.startswith("."):
            continue
        name = path.stem
        try:
            spec = _read_override(project_root, name)
            if spec is None:
                errors.append({"name": name, "error": "not parseable"})
            else:
                loaded.append(name)
        except Exception as exc:  # noqa: BLE001
            errors.append({"name": name, "error": str(exc)})
    return {
        "kind": "agents",
        "loaded": loaded,
        "errors": errors,
        "dir": str(agents_dir),
    }


def _reload_hooks(project_root: Path) -> dict[str, object]:
    """Re-parse all ``.harness/hooks/*.json`` files."""
    import json

    from harness.hooks.hot_reload import _parse_hook_file

    hooks_dir = project_root / ".harness" / "hooks"
    loaded: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    if not hooks_dir.is_dir():
        return {
            "kind": "hooks",
            "loaded": loaded,
            "errors": errors,
            "dir": str(hooks_dir),
            "note": "directory does not exist",
        }
    for path in sorted(hooks_dir.glob("*.json")):
        try:
            specs = _parse_hook_file(path)
            for spec in specs:
                loaded.append({"file": path.name, "hook_id": spec.hook_id})
        except (ValueError, json.JSONDecodeError) as exc:
            errors.append({"file": path.name, "error": str(exc)})
    return {
        "kind": "hooks",
        "loaded": loaded,
        "errors": errors,
        "dir": str(hooks_dir),
    }


def _reload_privacy(project_root: Path) -> dict[str, object]:
    """Re-parse all ``.harness/privacy/*.json`` files."""
    from harness.config import settings
    from harness.privacy.hot_reload import _parse_privacy_file

    privacy_dir = project_root / ".harness" / "privacy"
    rule_count = 0
    errors: list[dict[str, str]] = []
    files: list[str] = []
    if not privacy_dir.is_dir():
        return {
            "kind": "privacy",
            "rule_count": rule_count,
            "files": files,
            "errors": errors,
            "dir": str(privacy_dir),
            "note": "directory does not exist",
        }
    for path in sorted(privacy_dir.glob("*.json")):
        try:
            rules = _parse_privacy_file(path, settings.privacy_zone_default_action)
            rule_count += len(rules)
            files.append(path.name)
        except Exception as exc:  # noqa: BLE001
            errors.append({"file": path.name, "error": str(exc)})
    return {
        "kind": "privacy",
        "rule_count": rule_count,
        "files": files,
        "errors": errors,
        "dir": str(privacy_dir),
    }


def _cmd_reload(args: argparse.Namespace) -> int:
    """``harness reload [kind]`` — force-reload hot-reloadable resources.

    Validates files in ``.harness/`` subdirs locally (no server
    connection required). The server's hot-reload watchers will
    pick up the same files automatically on the next file event;
    this command is for users who want to validate without
    waiting for a file event or who want a quick sanity check.

    Kinds: ``all`` (default), ``agents``, ``hooks``, ``privacy``.

    Exit codes:
        0 — all parsed cleanly (may have 0 files).
        1 — at least one file failed to parse.
        2 — invalid arguments.
    """
    import json as _json

    project_root_arg = getattr(args, "project_root", None)
    project_root = (
        Path(project_root_arg).resolve()
        if project_root_arg
        else Path.cwd()
    )
    if not project_root.is_dir():
        print(
            f"[harness] reload: project_root {project_root} is not a directory",
            file=sys.stderr,
        )
        return 2

    kind = args.reload_command or "all"
    if kind not in _RELOAD_KINDS and kind != "all":
        print(
            f"[harness] reload: unknown kind {kind!r}; "
            f"expected one of {('all', *_RELOAD_KINDS)}",
            file=sys.stderr,
        )
        return 2

    results: list[dict[str, object]] = []
    kinds_to_run = _RELOAD_KINDS if kind == "all" else [kind]
    for k in kinds_to_run:
        if k == "agents":
            results.append(_reload_agents(project_root))
        elif k == "hooks":
            results.append(_reload_hooks(project_root))
        elif k == "privacy":
            results.append(_reload_privacy(project_root))

    has_errors = any(bool(r.get("errors")) for r in results)

    if getattr(args, "json", False):
        print(_json.dumps({"results": results, "ok": not has_errors}, indent=2))
    else:
        for r in results:
            kind_name = r.get("kind", "?")
            if kind_name == "privacy":
                files = r.get("files", [])
                print(
                    f"[harness] reload: privacy — {r.get('rule_count', 0)} rules "
                    f"from {len(files)} file(s) in {r.get('dir')}"
                )
                for f in files:
                    print(f"  ok {f}")
            else:
                loaded = r.get("loaded", [])
                print(
                    f"[harness] reload: {kind_name} — {len(loaded)} loaded "
                    f"from {r.get('dir')}"
                )
                for item in loaded:
                    if isinstance(item, str):
                        print(f"  ok {item}")
                    elif isinstance(item, dict):
                        print(f"  ok {item.get('hook_id', item.get('file', '?'))}")
            for err in r.get("errors", []):
                if "name" in err:
                    print(
                        f"  ERROR {err['name']}: {err['error']}",
                        file=sys.stderr,
                    )
                elif "file" in err:
                    print(
                        f"  ERROR {err['file']}: {err['error']}",
                        file=sys.stderr,
                    )
        if has_errors:
            print(
                "[harness] reload: failed (see errors above)",
                file=sys.stderr,
            )
        else:
            print("[harness] reload: ok")

    return 1 if has_errors else 0


def _cmd_auth_create(args: argparse.Namespace) -> int:
    """``harness auth create --label L --scopes S`` → print token ONCE."""
    import asyncio
    from harness.server.auth.scopes import ALL_SCOPES, Scope, format_scopes, parse_scopes
    from harness.server.auth.tokens import TokenStore

    async def _run() -> int:
        store = TokenStore(settings.auth_db_path)
        await store.init()
        if args.bootstrap:
            scopes: set[Scope] = set(ALL_SCOPES)
        elif args.scopes is not None:
            try:
                scopes = parse_scopes(args.scopes)
            except ValueError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
        else:
            try:
                scopes = parse_scopes(settings.auth_default_scopes)
            except ValueError as e:
                print(f"error: {e}", file=sys.stderr)
                return 2
        if not scopes:
            print(
                "error: no scopes specified (use --scopes, --bootstrap, "
                "or set settings.auth_default_scopes)",
                file=sys.stderr,
            )
            return 2
        plaintext, record = await store.create(args.label, scopes)
        # Parseable stdout format — easy to grep from a script.
        print(f"token={plaintext}")
        print(f"label={record.label}")
        # Use format_scopes so the ``*`` shorthand is rendered for
        # the ALL_SCOPES case (matches `harness auth list` output).
        print(f"scopes={format_scopes(scopes)}")
        print(
            "WARNING: this is the only time the plaintext will be shown. "
            "Store it in a password manager or env var.",
            file=sys.stderr,
        )
        return 0

    return asyncio.run(_run())


def _cmd_auth_list(_args: argparse.Namespace) -> int:
    """``harness auth list`` → table of active tokens."""
    import asyncio
    from harness.server.auth.scopes import format_scopes
    from harness.server.auth.tokens import TokenStore

    async def _run() -> int:
        store = TokenStore(settings.auth_db_path)
        await store.init()
        records = await store.list_active()
        if not records:
            print("(no active tokens)", file=sys.stderr)
            return 0
        label_w = max(len("label"), max(len(r.label) for r in records))
        scopes_w = max(
            len("scopes"),
            max(len(format_scopes(r.scopes)) for r in records),
        )
        print(
            f"{'label':{label_w}s}  {'scopes':{scopes_w}s}  "
            f"{'created_at':19s}  {'last_used_at':19s}  hash"
        )
        print("-" * (label_w + scopes_w + 19 * 2 + 100))
        for r in records:
            last_used = (
                r.last_used_at.isoformat() if r.last_used_at else "(never)"
            )
            created = (
                r.created_at.isoformat() if r.created_at else "?"
            )
            print(
                f"{r.label:{label_w}s}  "
                f"{format_scopes(r.scopes):{scopes_w}s}  "
                f"{created:19s}  "
                f"{last_used:19s}  "
                f"{r.token_hash[:12]}..."
            )
        return 0

    return asyncio.run(_run())


def _cmd_auth_revoke(args: argparse.Namespace) -> int:
    """``harness auth revoke <hash-or-label>`` → mark revoked.

    Accepts a 64-char token_hash or a label. The label form is
    for one-off revokes; the hash form is the programmatic
    path (no ambiguity when labels collide).
    """
    import asyncio
    from harness.server.auth.tokens import TokenStore

    async def _run() -> int:
        store = TokenStore(settings.auth_db_path)
        await store.init()
        target = args.target.strip()
        is_hash = (
            len(target) == 64
            and all(c in "0123456789abcdef" for c in target.lower())
        )
        if is_hash:
            ok = await store.revoke(target)
            if ok:
                print(f"revoked: {target[:12]}...")
                return 0
            print(
                f"error: no active token with hash {target[:12]}...",
                file=sys.stderr,
            )
            return 1
        # Label path.
        records = await store.list_active()
        matches = [r for r in records if r.label == target]
        if not matches:
            print(
                f"error: no active token with label {target!r}",
                file=sys.stderr,
            )
            return 1
        if len(matches) > 1:
            print(
                f"error: multiple active tokens with label {target!r} "
                f"({len(matches)} found) — revoke by hash to disambiguate",
                file=sys.stderr,
            )
            return 1
        ok = await store.revoke(matches[0].token_hash)
        if ok:
            print(
                f"revoked: {target} ({matches[0].token_hash[:12]}...)"
            )
            return 0
        print(
            f"error: token {target!r} was already revoked (race?)",
            file=sys.stderr,
        )
        return 1

    return asyncio.run(_run())


def _cmd_auth_whoami(args: argparse.Namespace) -> int:
    """``harness auth whoami <plaintext>`` → show scopes + metadata."""
    import asyncio
    from harness.server.auth.scopes import format_scopes
    from harness.server.auth.tokens import TokenStore

    async def _run() -> int:
        store = TokenStore(settings.auth_db_path)
        await store.init()
        record = await store.lookup(args.plaintext)
        if record is None:
            print("error: invalid or revoked token", file=sys.stderr)
            return 1
        print(f"label        : {record.label}")
        print(f"scopes       : {format_scopes(record.scopes)}")
        print(f"hash         : {record.token_hash}")
        print(
            f"created_at   : "
            f"{record.created_at.isoformat() if record.created_at else '?'}"
        )
        print(
            f"last_used_at : "
            f"{record.last_used_at.isoformat() if record.last_used_at else '(never)'}"
        )
        print(
            f"revoked_at   : "
            f"{record.revoked_at.isoformat() if record.revoked_at else '(active)'}"
        )
        return 0

    return asyncio.run(_run())


def _cmd_auth_test(args: argparse.Namespace) -> int:
    """``harness auth test <plaintext>`` → smoke test the token.

    Calls ``GET /api/v1/capabilities`` on the local server with
    the supplied token. Useful for CI smoke tests and operator
    debugging. Exits 0 on 200, 1 on any error.
    """
    import urllib.error
    import urllib.request

    url = args.base_url.rstrip("/") + "/api/v1/capabilities"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {args.plaintext}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                print(f"ok: {url} -> 200")
                return 0
            body = resp.read().decode("utf-8")
            print(
                f"error: {url} -> {resp.status} {body}",
                file=sys.stderr,
            )
            return 1
    except urllib.error.HTTPError as e:
        print(f"error: {url} -> {e.code} {e.reason}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError) as e:
        print(
            f"error: {url} unreachable ({type(e).__name__}: {e}). "
            f"Is `harness serve` running on {args.base_url}?",
            file=sys.stderr,
        )
        return 1


def _cmd_sessions_compact(args: argparse.Namespace) -> int:
    """Phase 3 v1.4.0: ``harness sessions compact --session <id>``.

    Manual /compact via the CLI. Posts to the running server's
    ``POST /api/v1/sessions/{id}/compact`` endpoint and prints
    a one-line summary plus the structured JSON.

    The CLI does NOT do the compact itself — the server is the
    owner of the compactor. This avoids the need to bootstrap a
    full app in the CLI process just to summarise a session.
    """
    import json
    import urllib.error
    import urllib.request

    url = f"{args.base_url.rstrip('/')}/api/v1/sessions/{args.session}/compact"
    body = b""
    if args.bypass_cache:
        url += "?bypass_cache=true"

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail", str(exc))
        except Exception:
            detail = str(exc)
        print(
            f"error: HTTP {exc.code} from server: {detail}",
            file=sys.stderr,
        )
        return 1
    except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
        print(
            f"error: cannot reach {args.base_url}: {exc}. "
            f"Is `harness serve` running?",
            file=sys.stderr,
        )
        return 1

    # Print human summary + structured JSON.
    saved = payload.get("saved_tokens", 0)
    orig = payload.get("original_tokens", 0)
    comp = payload.get("compacted_tokens", 0)
    cache = payload.get("cache_hit", False)
    print(
        f"compacted session={args.session}: "
        f"{orig}->{comp} tokens (saved {saved}, cache_hit={cache})"
    )
    if payload.get("summary_preview"):
        preview = payload["summary_preview"]
        if len(preview) > 200:
            preview = preview[:200] + "…"
        print(f"  preview: {preview}")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _dispatch_auth(args: argparse.Namespace) -> int:
    """Dispatch on ``auth`` sub-subcommand + run bootstrap when needed.

    Bootstrap runs BEFORE read-only commands (``list``, ``whoami``,
    ``test``) so the operator can always inspect the auth state.
    It does NOT run before ``create`` / ``revoke`` — those are
    write commands and bootstrap could surprise the user.
    """
    import asyncio
    from harness.server.auth.tokens import TokenStore

    needs_bootstrap = args.auth_command in {"list", "whoami", "test"}
    if needs_bootstrap and settings.auth_required:
        async def _run_bootstrap() -> None:
            store = TokenStore(settings.auth_db_path)
            await store.init()
            await _bootstrap_admin_token_if_needed(store)
        asyncio.run(_run_bootstrap())

    if args.auth_command is None or args.auth_command == "list":
        return _cmd_auth_list(args)
    if args.auth_command == "create":
        return _cmd_auth_create(args)
    if args.auth_command == "revoke":
        return _cmd_auth_revoke(args)
    if args.auth_command == "whoami":
        return _cmd_auth_whoami(args)
    if args.auth_command == "test":
        return _cmd_auth_test(args)
    print(f"unknown auth subcommand: {args.auth_command!r}", file=sys.stderr)
    return 2


# === Phase 7.4 WI-04 v1.32.0: plugin install / uninstall ===


def _semver_gte(current: str, minimum: str) -> bool:
    """Return True if current >= minimum (major.minor.patch only). Pre-release suffixes are stripped."""
    import re as _re

    def parts(v: str) -> tuple[int, ...]:
        v = _re.sub(r"-.*$", "", v)  # strip pre-release suffix (e.g. "0-alpha")
        return tuple(int(x) for x in v.split(".")[:3])
    return parts(current) >= parts(minimum)


def _load_manifests_from_dir(
    marketplace: object, json_dir: Path,
) -> None:
    """Load plugin manifests from ``*.json`` files in ``json_dir``.

    Each file is deserialized via :meth:`PluginManifestV2.from_dict`,
    validated, and registered into the ``MarketplaceManager``. Invalid
    files are logged and skipped (best-effort).
    """
    import json as _json

    from harness.plugins.manifest_v2 import PluginManifestV2

    if not json_dir.is_dir():
        return

    for path in sorted(json_dir.glob("*.json")):
        try:
            raw = path.read_text(encoding="utf-8")
            data = _json.loads(raw)
            manifest = PluginManifestV2.from_dict(data)
            manifest.validate()
            marketplace.register(manifest)
        except Exception:  # noqa: BLE001 — best-effort
            pass


def _cmd_plugins_install(args: argparse.Namespace) -> int:
    """``harness plugins install <name>`` — install a plugin from the marketplace.

    1. Load manifests from ``--marketplace-dir`` into a ``MarketplaceManager``.
    2. Look up the manifest by name.
    3. Check ``min_harness_version`` against current version.
    4. Verify signature if the manifest is signed (via ``TrustRegistry``).
    5. Atomically copy the plugin ``.py`` file to ``plugins_dir``.
    6. Load into the :class:`PluginRegistry`.
    """
    import json as _json
    import tempfile
    from pathlib import Path
    from harness import __version__ as _current_version
    from harness.config import settings
    from harness.plugins import get_registry
    from harness.plugins.loader import load_plugins_from_dir
    from harness.plugins.marketplace import MarketplaceManager
    from harness.security.trust_registry import TrustRegistry

    name = args.plugin_name

    # Resolve directories.
    marketplace_dir = (
        Path(args.marketplace_dir)
        if args.marketplace_dir
        else settings.project_root / ".harness" / "marketplace"
    )
    plugins_dir = (
        Path(args.plugins_dir)
        if args.plugins_dir
        else settings.project_root / settings.plugins_dir
    )

    # Bootstrap marketplace from JSON manifests.
    marketplace = MarketplaceManager()
    _load_manifests_from_dir(marketplace, marketplace_dir)

    manifest = marketplace.get(name)
    if manifest is None:
        print(
            f"error: Plugin {name!r} not found in marketplace",
            file=sys.stderr,
        )
        return 1

    # semver check.
    if manifest.min_harness_version:
        if not _semver_gte(_current_version, manifest.min_harness_version):
            print(
                f"error: Plugin {name!r} requires harness >="
                f" {manifest.min_harness_version}; current is"
                f" {_current_version}",
                file=sys.stderr,
            )
            return 1

    # Signature verification.
    if manifest.signature is not None and manifest.public_key is not None:
        tr_path = (
            Path(args.trust_registry)
            if args.trust_registry
            else settings.project_root / ".harness" / "trust-registry.json"
        )
        trust_registry = TrustRegistry(path=tr_path if tr_path.exists() else None)
        if tr_path.exists():
            try:
                trust_registry.load()
            except Exception:
                pass  # best-effort — registry may be absent in dev

        # Check if key is trusted (for warning only).
        key_trusted = any(
            pk == manifest.public_key for pk in trust_registry._keys.values()  # noqa: SLF001
        )
        if not key_trusted:
            print(
                f"warning: Public key not in trust registry"
                f" for plugin {name!r}",
                file=sys.stderr,
            )

        # Crypto verification.
        manifest_bytes = _json.dumps(
            manifest.to_dict(), sort_keys=True,
        ).encode("utf-8")
        ok = trust_registry.verify_signature(
            manifest.public_key,
            manifest.signature,
            manifest_bytes,
        )
        if not ok:
            print(
                f"error: Signature verification failed"
                f" for plugin {name!r}",
                file=sys.stderr,
            )
            return 1
    else:
        print(
            f"warning: Plugin {name!r} is unsigned"
            f" — install at your own risk",
            file=sys.stderr,
        )

    # Atomic install: copy .py from marketplace dir → plugins dir.
    source_py = marketplace_dir / f"{name}.py"
    if not source_py.exists():
        print(
            f"error: Plugin source file {source_py}"
            f" not found in marketplace",
            file=sys.stderr,
        )
        return 1

    plugins_dir.mkdir(parents=True, exist_ok=True)

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=".py",
            delete=False, dir=plugins_dir,
        ) as tmp:
            tmp.write(source_py.read_bytes())
            tmp_path = Path(tmp.name)
        tmp_path.replace(plugins_dir / f"{name}.py")
    except Exception as exc:  # noqa: BLE001 — surface to operator
        print(
            f"error: Failed to copy plugin {name!r}:"
            f" {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        # Clean up temp file on failure.
        try:
            tmp_path.unlink(missing_ok=True)  # type: ignore[name-defined]
        except Exception:
            pass
        return 1

    # Register into PluginRegistry.
    registry = get_registry()
    load_plugins_from_dir(plugins_dir, registry=registry, allowed=[name])

    print(f"Plugin {name!r} installed successfully")
    return 0


def _cmd_plugins_uninstall(args: argparse.Namespace) -> int:
    """``harness plugins uninstall <name>`` — uninstall a plugin.

    1. Check that the plugin is loaded in the ``PluginRegistry``.
    2. Call ``registry.disable(name)``.
    3. Remove the ``.py`` file from ``plugins_dir``.
    """
    from pathlib import Path
    from harness.config import settings
    from harness.plugins import get_registry

    name = args.plugin_name

    plugins_dir = (
        Path(args.plugins_dir)
        if args.plugins_dir
        else settings.project_root / settings.plugins_dir
    )

    registry = get_registry()
    if registry.get_plugin(name) is None:
        print(
            f"error: Plugin {name!r} is not loaded",
            file=sys.stderr,
        )
        return 1

    registry.disable(name)

    # Remove .py file (best-effort — may not exist if uninstall was
    # called on a manually-registered plugin).
    plugin_file = plugins_dir / f"{name}.py"
    try:
        if plugin_file.exists():
            plugin_file.unlink()
    except OSError as exc:
        print(
            f"warning: Could not remove {plugin_file}: {exc}",
            file=sys.stderr,
        )

    print(f"Plugin {name!r} uninstalled successfully")
    return 0


def _cmd_plugins(args: argparse.Namespace) -> int:
    """Dispatch on ``plugins`` sub-subcommand."""
    if args.plugins_command == "install":
        return _cmd_plugins_install(args)
    if args.plugins_command == "uninstall":
        return _cmd_plugins_uninstall(args)
    print(
        f"unknown plugins subcommand: {args.plugins_command!r}",
        file=sys.stderr,
    )
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
    run.add_argument(
        "--auto-merge",
        action="store_true",
        help=(
            "Phase 2.3: use 'gh pr merge --auto' (branch-protection-aware) "
            "after CI checks pass. The job transitions to "
            "pr_auto_merge_enabled and waits for an inbound GitHub "
            "webhook to mark it merged. Requires --pr / --pr-draft / "
            "--pr-ready AND --background. Falls back to direct merge "
            "if branch protection does not allow --auto."
        ),
    )
    run.add_argument(
        "--pr-auto-merge",
        action="store_true",
        help=(
            "Phase 2.3: shorthand for '--pr --auto-merge' (open a "
            "draft PR AND enable branch-protection-aware auto-merge). "
            "Requires --background."
        ),
    )
    run.add_argument(
        "--auto-merge-method",
        choices=["squash", "merge", "rebase"], default=None,
        help=(
            "Phase 2.3: merge method for 'gh pr merge --auto' "
            "(default: settings.auto_merge_method = 'squash'). "
            "Ignored without --auto-merge."
        ),
    )
    run.add_argument(
        "--auto-merge-label",
        default=None,
        help=(
            "Phase 2.3: override settings.auto_merge_label (default "
            "'harness-auto-merge'). The queue does NOT add this label "
            "to the PR — branch protection is expected to require it. "
            "Ignored without --auto-merge."
        ),
    )
    # === Phase 2.4: stacked / multi-PR ===
    run.add_argument(
        "--split-into",
        type=int, default=None,
        help=(
            "Phase 2.4: split the worktree's diff into N PR slices "
            "(N stacked PRs per job). Each slice's PR targets the "
            "previous slice's branch (GitHub stacked-PR convention). "
            "Requires --pr / --pr-draft / --pr-ready AND --background. "
            "Use 'harness agents split-plan' to preview the split "
            "before enqueuing."
        ),
    )
    run.add_argument(
        "--split-strategy",
        choices=["auto", "files", "directory", "size"], default=None,
        help=(
            "Phase 2.4: split strategy for --split-into. "
            "(default: settings.pr_split_strategy = 'auto'). "
            "'auto' collapses to 1 slice if the diff fits in "
            "max_files_per_slice; 'directory' groups by top-level "
            "dir; 'files' round-robins; 'size' balances by LOC."
        ),
    )
    run.add_argument(
        "--stack-files",
        type=argparse.FileType("r", encoding="utf-8"), default=None,
        help=(
            "Phase 2.4: file with newline-separated paths to use for "
            "the split (overrides the planner's grouping). Only the "
            "listed files are split; unlisted files are ignored. "
            "Useful for ops: 'git diff --name-only main > stack.txt'."
        ),
    )
    run.add_argument(
        "--stack-repos",
        default=None,
        help=(
            "Phase 2.5: comma-separated absolute paths to git "
            "repos, ONE PER SLICE. Cross-repo stacks: each slice "
            "lives in its own repo, opens its own PR, and is "
            "merged independently. The list length MUST match "
            "--split-into (validated). Example: "
            "--split-into 2 --stack-repos /repo/a,/repo/b. "
            "Default: None (single-repo, all slices in the "
            "current worktree repo — Phase 2.4 behaviour)."
        ),
    )
    # Internal: stack_id / stack_position / stack_size /
    # depends_on_pr_number are advanced args for re-enqueueing an
    # existing stack (rare; usually managed by the orchestrator).
    run.add_argument(
        "--stack-id",
        default=None,
        help=argparse.SUPPRESS,  # internal: re-enqueue a known stack
    )
    run.add_argument(
        "--stack-position",
        type=int, default=0,
        help=argparse.SUPPRESS,  # internal: 0 = orchestrator
    )
    run.add_argument(
        "--stack-size",
        type=int, default=1,
        help=argparse.SUPPRESS,  # internal: total slice count
    )
    run.add_argument(
        "--depends-on-pr-number",
        type=int, default=None,
        help=argparse.SUPPRESS,  # internal: parent slice's PR number
    )
    # ^ Note: ``--stack-files`` is the public override; the other 4
    # fields are set automatically by the stack orchestrator when it
    # re-enqueues child slices. We hide them from ``--help`` to keep
    # the CLI surface clean.
    jobs = agents_sub.add_parser("jobs", help="Inspect background jobs (Phase 2.1)")
    jobs.add_argument(
        "job_id", nargs="?", default=None,
        help="Job id to inspect. If omitted, lists recent jobs.",
    )
    jobs.add_argument(
        "--recent", type=int, default=20,
        help="Number of recent jobs to list when no job_id is given (default 20).",
    )
    # Phase 2.4: split-plan subcommand for previewing stacked PR splits.
    split_plan = agents_sub.add_parser(
        "split-plan",
        help=(
            "Phase 2.4: preview how a worktree's diff would be split "
            "into N stacked PRs (dry-run, no git/gh). "
            "Run from a git repo or pass --files <list>."
        ),
    )
    split_plan.add_argument(
        "worktree_path", nargs="?",
        help=(
            "Path to a git worktree (default: current directory). "
            "The split plan is built from 'git diff --name-only <base>'."
        ),
    )
    split_plan.add_argument(
        "--base", default=None,
        help=(
            "Base branch / ref for the diff (default: "
            "settings.pr_default_target_branch = 'main')."
        ),
    )
    split_plan.add_argument(
        "--split-into", type=int, default=None,
        help=(
            "Phase 2.4: target slice count. If omitted, the planner "
            "uses max_files_per_slice to decide."
        ),
    )
    split_plan.add_argument(
        "--strategy",
        choices=["auto", "files", "directory", "size"], default=None,
        help=(
            "Phase 2.4: split strategy (default: "
            "settings.pr_split_strategy = 'auto')."
        ),
    )
    split_plan.add_argument(
        "--files", type=argparse.FileType("r", encoding="utf-8"), default=None,
        help=(
            "Override the diff: read newline-separated file paths "
            "from this file instead of running git diff. Useful "
            "for CI: 'git diff --name-only main > files.txt'."
        ),
    )
    agents.set_defaults(func=_cmd_agents)

    # Phase 4.8 v1.18.0: ``elicitation`` subcommand — decision history.
    # Reads directly from ``agent-jobs.db`` so the operator can inspect
    # state even when the server is down.
    elicitation_p = sub.add_parser(
        "elicitation",
        help=(
            "Inspect the Elicitation decision log (Phase 4.8 v1.18.0). "
            "Reads directly from the agent-jobs.db SQLite file. "
            "See `harness elicitation --help`."
        ),
    )
    elicitation_sub = elicitation_p.add_subparsers(dest="elicitation_command")

    elicitation_history_p = elicitation_sub.add_parser(
        "history",
        help=(
            "List recent Elicitation decisions (publish / answer / timeout). "
            "Newest first. Filter by --session, cap with --limit."
        ),
    )
    elicitation_history_p.add_argument(
        "--session", default=None,
        help="Filter by session_id (exact match).",
    )
    elicitation_history_p.add_argument(
        "--limit", type=int, default=100,
        help="Max rows to show (default 100).",
    )
    elicitation_history_p.add_argument(
        "--json", action="store_true",
        help="Print as a JSON array (machine-readable).",
    )
    elicitation_history_p.add_argument(
        "--project-root", default=None,
        help=(
            "Project root directory (default: settings.project_root). "
            "The CLI looks for ``data/agent-jobs.db`` under this root."
        ),
    )
    elicitation_history_p.set_defaults(func=_cmd_elicitation_history)
    # If no subcommand, default to "history" with the parent's flags.
    elicitation_p.set_defaults(
        func=_cmd_elicitation, elicitation_command="history",
    )

    # Phase 3 v1.2.0: scratchpad inspector. Reads directly from
    # ``agent-jobs.db`` (no HTTP) so the operator can inspect state
    # even when the server is down.
    ctx = sub.add_parser(
        "context",
        help=(
            "Scratchpad (notes + plan) inspector (Phase 3 v1.2.0). "
            "Reads directly from the agent-jobs.db SQLite file. "
            "See `harness context --help`."
        ),
    )
    ctx_sub = ctx.add_subparsers(dest="context_command")

    ctx_read = ctx_sub.add_parser(
        "read", help="List scratchpad notes for a session",
    )
    ctx_read.add_argument("--session", required=True, help="Session id")
    ctx_read.add_argument(
        "--agent", default=None,
        help="Agent id (default: any agent / admin context)",
    )
    ctx_read.add_argument(
        "--level", choices=["L0", "L1", "L2"], default=None,
        help="Filter by memory layer (default: all)",
    )

    ctx_write = ctx_sub.add_parser(
        "write", help="Append a note to the scratchpad",
    )
    ctx_write.add_argument("--session", required=True)
    ctx_write.add_argument("--agent", default=None)
    ctx_write.add_argument(
        "--level", required=True, choices=["L0", "L1", "L2"],
    )
    ctx_write.add_argument("--content", required=True, help="Note text")
    ctx_write.add_argument(
        "--tags", default="",
        help="Comma-separated tags (optional)",
    )

    ctx_plan = ctx_sub.add_parser(
        "plan", help="List plan steps or mark one done",
    )
    ctx_plan.add_argument("--session", required=True)
    ctx_plan.add_argument("--agent", default=None)
    ctx_plan.add_argument(
        "--status", choices=["pending", "in_progress", "done", "blocked"],
        default=None, help="Filter by status (default: all)",
    )
    ctx_plan.add_argument(
        "--mark-done", action="store_true",
        help="Mark a step done (requires --step-id)",
    )
    ctx_plan.add_argument(
        "--step-id", type=int, default=None,
        help="Plan step id (for --mark-done)",
    )

    ctx.set_defaults(func=_cmd_context)

    # Phase 1.6: auth subcommand for managing scope-gated API tokens.
    auth = sub.add_parser(
        "auth",
        help=(
            "Manage scope-gated API tokens (Phase 1.6). "
            "Tokens gate the /api/v1/* routes; see `harness auth --help`."
        ),
    )
    auth_sub = auth.add_subparsers(dest="auth_command")

    # ``harness auth create`` — mint a new token, print plaintext ONCE.
    auth_create = auth_sub.add_parser(
        "create",
        help="Create a new token. Prints the plaintext to stdout ONCE.",
    )
    auth_create.add_argument(
        "--label", required=True,
        help="Human-readable label for the token (e.g. 'opencode-mcp').",
    )
    auth_create.add_argument(
        "--scopes", default=None,
        help=(
            "Comma-separated scopes (e.g. 'agents.read,memory.read'). "
            f"Valid: {', '.join(s.value for s in __import__('harness.server.auth.scopes', fromlist=['Scope']).Scope)}. "
            "Defaults to settings.auth_default_scopes (empty if not set)."
        ),
    )
    auth_create.add_argument(
        "--bootstrap",
        action="store_true",
        help=(
            "Mint with ALL_SCOPES (admin token). Only intended for the "
            "implicit bootstrap path; explicitly minting a bootstrap "
            "token is allowed but the label should be unique."
        ),
    )
    # Note: no set_defaults(func=...) — see :func:`main`, the auth
    # sub-subcommands are dispatched in one pass by
    # :func:`_dispatch_auth` because the bootstrap path needs to
    # run before the user's command.

    # ``harness auth list`` — table of active tokens.
    auth_list = auth_sub.add_parser(
        "list",
        help="List active (non-revoked) tokens.",
    )

    # ``harness auth revoke <hash-or-label>`` — mark as revoked.
    auth_revoke = auth_sub.add_parser(
        "revoke",
        help="Revoke a token by its hash or by its label.",
    )
    auth_revoke.add_argument(
        "target",
        help="The token's hash (64 hex chars) or its label.",
    )

    # ``harness auth whoami <plaintext>`` — debug: show scopes for a token.
    auth_whoami = auth_sub.add_parser(
        "whoami",
        help="Show the scopes and metadata for a token (debug).",
    )
    auth_whoami.add_argument(
        "plaintext",
        help="The plaintext token (returned by `harness auth create`).",
    )

    # ``harness auth test <plaintext>`` — smoke-test a token against
    # the local server (calls /api/v1/capabilities with the token).
    auth_test = auth_sub.add_parser(
        "test",
        help="Test a token against the local /api/v1/capabilities endpoint.",
    )
    auth_test.add_argument(
        "plaintext",
        help="The plaintext token (returned by `harness auth create`).",
    )
    auth_test.add_argument(
        "--base-url", default="http://127.0.0.1:8765",
        help="Base URL of the harness server (default: %(default)s).",
    )

    # Phase 4.2+ v1.9.0: ``reload`` subcommand (force re-parse of
    # hot-reloadable resources without waiting for file events).
    reload_p = sub.add_parser(
        "reload",
        help=(
            "Force-reload hot-reloadable resources (Phase 4.2+ v1.9.0). "
            "Re-parses .harness/agents/*.md, .harness/hooks/*.json, and "
            ".harness/privacy/*.json locally (no server required). "
            "Use this after editing files when you want to validate them "
            "without restarting the server."
        ),
    )
    reload_sub = reload_p.add_subparsers(dest="reload_command")

    def _add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--project-root",
            default=None,
            help=(
                "Project root directory. Defaults to the current working "
                "directory. The watcher looks for ``.harness/`` subdirs here."
            ),
        )
        p.add_argument(
            "--json", action="store_true",
            help="Print results as JSON (machine-readable).",
        )
        p.set_defaults(func=_cmd_reload)

    all_p = reload_sub.add_parser(
        "all",
        help="Reload all hot-reloadable resources (default).",
    )
    _add_common(all_p)
    agents_p = reload_sub.add_parser(
        "agents",
        help="Re-parse .harness/agents/*.md (project overrides).",
    )
    _add_common(agents_p)
    hooks_p = reload_sub.add_parser(
        "hooks",
        help="Re-parse .harness/hooks/*.json.",
    )
    _add_common(hooks_p)
    privacy_p = reload_sub.add_parser(
        "privacy",
        help="Re-parse .harness/privacy/*.json.",
    )
    _add_common(privacy_p)
    # If no subcommand, default to "all" with the parent's flags.
    reload_p.set_defaults(func=_cmd_reload)

    # Phase 3 v1.4.0: ``sessions`` subcommand (manual /compact via CLI).
    sessions_p = sub.add_parser(
        "sessions",
        help=(
            "Session control (Phase 3 v1.4.0). "
            "See `harness sessions --help`."
        ),
    )
    sessions_sub = sessions_p.add_subparsers(dest="sessions_command")
    compact_p = sessions_sub.add_parser(
        "compact",
        help=(
            "Force-compact a session's context (manual /compact). "
            "Calls the running server's POST /api/v1/sessions/{id}/compact "
            "via HTTP — so the server must be running."
        ),
    )
    compact_p.add_argument(
        "--session", required=True,
        help="Session id to compact (UUID or any string).",
    )
    compact_p.add_argument(
        "--bypass-cache", action="store_true",
        help="Re-summarise even if a cached compact exists.",
    )
    compact_p.add_argument(
        "--base-url", default="http://127.0.0.1:8765",
        help="Base URL of the harness server (default: %(default)s).",
    )
    compact_p.set_defaults(func=_cmd_sessions_compact)

    # === Phase 4.4 v1.13.0: ``hooks`` subcommand (local inspection) ===
    from harness.cli_hooks import (
        _cmd_hooks_audit as _cmd_hooks_audit_impl,
        _cmd_hooks_dispatch as _cmd_hooks_dispatch_impl,
        _cmd_hooks_list as _cmd_hooks_list_impl,
        _cmd_hooks_show as _cmd_hooks_show_impl,
        _cmd_hooks_status as _cmd_hooks_status_impl,
    )
    # Phase 4.7 v1.17.0: live tail (--follow) variants.
    from harness.cli_follow import (
        cmd_hooks_audit_follow as _cmd_hooks_audit_follow_impl,
    )

    def _add_hooks_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--project-root", default=None,
            help=(
                "Project root directory (default: current working directory). "
                "The CLI looks for ``.harness/hooks/*.json`` here."
            ),
        )
        p.add_argument(
            "--json", action="store_true",
            help="Print results as JSON (machine-readable).",
        )

    hooks_p = sub.add_parser(
        "hooks",
        help=(
            "Inspect the hook registry (Phase 4.4 v1.13.0). "
            "Lists builtin + project hooks, shows one hook's spec, "
            "or summarises hot-reload status. Local — no server needed."
        ),
    )
    hooks_sub = hooks_p.add_subparsers(dest="hooks_command")

    hooks_list_p = hooks_sub.add_parser(
        "list",
        help="List all registered hooks (builtin + project).",
    )
    _add_hooks_common(hooks_list_p)
    hooks_list_p.add_argument(
        "--event", default=None,
        help=(
            "Comma-separated list of event names to include "
            "(e.g. 'PreToolUse,Elicitation'). Case-sensitive, "
            "matches EventType.value (PascalCase)."
        ),
    )
    hooks_list_p.add_argument(
        "--transport", default=None,
        help="Comma-separated list of transports: builtin,subprocess,http,llm.",
    )
    # Tri-state: ``--enabled`` shows only enabled hooks, ``--disabled``
    # shows only disabled hooks, neither shows all.
    enabled_group = hooks_list_p.add_mutually_exclusive_group()
    enabled_group.add_argument(
        "--enabled", action="store_const", const="yes", dest="enabled_flag",
        help="Show only enabled hooks.",
    )
    enabled_group.add_argument(
        "--disabled", action="store_const", const="no", dest="enabled_flag",
        help="Show only disabled hooks.",
    )
    hooks_list_p.set_defaults(func=_cmd_hooks_list_impl, enabled_flag=None)

    hooks_show_p = hooks_sub.add_parser(
        "show",
        help="Show full spec for one hook by id.",
    )
    _add_hooks_common(hooks_show_p)
    hooks_show_p.add_argument(
        "hook_id",
        help="Hook id (e.g. 'builtin.log', 'builtin.confirm_dangerous').",
    )
    hooks_show_p.set_defaults(func=_cmd_hooks_show_impl)

    hooks_status_p = hooks_sub.add_parser(
        "status",
        help="Local hot-reload status summary.",
    )
    _add_hooks_common(hooks_status_p)
    hooks_status_p.set_defaults(func=_cmd_hooks_status_impl)

    # Phase 4.5 v1.15.0: ``harness hooks dispatch <event>`` — fire an
    # event against the global registry and print the decision.
    hooks_dispatch_p = hooks_sub.add_parser(
        "dispatch",
        help=(
            "Fire a hook event and print the aggregate decision "
            "(Phase 4.5 v1.15.0). Useful for shell-based testing "
            "of hook configurations without the server."
        ),
    )
    _add_hooks_common(hooks_dispatch_p)
    hooks_dispatch_p.add_argument(
        "event",
        help=(
            "Hook event name (PascalCase, matches EventType.value). "
            "Examples: 'PreToolUse', 'OnRoutingDecision', 'OnCompaction'."
        ),
    )
    hooks_dispatch_p.add_argument(
        "--session", default="",
        help="Session id to attach to the HookContext (default: empty).",
    )
    hooks_dispatch_p.add_argument(
        "--agent", default="",
        help="Agent id to attach to the HookContext (default: empty).",
    )
    hooks_dispatch_p.add_argument(
        "--payload", default="{}",
        help=(
            "JSON object string for the event payload (default: '{}'). "
            "Example: --payload '{\"tool_name\": \"bash\"}'."
        ),
    )
    hooks_dispatch_p.set_defaults(func=_cmd_hooks_dispatch_impl)

    # Phase 4.6 v1.16.0: ``harness hooks audit`` — read the NDJSON
    # audit log from disk (today's UTC file by default).
    hooks_audit_p = hooks_sub.add_parser(
        "audit",
        help=(
            "Read the hook audit log (Phase 4.6 v1.16.0). "
            "Prints the last N entries from "
            "<project_root>/data/audit/hooks-YYYY-MM-DD.ndjson "
            "(today's UTC file). Apply filters with --event, "
            "--decision, --session, --since."
        ),
    )
    _add_hooks_common(hooks_audit_p)
    hooks_audit_p.add_argument(
        "--tail", type=int, default=50,
        help=(
            "Number of entries to show (default: 50). "
            "Set to 0 for all entries in today's file."
        ),
    )
    hooks_audit_p.add_argument(
        "--event", default=None,
        help=(
            "Filter by event name (exact match, case-sensitive). "
            "Examples: 'PreToolUse', 'OnRoutingDecision'."
        ),
    )
    hooks_audit_p.add_argument(
        "--decision", default=None,
        choices=["allow", "block", "modify"],
        help=(
            "Filter by aggregate final_decision. "
            "One of: allow, block, modify."
        ),
    )
    hooks_audit_p.add_argument(
        "--session", default=None,
        help="Filter by session_id (exact match).",
    )
    hooks_audit_p.add_argument(
        "--since", default=None,
        help=(
            "Show only entries with ts >= since (ISO-8601). "
            "Examples: '2026-06-17T00:00:00Z', "
            "'2026-06-17T12:00:00+00:00'."
        ),
    )
    # Phase 4.7 v1.17.0: live tail. When set, the command switches
    # to the follow implementation and ignores the snapshot-only
    # flags (``--tail``, ``--since`` are not applicable to a live
    # stream). ``--filter`` and ``--json`` are still honoured.
    hooks_audit_p.add_argument(
        "--follow", action="store_true",
        help=(
            "Live tail: open today's audit file at EOF and print each "
            "new entry as it is appended (Phase 4.7 v1.17.0). "
            "Polls every 250ms. Ctrl+C to exit. "
            "Combine with --filter REGEX (regex on the raw line) and "
            "--json (echo NDJSON verbatim)."
        ),
    )
    hooks_audit_p.add_argument(
        "--filter", default=None, metavar="REGEX",
        help=(
            "Regex filter. In --follow mode: applied via re.search to "
            "each raw audit line. In snapshot mode (Phase 4.7 v1.17.0): "
            "applied via re.search to the JSON-serialised entry, AFTER "
            "structured filters (--event/--decision/--session/--since). "
            "Invalid regex exits 1."
        ),
    )
    hooks_audit_p.add_argument(
        "--max-bytes", type=int, default=0,
        help=(
            "(--follow only) Cap the audit file size in bytes; when "
            "exceeded, the file is rotated to .1/.2/... (default 0 = "
            "no rotation)."
        ),
    )
    # Phase 4.12 v1.22.0: --follow improvements (batching + state).
    hooks_audit_p.add_argument(
        "--batch-size", type=int, default=0, dest="batch_size",
        help=(
            "(--follow only) Yield lines in batches of N. When set, "
            "switches to the Follower implementation (async, "
            "persistent state). Default 0 = legacy single-line path "
            "(settings.cli_follow_default_batch_size is used when "
            "--resume / --reset is set without --batch-size)."
        ),
    )
    hooks_audit_p.add_argument(
        "--resume", action="store_true",
        help=(
            "(--follow only) Continue from the byte offset saved by "
            "the previous --follow run (Phase 4.12 v1.22.0). State "
            "is stored under settings.cli_follow_state_dir."
        ),
    )
    hooks_audit_p.add_argument(
        "--reset", action="store_true",
        help=(
            "(--follow only) Ignore any saved state and start from "
            "byte 0 of the audit file (Phase 4.12 v1.22.0)."
        ),
    )

    def _dispatch_hooks_audit(a: argparse.Namespace) -> int:
        if getattr(a, "follow", False):
            return _cmd_hooks_audit_follow_impl(a)
        return _cmd_hooks_audit_impl(a)

    hooks_audit_p.set_defaults(func=_dispatch_hooks_audit)

    # If no subcommand, default to "list" with the parent's flags.
    hooks_p.set_defaults(func=_cmd_hooks_list_impl)

    # === Phase 4.4 v1.13.0: ``observability`` subcommand (local + HTTP) ===
    from harness.cli_observability import (
        _cmd_observability_health as _cmd_observability_health_impl,
        _cmd_observability_log as _cmd_observability_log_impl,
        _cmd_observability_metrics as _cmd_observability_metrics_impl,
        _cmd_observability_stats as _cmd_observability_stats_impl,
    )
    # Phase 4.7 v1.17.0: live metrics --follow (local snapshot diff).
    from harness.cli_follow import (
        cmd_observability_metrics_follow as _cmd_observability_metrics_follow_impl,
    )

    obs_p = sub.add_parser(
        "observability",
        help=(
            "Inspect the observability layer (Phase 4.4 v1.13.0). "
            "Tail the JSONL log, scrape /metrics, probe /health/*, "
            "or show the in-process counter snapshot."
        ),
    )
    obs_sub = obs_p.add_subparsers(dest="obs_command")

    obs_log_p = obs_sub.add_parser(
        "log",
        help=(
            "Tail the JSONL log file. Local read — no server required. "
            "Filter by --event (top-level event field), read a "
            "specific --date (UTC, YYYY-MM-DD)."
        ),
    )
    obs_log_p.add_argument(
        "--tail", type=int, default=20,
        help="Number of last lines to read (default 20).",
    )
    obs_log_p.add_argument(
        "--event", default=None,
        help="Comma-separated list of event names to include.",
    )
    obs_log_p.add_argument(
        "--date", default=None,
        help="UTC date in YYYY-MM-DD format (default: today UTC).",
    )
    obs_log_p.add_argument(
        "--max-bytes", type=int, default=1_048_576,
        help=(
            "Cap the file read to the last N bytes (default 1 MiB) "
            "to avoid OOM on long-running servers."
        ),
    )
    obs_log_p.add_argument(
        "--json", action="store_true",
        help="Print entries as a JSON array.",
    )
    obs_log_p.set_defaults(func=_cmd_observability_log_impl)

    obs_metrics_p = obs_sub.add_parser(
        "metrics",
        help=(
            "Scrape GET /metrics. Output is Prometheus text format; "
            "--filter is a regex on metric NAMES. "
            "(No --json — the wire format is not JSON.)"
        ),
    )
    obs_metrics_p.add_argument(
        "--base-url", default="http://127.0.0.1:8765",
        help="Base URL of the harness server (default: %(default)s).",
    )
    obs_metrics_p.add_argument(
        "--filter", default=None,
        help="Regex on metric names. HELP/TYPE for matches are kept.",
    )
    obs_metrics_p.add_argument(
        "--timeout-s", type=float, default=5.0,
        help="HTTP timeout in seconds (default 5).",
    )
    # Phase 4.7 v1.17.0: live tail of in-process metrics. Switches to
    # the local snapshot-diff implementation (no HTTP scrape). Useful
    # for observing counter changes in a long-lived CLI process or
    # when the server is not reachable.
    obs_metrics_p.add_argument(
        "--follow", action="store_true",
        help=(
            "Live tail: poll the in-process PrometheusMetrics.snapshot() "
            "every --interval-ms and print only changed counters/gauges "
            "(Phase 4.7 v1.17.0). No HTTP scrape. Ctrl+C to exit."
        ),
    )
    obs_metrics_p.add_argument(
        "--interval-ms", type=int, default=1000,
        help="(--follow only) Polling interval in ms (default 1000).",
    )
    obs_metrics_p.add_argument(
        "--json", action="store_true",
        help="(--follow only) Print each diff as a JSON object per line.",
    )
    # Phase 4.12 v1.22.0: --batch-size + --resume/--reset (parity with
    # hooks audit --follow). --batch-size buffers diffs into batches
    # of N entries before flushing. --resume/--reset are no-ops for
    # in-memory counters but accepted for CLI symmetry.
    obs_metrics_p.add_argument(
        "--batch-size", type=int, default=0, dest="batch_size",
        help=(
            "(--follow only) Buffer metric diffs into batches of N "
            "entries before flushing to stdout (Phase 4.12 v1.22.0). "
            "Default 0 = flush each diff immediately."
        ),
    )
    obs_metrics_p.add_argument(
        "--resume", action="store_true",
        help=(
            "(--follow only) Accepted for CLI parity with "
            "`hooks audit --follow` but a no-op for in-memory "
            "counters (Phase 4.12 v1.22.0)."
        ),
    )
    obs_metrics_p.add_argument(
        "--reset", action="store_true",
        help=(
            "(--follow only) Accepted for CLI parity with "
            "`hooks audit --follow` but a no-op for in-memory "
            "counters (Phase 4.12 v1.22.0)."
        ),
    )

    def _dispatch_observability_metrics(a: argparse.Namespace) -> int:
        if getattr(a, "follow", False):
            return _cmd_observability_metrics_follow_impl(a)
        return _cmd_observability_metrics_impl(a)

    obs_metrics_p.set_defaults(func=_dispatch_observability_metrics)

    obs_health_p = obs_sub.add_parser(
        "health",
        help=(
            "GET /health/{level} (live|ready|deep). "
            "Exit 0=ok, 1=degraded, 2=unhealthy/HTTP-error."
        ),
    )
    obs_health_p.add_argument(
        "--level", choices=["live", "ready", "deep"], default="deep",
        help="Health endpoint level (default deep).",
    )
    obs_health_p.add_argument(
        "--base-url", default="http://127.0.0.1:8765",
        help="Base URL of the harness server (default: %(default)s).",
    )
    obs_health_p.add_argument(
        "--timeout-s", type=float, default=5.0,
        help="HTTP timeout in seconds (default 5).",
    )
    obs_health_p.add_argument(
        "--json", action="store_true",
        help="Print raw report as JSON.",
    )
    obs_health_p.set_defaults(func=_cmd_observability_health_impl)

    obs_stats_p = obs_sub.add_parser(
        "stats",
        help=(
            "In-process PrometheusMetrics snapshot. The CLI starts "
            "fresh — counters are 0 unless incremented in this process. "
            "Use `harness observability metrics` for live server values."
        ),
    )
    obs_stats_p.add_argument(
        "--json", action="store_true",
        help="Print snapshot as JSON.",
    )
    obs_stats_p.add_argument(
        "--diff", nargs=2, metavar=("BEFORE", "AFTER"), default=None,
        help=(
            "Phase 4.7 v1.17.0: compare two JSON snapshots and print "
            "the per-metric delta. Each argument is a path to a file "
            "produced by `harness observability stats --json`. Exit 0 "
            "if no changes, exit 2 if any delta, exit 1 on read/parse "
            "error."
        ),
    )
    obs_stats_p.set_defaults(func=_cmd_observability_stats_impl)
    # === Phase 4.13B v1.23.0: ``observability webhooks dlq`` ===
    # Subcommand for listing + replaying the outbound webhook DLQ.
    # Reuses the same base-url / token / json flags as the other
    # observability subcommands for consistency.
    from harness.cli_observability import (
        _cmd_webhooks_dlq as _cmd_webhooks_dlq_impl,
    )
    obs_webhooks_p = obs_sub.add_parser(
        "webhooks",
        help=(
            "Outbound webhook admin (Phase 4.13B v1.23.0). "
            "Subcommand: dlq."
        ),
    )
    obs_webhooks_sub = obs_webhooks_p.add_subparsers(
        dest="webhooks_command"
    )
    obs_dlq_p = obs_webhooks_sub.add_parser(
        "dlq",
        help="List or replay outbound webhook DLQ entries.",
    )
    obs_dlq_sub = obs_dlq_p.add_subparsers(dest="dlq_action")
    # dlq list
    obs_dlq_list_p = obs_dlq_sub.add_parser(
        "list", help="List recent unreplayed DLQ entries (default).",
    )
    obs_dlq_list_p.add_argument(
        "--limit", type=int, default=100,
        help="Max entries (default 100, server caps at 1000).",
    )
    obs_dlq_list_p.add_argument(
        "--include-replayed", action="store_true",
        help="Also show already-replayed entries (audit history).",
    )
    obs_dlq_list_p.add_argument(
        "--base-url", default="http://127.0.0.1:8765",
        help="Harness server base URL (default %(default)s).",
    )
    obs_dlq_list_p.add_argument(
        "--token", default="",
        help="Admin API token (required when auth_required=True).",
    )
    obs_dlq_list_p.add_argument(
        "--json", action="store_true",
        help="Emit raw JSON response.",
    )
    obs_dlq_list_p.set_defaults(
        func=_cmd_webhooks_dlq_impl, dlq_action="list",
    )
    # dlq replay
    obs_dlq_replay_p = obs_dlq_sub.add_parser(
        "replay", help="Re-send a DLQ entry's payload with the current secret.",
    )
    obs_dlq_replay_p.add_argument(
        "dlq_id", type=int, help="DLQ entry id (from `dlq list`).",
    )
    obs_dlq_replay_p.add_argument(
        "--base-url", default="http://127.0.0.1:8765",
        help="Harness server base URL (default %(default)s).",
    )
    obs_dlq_replay_p.add_argument(
        "--token", default="",
        help="Admin API token (required when auth_required=True).",
    )
    obs_dlq_replay_p.add_argument(
        "--json", action="store_true",
        help="Emit raw JSON response.",
    )
    obs_dlq_replay_p.set_defaults(
        func=_cmd_webhooks_dlq_impl, dlq_action="replay",
    )
    # Default dlq action when bare `observability webhooks dlq`.
    obs_dlq_p.set_defaults(
        func=_cmd_webhooks_dlq_impl, dlq_action="list",
    )
    obs_webhooks_p.set_defaults(
        func=_cmd_webhooks_dlq_impl, dlq_action="list",
    )
    # If no subcommand, default to "log" (most common entry point).
    obs_p.set_defaults(func=_cmd_observability_log_impl)

    # === Phase 7.4 WI-04 v1.32.0: plugin install / uninstall ===
    plugins_p = sub.add_parser(
        "plugins",
        help=(
            "Install / uninstall plugins from the marketplace "
            "(Phase 7.4 v1.32.0)."
        ),
    )
    plugins_sub = plugins_p.add_subparsers(dest="plugins_command")

    # ``harness plugins install <name>``
    install_p = plugins_sub.add_parser(
        "install",
        help="Install a plugin from the marketplace.",
    )
    install_p.add_argument("plugin_name", help="Plugin name to install.")
    install_p.add_argument(
        "--marketplace-dir", default=None,
        help=(
            "Directory containing marketplace JSON manifests and .py "
            "sources. Default: <project_root>/.harness/marketplace."
        ),
    )
    install_p.add_argument(
        "--plugins-dir", default=None,
        help=(
            "Target plugins directory. Default: <project_root>/.harness/plugins "
            "(settings.plugins_dir)."
        ),
    )
    install_p.add_argument(
        "--trust-registry", default=None,
        help=(
            "Path to trust-registry.json for signature verification. "
            "Default: <project_root>/.harness/trust-registry.json."
        ),
    )
    install_p.set_defaults(func=_cmd_plugins_install)

    # ``harness plugins uninstall <name>``
    uninstall_p = plugins_sub.add_parser(
        "uninstall",
        help="Uninstall a loaded plugin.",
    )
    uninstall_p.add_argument("plugin_name", help="Plugin name to uninstall.")
    uninstall_p.add_argument(
        "--plugins-dir", default=None,
        help=(
            "Plugins directory where the .py file resides. "
            "Default: <project_root>/.harness/plugins."
        ),
    )
    uninstall_p.set_defaults(func=_cmd_plugins_uninstall)

    # If no subcommand, dispatch to the handler (which prints usage).
    plugins_p.set_defaults(func=_cmd_plugins)

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

    # Phase 1.6: the auth subparser doesn't have a single func — it
    # has a sub-subcommand, and the dispatcher needs the parsed args
    # to know which to invoke. We rewrite the func here.
    if args.command == "auth":
        return _dispatch_auth(args)

    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
