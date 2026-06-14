"""SplitPlanner — split a job's diff into N PR slices (Phase 2.4, Step 0).

Phase 2.2 / 2.3 enforced a 1-job-1-PR model. Phase 2.4 introduces **stacked
PRs**: 1 task = N dependent PRs. PR-B's ``base_branch`` is PR-A's branch
(GitHub stacked-PR convention), so PR-B is automatically rebased onto
PR-A's branch when PR-A is merged.

This module owns the **planning** step: given a list of changed files,
decide how to group them into slices. The actual orchestration (creating
branches, calling ``gh pr create``, waiting for merges) lives in
:meth:`harness.agents.merge_queue.MergeQueue._run_stack_phase`.

**Design constraints:**

- **Pure functions, no I/O.** ``plan_splits`` takes a list of files and
  returns a list of :class:`SplitSlice`. It does not run ``git`` or
  touch the database. The orchestrator (caller) is responsible for
  building the file list (typically via ``git diff --name-only``) and
  persisting the slice jobs.

- **Stable ordering.** Slices are returned in ``stack_position`` order
  (0-indexed). Within a slice, files are sorted alphabetically. The
  planner is deterministic — same input → same output.

- **Backward compat.** A diff with 0 or 1 file returns a single
  ``SplitSlice`` (position 0, branch ``harness/<worktree_id>``). This
  preserves the Phase 2.2 / 2.3 single-PR path.

**Strategies (4):**

- ``auto`` (default): if ``len(files) <= max_files_per_slice``, return
  a single slice; else fall back to ``directory``. Keeps simple tasks
  as single PRs without forcing operators to choose.

- ``files``: round-robin into N slices, each capped at
  ``max_files_per_slice`` files. Predictable, ignores directory
  boundaries.

- ``directory``: group by the first path component (e.g. ``src/``,
  ``tests/``). Files at the repo root (no ``/``) go in their own slice.
  Most "natural" split for typical code/test/docs layouts.

- ``size``: balance by line count. The orchestrator must supply
  ``file_locs`` (a mapping from path → line count, typically from
  ``git diff --shortstat`` or ``wc -l``). The planner uses a greedy
  LPT (longest-processing-time-first) algorithm for load balancing.

**Limits** (enforced in :func:`plan_splits`):

- ``pr_split_min_slices``: if the diff is too small for the requested
  number of slices, collapse to a single slice. (E.g. ``--split-into 5``
  with 3 files → 1 slice, not 5 empty slices.)
- ``pr_split_max_slices``: hard cap. ``--split-into 100`` is clamped to
  this value.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

# === Result dataclass ===

@dataclass(frozen=True)
class SplitSlice:
    """One slice of a stacked PR job.

    Attributes:
        position: 0-based position in the stack. ``0`` is the first
            slice; its PR targets ``pr_target_branch`` (usually
            ``main``). ``1`` targets slice 0's branch, etc.
        files: Files in this slice (relative to repo root). Already
            committed to ``branch_name`` by the orchestrator.
        branch_name: The local branch the slice lives on, e.g.
            ``harness/<worktree_id>/step-0``. Pushed to ``origin`` by
            the orchestrator before ``gh pr create`` is called.
        title: Suggested PR title. Default convention
            ``"harness: {task[:80]} (step N/M)"``. The orchestrator
            may override.
    """

    position: int
    files: list[str]
    branch_name: str
    title: str

    def __post_init__(self) -> None:
        # Sort files in-place for determinism. ``object.__setattr__``
        # is needed because the dataclass is frozen.
        object.__setattr__(self, "files", sorted(self.files))


# === Public API ===

def plan_splits(
    *,
    diff_files: list[str],
    strategy: str,
    worktree_id: str,
    task: str,
    n_slices: int | None = None,
    max_files_per_slice: int = 10,
    min_slices: int = 1,
    max_slices: int = 8,
    file_locs: dict[str, int] | None = None,
) -> list[SplitSlice]:
    """Plan how to split ``diff_files`` into N PR slices.

    Args:
        diff_files: List of changed file paths (relative to repo root),
            as returned by ``git diff --name-only <base>``. May be empty.
        strategy: One of ``"auto"``, ``"files"``, ``"directory"``,
            ``"size"``. See module docstring.
        worktree_id: Stable id of the worktree (used to build branch
            names like ``harness/<id>/step-0``).
        task: Human-readable task description; used to build PR titles
            (truncated to 80 chars).
        n_slices: Explicit slice count for ``files`` / ``size`` /
            ``directory`` strategies. ``None`` = planner chooses.
            Ignored by ``auto`` (which uses ``max_files_per_slice``).
        max_files_per_slice: Cap per slice for ``files`` / ``auto``.
        min_slices: If the resulting slice count would be below this,
            collapse to a single slice.
        max_slices: Hard upper bound. ``n_slices`` is clamped to this.
        file_locs: Optional mapping from file path → line count (for
            the ``size`` strategy). If absent, ``size`` falls back to
            ``files`` (treats all files as equal weight).

    Returns:
        A list of :class:`SplitSlice` (length 1 to ``max_slices``).
        Length 1 = single-PR fallback (Phase 2.2/2.3 path).

    Raises:
        ValueError: If ``strategy`` is not one of the 4 known values.
    """
    # Defensive: empty diff → 1 empty slice (orchestrator can decide
    # whether to abort or fall back to a no-op).
    if not diff_files:
        return [_empty_slice(worktree_id, task)]

    # Strip duplicates, sort (deterministic output).
    files = sorted(set(diff_files))

    # Decide actual strategy.
    effective_strategy = _resolve_strategy(
        strategy=strategy,
        n_files=len(files),
        max_files_per_slice=max_files_per_slice,
        n_slices=n_slices,
    )

    # Compute slice count. ``auto`` returns 1 by definition when below
    # the per-slice cap; otherwise delegates to ``directory``.
    if effective_strategy == "files":
        n = _ceil_slices(
            len(files), max_files_per_slice,
            n_slices=n_slices, min_slices=min_slices,
            max_slices=max_slices,
        )
        groups = _split_by_count(files, n)
    elif effective_strategy == "directory":
        groups = _split_by_directory(files)
        n = len(groups)
        n = _clamp(n, lo=1, hi=max_slices)
        # Apply explicit n_slices cap if larger than what directory gives
        # (rare — directory usually produces fewer groups than asked).
        if n_slices is not None and n < n_slices:
            n = n
        groups = groups[:n]
    elif effective_strategy == "size":
        n = _ceil_slices(
            len(files), max_files_per_slice,
            n_slices=n_slices, min_slices=min_slices,
            max_slices=max_slices,
        )
        groups = _split_by_size(files, n, file_locs or {})
    else:
        # Should be unreachable (resolved above).
        raise ValueError(f"unknown strategy: {strategy!r}")

    # If only 1 group → single-slice path (Phase 2.2/2.3 back-compat).
    if len(groups) <= 1:
        return [_make_slice(
            position=0, files=groups[0] if groups else files,
            worktree_id=worktree_id, task=task, slice_total=1,
        )]

    # Build SplitSlice objects.
    total = len(groups)
    return [
        _make_slice(
            position=i, files=group, worktree_id=worktree_id,
            task=task, slice_total=total,
        )
        for i, group in enumerate(groups)
    ]


# === Internal helpers ===

def _resolve_strategy(
    *,
    strategy: str,
    n_files: int,
    max_files_per_slice: int,
    n_slices: int | None,
) -> str:
    """Map ``auto`` to a concrete strategy based on file count.

    Rules:

    - Explicit ``files`` / ``directory`` / ``size`` → use as-is.
    - ``auto``:
      - If ``n_files <= max_files_per_slice`` → ``files`` (which will
        then produce 1 slice).
      - Else → ``directory`` (most "natural" grouping for medium
        diffs; user can override with ``--split-strategy files``).
    """
    if strategy in ("files", "directory", "size"):
        return strategy
    if strategy == "auto":
        if n_files <= max_files_per_slice:
            return "files"  # collapses to 1 slice
        return "directory"
    raise ValueError(
        f"pr_split_strategy must be one of 'auto' / 'files' / "
        f"'directory' / 'size', got {strategy!r}"
    )


def _ceil_slices(
    n_files: int,
    max_files_per_slice: int,
    *,
    n_slices: int | None,
    min_slices: int,
    max_slices: int,
) -> int:
    """Decide how many slices to produce for ``files`` / ``size``.

    Precedence:
      1. ``n_slices`` if explicitly given → clamp to ``max_slices``.
      2. Else: ``ceil(n_files / max_files_per_slice)``.
      3. After computing, if below ``min_slices`` → ``min_slices``
         (but capped at the number of files — can't have more slices
         than files).

    Note: ``min_slices`` here is mostly a safety net (the orchestrator
    can short-circuit on the result).
    """
    if n_slices is not None:
        n = max(1, int(n_slices))
    else:
        n = max(1, math.ceil(n_files / max_files_per_slice))
    n = min(n, max_slices, n_files)  # never more slices than files
    n = max(n, min(min_slices, n_files))
    return n


def _split_by_count(files: list[str], n: int) -> list[list[str]]:
    """Round-robin split: take every Nth file into the same bucket.

    Example: ``files=[a,b,c,d,e,f], n=2`` →
    ``[[a,c,e], [b,d,f]]``. This balances sizes when files differ
    wildly in count of lines.
    """
    if n <= 1:
        return [list(files)]
    out: list[list[str]] = [[] for _ in range(n)]
    for i, f in enumerate(files):
        out[i % n].append(f)
    # Drop empty buckets (e.g. 5 files into 4 slices → 1 empty bucket).
    return [g for g in out if g]


def _split_by_directory(files: list[str]) -> list[list[str]]:
    """Group files by their first path component.

    Example: ``[src/a.py, src/b.py, tests/t.py, docs/d.md]`` →
    ``[[src/a.py, src/b.py], [tests/t.py], [docs/d.md]]``.

    Files at the repo root (no ``/``) go in a synthetic ``"(root)"``
    bucket that is sorted alphabetically with the other buckets.

    The ``_`` prefix on the bucket name is a convention that keeps
    root files at the top of the ordering (a stable, predictable
    position for slice 0).
    """
    buckets: dict[str, list[str]] = defaultdict(list)
    for f in files:
        if "/" in f:
            top = f.split("/", 1)[0]
        else:
            top = "(root)"
        buckets[top].append(f)
    # Sort by bucket name; root bucket first (stable, predictable).
    sorted_keys = sorted(buckets.keys(), key=lambda k: (k != "(root)", k))
    return [sorted(buckets[k]) for k in sorted_keys]


def _split_by_size(
    files: list[str],
    n: int,
    file_locs: dict[str, int],
) -> list[list[str]]:
    """Balance by line count using a greedy LPT algorithm.

    For each file, in descending LOC order, assign to the slice with
    the smallest current total. LPT gives a 4/3-approximation of the
    optimal makespan (a classic result for multiprocessor scheduling).

    If ``file_locs`` is empty (no LOC info), all files are treated
    as weight 1 → behaves like ``_split_by_count``.

    Example: files=[a(100),b(50),c(50),d(10)], n=2 →
    LPT order [a, b, c, d]:
      - a→slice 0 (total 100)
      - b→slice 1 (total 50)  # smaller
      - c→slice 0 (total 150) # smaller
      - d→slice 1 (total 60)  # smaller
    Result: [[a, c], [b, d]] (total 150 vs 60).
    """
    if n <= 1:
        return [list(files)]
    if not file_locs:
        return _split_by_count(files, n)

    # Sort by LOC desc; tie-break by path for determinism.
    weighted = sorted(
        files,
        key=lambda f: (-file_locs.get(f, 0), f),
    )
    buckets: list[list[str]] = [[] for _ in range(n)]
    totals: list[int] = [0] * n
    for f in weighted:
        # Pick the bucket with the smallest current total.
        idx = totals.index(min(totals))
        buckets[idx].append(f)
        totals[idx] += file_locs.get(f, 0)
    # Drop empty buckets (e.g. 5 files into 6 slices).
    return [g for g in buckets if g]


def _clamp(n: int, *, lo: int, hi: int) -> int:
    return max(lo, min(n, hi))


def _make_slice(
    *,
    position: int,
    files: list[str],
    worktree_id: str,
    task: str,
    slice_total: int,
) -> SplitSlice:
    """Construct a :class:`SplitSlice` with conventional defaults.

    Branch name convention: ``harness/<worktree_id>/step-<N>``. Title
    convention: ``"harness: {task[:80]} (step N/M)"``.
    """
    return SplitSlice(
        position=position,
        files=sorted(files),
        branch_name=f"harness/{worktree_id}/step-{position}",
        title=f"harness: {task[:80]} (step {position + 1}/{slice_total})",
    )


def _empty_slice(worktree_id: str, task: str) -> SplitSlice:
    """The canonical "no diff" placeholder slice (position 0, no files)."""
    return SplitSlice(
        position=0,
        files=[],
        branch_name=f"harness/{worktree_id}/step-0",
        title=f"harness: {task[:80] or '(no changes)'} (step 1/1)",
    )


__all__ = [
    "SplitSlice",
    "plan_splits",
]
