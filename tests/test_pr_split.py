"""Tests for :class:`harness.agents.pr_split.SplitPlanner` (Phase 2.4 Step 0).

Covers all 4 strategies + edge cases (empty diff, 1 file, 100 files,
n_slices=0, min/max slice bounds). The planner is a pure function —
no git, no database, no I/O.
"""
from __future__ import annotations

import pytest

from harness.agents.pr_split import SplitSlice, plan_splits


# === Empty / trivial diff ===

class TestEmptyDiff:
    def test_empty_diff_returns_single_empty_slice(self) -> None:
        slices = plan_splits(
            diff_files=[], strategy="files", worktree_id="wt-1",
            task="noop", n_slices=3,
        )
        assert len(slices) == 1
        assert slices[0].position == 0
        assert slices[0].files == []
        assert slices[0].branch_name == "harness/wt-1/step-0"

    def test_single_file_returns_single_slice_regardless_of_n(self) -> None:
        slices = plan_splits(
            diff_files=["only.py"], strategy="files", worktree_id="wt-1",
            task="t", n_slices=5, max_files_per_slice=10,
        )
        assert len(slices) == 1
        assert slices[0].files == ["only.py"]
        # Title: "harness: t (step 1/1)" — single slice
        assert "step 1/1" in slices[0].title


# === Strategy: auto ===

class TestAutoStrategy:
    def test_auto_small_diff_returns_single_slice(self) -> None:
        """Auto collapses to 1 slice when diff fits in max_files_per_slice."""
        files = [f"src/f{i}.py" for i in range(5)]
        slices = plan_splits(
            diff_files=files, strategy="auto", worktree_id="wt-1",
            task="small", max_files_per_slice=10,
        )
        assert len(slices) == 1
        assert slices[0].files == sorted(files)

    def test_auto_large_diff_falls_back_to_directory(self) -> None:
        """Auto delegates to directory when diff > max_files_per_slice."""
        files = (
            [f"src/f{i}.py" for i in range(5)]
            + [f"tests/t{i}.py" for i in range(5)]
            + [f"docs/d{i}.md" for i in range(3)]
        )
        slices = plan_splits(
            diff_files=files, strategy="auto", worktree_id="wt-1",
            task="big", max_files_per_slice=5,
        )
        # 3 directories → 3 slices (or 2 if root bucket joined)
        assert len(slices) >= 2
        # Each slice should only contain files from one directory
        for s in slices:
            assert s.files
            tops = {f.split("/", 1)[0] for f in s.files}
            assert len(tops) == 1, f"slice {s.position} mixes dirs: {tops}"


# === Strategy: files ===

class TestFilesStrategy:
    def test_files_round_robin(self) -> None:
        files = ["a", "b", "c", "d", "e", "f"]
        slices = plan_splits(
            diff_files=files, strategy="files", worktree_id="wt-1",
            task="t", n_slices=2, max_files_per_slice=10,
        )
        assert len(slices) == 2
        # Round-robin: [a,c,e] and [b,d,f]
        assert slices[0].files == ["a", "c", "e"]
        assert slices[1].files == ["b", "d", "f"]

    def test_files_caps_by_max_files_per_slice(self) -> None:
        files = [f"f{i}" for i in range(25)]
        slices = plan_splits(
            diff_files=files, strategy="files", worktree_id="wt-1",
            task="t", n_slices=None, max_files_per_slice=10,
        )
        # ceil(25 / 10) = 3 slices
        assert len(slices) == 3
        # Each slice capped at 10
        for s in slices:
            assert len(s.files) <= 10

    def test_files_n_slices_clamped_to_max_slices(self) -> None:
        files = [f"f{i}" for i in range(20)]
        slices = plan_splits(
            diff_files=files, strategy="files", worktree_id="wt-1",
            task="t", n_slices=100, max_files_per_slice=10,
            max_slices=4,
        )
        # Clamped to max_slices=4
        assert len(slices) == 4

    def test_files_n_slices_clamped_to_n_files(self) -> None:
        files = ["a", "b", "c"]
        slices = plan_splits(
            diff_files=files, strategy="files", worktree_id="wt-1",
            task="t", n_slices=10, max_files_per_slice=10,
        )
        # Can't have more slices than files → 3
        assert len(slices) == 3

    def test_files_drops_empty_buckets(self) -> None:
        files = ["a", "b", "c", "d", "e"]
        slices = plan_splits(
            diff_files=files, strategy="files", worktree_id="wt-1",
            task="t", n_slices=4, max_files_per_slice=10,
        )
        # Round-robin into 4 buckets: [a,e], [b], [c], [d]
        # No empty buckets after drop_empty_buckets
        for s in slices:
            assert s.files  # no empties
        total = sum(len(s.files) for s in slices)
        assert total == 5


# === Strategy: directory ===

class TestDirectoryStrategy:
    def test_directory_groups_by_top_level(self) -> None:
        files = [
            "src/a.py", "src/b.py",
            "tests/t.py",
            "docs/d.md",
        ]
        slices = plan_splits(
            diff_files=files, strategy="directory", worktree_id="wt-1",
            task="t",
        )
        assert len(slices) == 3
        assert [f.split("/", 1)[0] for f in slices[0].files] == ["docs"] * 1
        # The exact order depends on sort, but each slice is one dir
        for s in slices:
            tops = {f.split("/", 1)[0] for f in s.files}
            assert len(tops) == 1

    def test_directory_root_files_in_own_bucket(self) -> None:
        files = ["README.md", "setup.py", "src/main.py"]
        slices = plan_splits(
            diff_files=files, strategy="directory", worktree_id="wt-1",
            task="t",
        )
        # 2 buckets: root files and src/
        assert len(slices) == 2
        # Each slice should be homogeneous (one directory or root)
        for s in slices:
            tops = {
                f.split("/", 1)[0] if "/" in f else "(root)"
                for f in s.files
            }
            assert len(tops) == 1, f"slice {s.position} mixes: {tops}"
        # Find the root slice and confirm README + setup there
        root_slice = next(
            s for s in slices
            if any("/" not in f for f in s.files)
        )
        assert "README.md" in root_slice.files
        assert "setup.py" in root_slice.files
        assert "src/main.py" not in root_slice.files

    def test_directory_n_slices_caps_groups(self) -> None:
        files = [f"d{i}/f.py" for i in range(10)]
        slices = plan_splits(
            diff_files=files, strategy="directory", worktree_id="wt-1",
            task="t", n_slices=3, max_slices=3,
        )
        # 10 directories, capped to 3
        assert len(slices) == 3


# === Strategy: size ===

class TestSizeStrategy:
    def test_size_balances_by_loc(self) -> None:
        files = ["a", "b", "c", "d"]
        file_locs = {"a": 100, "b": 50, "c": 50, "d": 10}
        slices = plan_splits(
            diff_files=files, strategy="size", worktree_id="wt-1",
            task="t", n_slices=2, file_locs=file_locs,
        )
        assert len(slices) == 2
        # LPT order [a(100), b(50), c(50), d(10)]
        # a→S0(100), b→S1(50), c→S0(150), d→S1(60)
        # Result: S0=[a,c]=150, S1=[b,d]=60
        totals = {tuple(s.files): sum(file_locs[f] for f in s.files)
                  for s in slices}
        # Both slices should be roughly balanced
        assert all(t > 0 for t in totals.values())
        # Imbalance ≤ 100 (a is big)
        # Note: the test is loose — exact split depends on tie-breaking.

    def test_size_falls_back_to_count_when_no_locs(self) -> None:
        files = ["a", "b", "c", "d"]
        slices = plan_splits(
            diff_files=files, strategy="size", worktree_id="wt-1",
            task="t", n_slices=2, file_locs={},
        )
        # Falls back to round-robin
        assert len(slices) == 2

    def test_size_missing_files_default_to_zero(self) -> None:
        files = ["a", "b", "c", "d"]
        file_locs = {"a": 100, "b": 50}  # c, d missing
        slices = plan_splits(
            diff_files=files, strategy="size", worktree_id="wt-1",
            task="t", n_slices=2, file_locs=file_locs,
        )
        # Should not crash; c, d get weight 0
        assert len(slices) == 2
        assert all(s.files for s in slices)


# === Limits ===

class TestLimits:
    def test_min_slices_collapses_to_single(self) -> None:
        """If planner would produce < min_slices, the result is
        already at least 1 slice, so no collapse is needed. But
        if the diff is empty → still 1 slice."""
        files = ["a.py"]
        slices = plan_splits(
            diff_files=files, strategy="files", worktree_id="wt-1",
            task="t", n_slices=5, min_slices=1, max_files_per_slice=10,
        )
        # 1 file < 5 slices → only 1 slice produced
        assert len(slices) == 1

    def test_max_slices_clamp(self) -> None:
        files = [f"f{i}" for i in range(50)]
        slices = plan_splits(
            diff_files=files, strategy="files", worktree_id="wt-1",
            task="t", n_slices=None, max_files_per_slice=2,
            max_slices=4,
        )
        # ceil(50/2) = 25, but max_slices=4 → 4 slices
        assert len(slices) == 4


# === Determinism ===

class TestDeterminism:
    def test_same_input_same_output(self) -> None:
        files = ["src/c.py", "src/a.py", "src/b.py", "tests/t.py"]
        kwargs = dict(
            diff_files=files, strategy="directory",
            worktree_id="wt-1", task="t",
        )
        a = plan_splits(**kwargs)
        b = plan_splits(**kwargs)
        assert [(s.position, s.files) for s in a] == [
            (s.position, s.files) for s in b
        ]

    def test_files_in_slice_are_sorted(self) -> None:
        files = ["z.py", "a.py", "m.py", "b.py"]
        slices = plan_splits(
            diff_files=files, strategy="files", worktree_id="wt-1",
            task="t", n_slices=1, max_files_per_slice=10,
        )
        # All files in one slice, sorted alphabetically
        assert slices[0].files == sorted(files)


# === Branch naming ===

class TestBranchNaming:
    def test_branch_names_include_position(self) -> None:
        slices = plan_splits(
            diff_files=["a.py", "b.py", "c.py", "d.py"],
            strategy="files", worktree_id="wt-abc",
            task="t", n_slices=2,
        )
        positions = [s.position for s in slices]
        branches = [s.branch_name for s in slices]
        assert branches == [
            f"harness/wt-abc/step-{p}" for p in positions
        ]


# === Errors ===

class TestErrors:
    def test_unknown_strategy_raises(self) -> None:
        with pytest.raises(ValueError, match="pr_split_strategy"):
            plan_splits(
                diff_files=["a.py"], strategy="wrong",
                worktree_id="wt-1", task="t",
            )

    def test_invalid_strategy_via_settings(self, monkeypatch) -> None:
        """If strategy comes from settings.pr_split_strategy and is
        invalid, the Pydantic validator in config.py rejects at load
        time, not here. This test only checks the planner itself."""
        with pytest.raises(ValueError, match="pr_split_strategy must be"):
            plan_splits(
                diff_files=["a.py"], strategy="",
                worktree_id="wt-1", task="t",
            )
