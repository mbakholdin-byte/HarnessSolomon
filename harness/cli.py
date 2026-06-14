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
