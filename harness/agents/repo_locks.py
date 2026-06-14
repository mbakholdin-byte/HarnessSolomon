"""Per-repo asyncio.Lock registry (Phase 2.2, Step 1).

Phase 2.0+2.1 used a single global :class:`asyncio.Lock` to serialise
merge-queue jobs. That serialised everything, including jobs targeting
*different* repos. Phase 2.2 introduces a per-repo registry so that
two jobs in different repos can run in parallel while jobs in the
same repo still serialise (because the worktree + git operations
are not safe to run concurrently inside one repo).

The registry is a tiny process-local structure: a dict keyed by the
absolute path of the repo, with one :class:`asyncio.Lock` per entry.
A guard lock protects insertion (microsecond scope) so concurrent
first-touches of the same repo don't both create a fresh Lock.

Public API
----------

- :class:`RepoLockRegistry` — the registry itself.
- :class:`_AsyncContextManagerProxy` — adapter exposing a Lock as an
  ``async with`` context (so callers write ``async with registry.lock_for(repo):``
  exactly like they used to write ``async with self._lock:``).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import TracebackType
from typing import Self


class _AsyncContextManagerProxy:
    """Tiny adapter so ``registry.lock_for(repo)`` returns an awaitable
    async-context manager.

    Usage::

        async with registry.lock_for(repo):
            await do_work(repo)

    Implemented as a small wrapper that holds a reference to the
    underlying :class:`asyncio.Lock` and proxies ``__aenter__`` /
    ``__aexit__`` to it. We do NOT subclass ``asyncio.Lock`` because
    Lock's ``acquire()`` semantics don't compose cleanly with the
    ``async with`` statement in all Python versions.
    """

    __slots__ = ("_lock",)

    def __init__(self, lock: asyncio.Lock) -> None:
        self._lock = lock

    async def __aenter__(self) -> Self:
        await self._lock.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self._lock.release()


class RepoLockRegistry:
    """Per-repo :class:`asyncio.Lock` registry.

    Lookups are by ``str(Path(repo).resolve())`` so the same repo
    accessed via different relative paths (or with/without trailing
    slash) maps to the same lock. ``Path.resolve(strict=False)`` is
    used so the lookup never raises on Windows for a non-existent
    path (Phase 2.2 jobs may be enqueued for a repo that doesn't
    exist yet on disk — the worktree creation is the first thing
    that touches the filesystem inside the lock).

    Thread-safety: the registry is meant to be used from a single
    asyncio event loop, like the rest of the merge queue. The
    ``_guard`` lock serialises dict insertion (microsecond scope);
    we never hold a per-repo lock while mutating the dict.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        # ``_guard`` is held only during dict insertion; never held
        # during the actual job run. This is the standard pattern for
        # memoising asyncio.Lock instances safely.
        self._guard: asyncio.Lock = asyncio.Lock()

    @staticmethod
    def _key(repo: Path) -> str:
        """Normalise a repo path to a registry key.

        ``Path.resolve(strict=False)`` is preferred over ``str()``
        so that symlinks / mixed slashes / ``..`` segments all
        collapse to the same canonical form.
        """
        return str(Path(repo).resolve(strict=False))

    async def get(self, repo: Path) -> asyncio.Lock:
        """Return the :class:`asyncio.Lock` for ``repo``, creating on first touch.

        The returned lock is owned by the registry — callers MUST NOT
        call ``acquire()`` directly (use :meth:`lock_for` instead).
        The lock is the same instance across calls for the same repo,
        so serialisation is preserved.
        """
        key = self._key(repo)
        existing = self._locks.get(key)
        if existing is not None:
            return existing
        # Slow path: create a fresh lock. Hold the guard so two
        # concurrent first-touches don't both insert.
        async with self._guard:
            existing = self._locks.get(key)
            if existing is not None:
                return existing
            lock = asyncio.Lock()
            self._locks[key] = lock
            return lock

    def lock_for(self, repo: Path) -> _AsyncContextManagerProxy:
        """Return an async context manager for the per-repo lock.

        Usage::

            async with registry.lock_for(repo):
                await do_work(repo)

        The returned object is cheap (no await), so it's safe to call
        outside an async function as long as you only ``__aenter__``
        it from inside one. The actual lock acquisition happens on
        ``__aenter__``.
        """
        # We deliberately do NOT call ``self.get`` here (which is
        # async). Instead, look up or synchronously create. The race
        # window for "two concurrent first-touches of the same repo"
        # is harmless — both would create their own lock and use it
        # for one acquisition each, and the SECOND one would be
        # orphaned in the dict (never reused). The next caller for
        # the same repo would then hit the orphaned lock. To avoid
        # this, we use a synchronous insertion guarded by an
        # ``asyncio.Lock``-free path: since asyncio is single-threaded,
        # two concurrent calls to ``lock_for`` for a fresh key can't
        # happen at the dict-mutation step (no ``await`` between the
        # check and the insert). So a plain dict assignment is safe.
        key = self._key(repo)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return _AsyncContextManagerProxy(lock)

    def stats(self) -> dict[str, int]:
        """Return a snapshot of the registry for ops monitoring.

        Returns ``{repo_key: queue_depth}`` where ``queue_depth`` is
        the number of waiters currently blocked on that lock. The
        shape is intentionally minimal — callers (e.g.
        ``GET /api/v1/agents/health``) can format it.

        We do NOT inspect the underlying :class:`asyncio.Lock` for
        its internal queue (that's not part of the public API and
        differs across CPython versions). Instead we report 0 for
        every registered repo; an operator who wants true depth can
        wrap ``lock_for`` in their own counter.
        """
        return {key: 0 for key in sorted(self._locks)}

    async def aclose(self) -> None:
        """Clear all locks. Used by tests; production lifetime is the process.

        Locks that are currently held are NOT released (we can't
        safely do that without breaking the owner). They are simply
        dropped from the dict, so any future ``lock_for`` call
        starts a fresh lock. In practice, this is only called between
        tests, never while a job is in flight.
        """
        async with self._guard:
            self._locks.clear()


__all__ = ["RepoLockRegistry", "_AsyncContextManagerProxy"]
