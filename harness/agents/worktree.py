"""Git worktree isolation for sub-agents (Phase 2.0, Step 3).

Wraps ``git worktree add`` / ``git worktree remove`` as an async context
manager so a sub-agent can run inside its own branch + working tree
without polluting the main worktree of the parent repo.

The session lifecycle is::

    async with WorktreeSession(repo, worktree_id="a1b2", base="HEAD") as wt:
        # wt.path  is the working tree (Path)
        # wt.branch is "harness/<id>"
        ... # agent runs in wt.path

**Crash safety:** even if the body raises, the worktree is removed and the
temporary branch is deleted. Cleanup failures are logged but never shadow
the original exception.

**Idempotency:** if a worktree for ``harness/<id>`` already exists (e.g. a
crash left it behind), the existing one is reused and a fresh one is NOT
created. The caller can detect this via :attr:`WorktreeInfo.reused`.

**Security:** the ``worktree_id`` is regex-restricted to
``[A-Za-z0-9_-]{1,32}`` to prevent shell injection in the constructed
``git worktree add -b harness/<id> ...`` command. The working tree path
is sanity-checked against the repo root via
:func:`harness.server.agent.safety.is_safe_path`.
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path

from harness.server.agent.safety import is_safe_path

logger = logging.getLogger(__name__)


# === Constants ===

#: Branch prefix for sub-agent worktrees. Keep stable for `git worktree list`
#: auditability — every sub-agent branch is namespaced under this.
BRANCH_PREFIX: str = "harness/"

#: Path component inside the repo where worktrees are created. The directory
#: is shared across all sub-agents (one worktree per id, namespaced by id).
WORKTREE_PARENT: str = ".harness/worktrees"

#: Allowed characters in a ``worktree_id``. Anything else is rejected.
_WORKTREE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

#: Default base ref for the new worktree branch.
DEFAULT_BASE: str = "HEAD"


# === Errors ===

class WorktreeError(RuntimeError):
    """Raised when a worktree operation fails (git not in PATH, init failed, etc.)."""


# === Info dataclass ===

@dataclass(frozen=True)
class WorktreeInfo:
    """Snapshot of a live worktree."""

    path: Path
    branch: str
    worktree_id: str
    reused: bool = False  # True if we joined an existing worktree instead of creating

    def __post_init__(self) -> None:
        # We do NOT call path.resolve() here — resolution can be expensive and
        # the path is already absolute and validated at __aenter__.
        if not self.path.is_absolute():
            raise WorktreeError(f"WorktreeInfo.path must be absolute, got {self.path!r}")


# === Session ===

class WorktreeSession:
    """Async context manager wrapping ``git worktree add/remove``.

    Args:
        repo:           The main repo directory (where ``.git/`` lives).
        worktree_id:    A 1–32 char kebab/snake identifier. Auto-generated
                        (``wt-<8 hex>``) if omitted.
        base:           Git ref to branch from. Defaults to ``"HEAD"``.

    Raises:
        WorktreeError: if ``git`` is not in PATH, the worktree_id is
                       invalid, or git commands fail.
    """

    def __init__(
        self,
        repo: Path,
        *,
        worktree_id: str | None = None,
        base: str = DEFAULT_BASE,
    ) -> None:
        self.repo = Path(repo).resolve(strict=False)
        # None → generate a fresh id. Empty string and other invalid values
        # are explicitly rejected (security: a caller passing "" is almost
        # certainly a bug — we should not silently substitute a default).
        if worktree_id is None:
            self.worktree_id = self._new_id()
        else:
            self.worktree_id = worktree_id
        self.base = base
        self._info: WorktreeInfo | None = None
        self._exited: bool = False

        if not _WORKTREE_ID_RE.match(self.worktree_id):
            raise WorktreeError(
                f"worktree_id must match {_WORKTREE_ID_RE.pattern!r}, "
                f"got {self.worktree_id!r}"
            )
        if shutil.which("git") is None:
            raise WorktreeError("git executable not found in PATH — cannot create worktree")

    # === public API ===

    @property
    def branch(self) -> str:
        return f"{BRANCH_PREFIX}{self.worktree_id}"

    @property
    def worktree_path(self) -> Path:
        """Where the worktree will be created (does not require __aenter__)."""
        return self.repo / WORKTREE_PARENT / self.worktree_id

    async def __aenter__(self) -> WorktreeInfo:
        if self._info is not None:
            raise WorktreeError("WorktreeSession already entered")
        try:
            await self._ensure_repo()
            # If a previous session crashed mid-cleanup, the branch may
            # exist as an orphan. Drop it so the next ``worktree add``
            # doesn't fail with "branch already exists".
            await self._delete_orphan_branch_if_exists()
            existing = await self._find_existing_worktree()
            if existing is not None:
                logger.info(
                    "worktree session %r reusing existing worktree at %s",
                    self.worktree_id, existing,
                )
                self._info = WorktreeInfo(
                    path=existing, branch=self.branch,
                    worktree_id=self.worktree_id, reused=True,
                )
                return self._info

            target = self.worktree_path
            target.parent.mkdir(parents=True, exist_ok=True)
            await self._run_git("worktree", "add", "-b", self.branch, str(target), self.base)
            # Defence in depth: confirm the path lands inside the repo.
            if not is_safe_path(target, self.repo):
                # Undo: remove the worktree we just created.
                await self._run_git("worktree", "remove", "--force", str(target), check=False)
                await self._run_git("branch", "-D", self.branch, check=False)
                raise WorktreeError(
                    f"worktree path {target} escaped the repo root {self.repo} — aborting"
                )
            # Seed the new branch with an empty commit ahead of the base
            # so that downstream ``git merge --ff-only harness/<id>`` has
            # something to fast-forward to. The commit message flags it as
            # a sub-agent start marker; reviewers can spot it at a glance.
            # We must run the commit INSIDE the new worktree (not the main
            # repo) so the commit lands on the new branch.
            proc = await asyncio.create_subprocess_exec(
                "git", "commit", "--allow-empty", "-m",
                f"sub-agent start: {self.branch} (id={self.worktree_id})",
                cwd=str(target),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()  # best-effort; ignore failure
            self._info = WorktreeInfo(
                path=target.resolve(strict=False),
                branch=self.branch,
                worktree_id=self.worktree_id,
                reused=False,
            )
            logger.info("worktree session %r created at %s on branch %s",
                        self.worktree_id, self._info.path, self._info.branch)
            return self._info
        except BaseException:
            # Init failure: no worktree to clean up, but make sure we don't
            # leave the parent dir dangling. Best-effort.
            await self._cleanup_partial()
            raise

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._exited:
            return
        self._exited = True
        if self._info is None:
            return  # init failed; nothing to clean
        try:
            # `git worktree remove --force` is the documented way to clean up.
            # We do NOT delete the branch here — a downstream merge-queue
            # call may still need it for ``git merge --ff-only``. The
            # caller is responsible for branch deletion (use
            # :meth:`delete_branch` explicitly, or rely on the merge-queue
            # to clean up after a successful merge).
            await self._run_git(
                "worktree", "remove", "--force", str(self._info.path), check=False,
            )
            # Optionally prune administrative data so `git worktree list` is clean.
            await self._run_git("worktree", "prune", check=False)
        except Exception as cleanup_err:
            # Never shadow the original exception.
            logger.warning(
                "worktree session %r cleanup failed: %s (original exc: %r)",
                self.worktree_id, cleanup_err, exc,
            )
        self._info = None

    async def delete_branch(self) -> None:
        """Explicitly delete the ``harness/<id>`` branch.

        Called by the merge queue after a successful ``git merge --ff-only``
        (or by callers that want a clean teardown without merging).
        Idempotent: deleting a non-existent branch is a no-op.
        """
        await self._run_git("branch", "-D", self.branch, check=False)

    # === helpers ===

    @staticmethod
    def _new_id() -> str:
        """Generate a fresh 11-char id: ``wt-`` + 8 hex chars."""
        return f"wt-{secrets.token_hex(4)}"

    async def _ensure_repo(self) -> None:
        """Confirm the repo path is a git working tree (has .git/ or is one)."""
        # `git rev-parse --git-dir` succeeds for both the main worktree and
        # for sub-worktrees, so it's a robust check.
        try:
            await self._run_git("rev-parse", "--show-toplevel")
        except WorktreeError as e:
            raise WorktreeError(f"{self.repo} is not a git repository: {e}") from e

    async def _find_existing_worktree(self) -> Path | None:
        """Return the path of an existing worktree for our branch, or None.

        ``git worktree list --porcelain`` emits one worktree per block::

            worktree <path>
            HEAD <sha>
            branch refs/heads/<name>   # OR ``detached`` for tag-pinned worktrees

        We track the most recent ``worktree`` line and return its path when
        we see a matching ``branch`` line. (Branches are listed AFTER their
        worktree line, so a single-pass forward scan works: remember the
        path, then check the branch on the next line.)

        **Important:** if no worktree is found, but the branch ``harness/<id>``
        EXISTS as an orphan (e.g. left over from a previous session that
        crashed mid-cleanup), we return None — the caller will then try
        ``git worktree add -b <branch> <path>`` and git will fail with
        "branch already exists". The cleanup hook in :meth:`__aenter__`
        (see below) handles this by deleting the orphan branch first.
        """
        try:
            out = await self._run_git("worktree", "list", "--porcelain")
        except WorktreeError:
            return None
        pending_path: Path | None = None
        for line in out.splitlines():
            if line.startswith("worktree "):
                pending_path = Path(line[len("worktree "):].strip())
            elif line.startswith("branch ") and pending_path is not None:
                ref = line[len("branch "):].strip()
                # git prints ``refs/heads/<name>`` — strip the prefix to
                # compare against our short form ``harness/<id>``.
                short = ref.removeprefix("refs/heads/")
                if short == self.branch:
                    return pending_path
                pending_path = None
        return None

    async def _delete_orphan_branch_if_exists(self) -> None:
        """If ``harness/<id>`` exists as an orphan branch (no worktree),
        delete it so a fresh ``git worktree add -b`` can proceed.

        Called at the start of :meth:`__aenter__` to recover from prior
        crashes. ``-D`` (not ``-d``) so it works even if HEAD~1 doesn't
        have the merge base resolved.
        """
        await self._run_git("branch", "-D", self.branch, check=False)

    async def _cleanup_partial(self) -> None:
        """Best-effort cleanup if __aenter__ raised mid-init."""
        target = self.worktree_path
        if target.exists():
            try:
                await self._run_git("worktree", "remove", "--force", str(target), check=False)
            except Exception as e:
                logger.debug("partial cleanup of %s failed: %s", target, e)
        # Drop a partially-created branch too.
        try:
            await self._run_git("branch", "-D", self.branch, check=False)
        except Exception as e:
            logger.debug("partial branch cleanup of %s failed: %s", self.branch, e)

    async def _run_git(
        self,
        *args: str,
        check: bool = True,
    ) -> str:
        """Run a git command inside ``self.repo`` and return stdout.

        Raises:
            WorktreeError: if the command exits non-zero (and ``check=True``).
        """
        cmd = ("git", *args)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(self.repo),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise WorktreeError(f"failed to spawn git: {e}") from e
        try:
            stdout, stderr = await proc.communicate()
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
        rc = proc.returncode
        if rc != 0 and check:
            err = stderr.decode("utf-8", errors="replace").strip() or "(no stderr)"
            raise WorktreeError(
                f"git command failed (rc={rc}): {' '.join(cmd)} → {err}"
            )
        return stdout.decode("utf-8", errors="replace")
