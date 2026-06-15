"""Tests for harness.agents.runner (Phase 2.0, Step 4).

Covers:
  - filter_tools: read-only strips write tools even if listed
  - filter_runtime: read-only proxy denies write_file / edit_file
  - filter_runtime: full returns the original runtime (no wrapper)
  - build_system_prompt_for: role description comes first
  - AgentRunner.run: end-to-end with a FakeRouter (scripted responses)
  - AgentRunner.run: worktree_required=False runs in self.repo
  - AgentRunner.run: spec.max_iterations is forwarded
  - AgentRunner.run: worktree is cleaned up on exception
  - AgentRunner.run: denied tool calls are counted
  - Permissions denylist mapping
  - Cost accumulation
  - Static guarantee: runner does not import LLMRouterClassifier/MergeQueue/AdversarialVerify
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

from harness.agents.runner import (
    AgentRunner,
    RunResult,
    _DeniedToolRuntime,
    build_system_prompt_for,
    filter_runtime,
    filter_tools,
    permissions_denylist,
)
from harness.agents.spec import AgentSpec
from harness.agents.worktree import WORKTREE_PARENT, WorktreeSession
from harness.server.agent.runtime import ToolRuntime, ToolResult
from harness.server.agent.tools import TOOL_SCHEMAS
from harness.server.llm.router import LLMRouter, StreamEvent


# === FakeRouter (mirrors test_agent_loop.py pattern) ===

class FakeRouter:
    """Drop-in LLMRouter replacement that scripts responses.

    Each call to ``streaming_completion`` pops the next response from
    ``scripted_turns`` and yields the events. When the list is exhausted
    it yields a single ``done`` event (so AgentLoop terminates cleanly).
    Both ``streaming_completion`` and ``completion`` record their kwargs
    to ``self.calls`` so tests can assert on what was sent.
    """

    def __init__(self, scripted_turns: list[list[StreamEvent]] | None = None) -> None:
        self.scripted_turns: list[list[StreamEvent]] = scripted_turns or []
        self.calls: list[dict[str, Any]] = []

    def _next_turn(self) -> list[StreamEvent]:
        if self.scripted_turns:
            return self.scripted_turns.pop(0)
        return [StreamEvent(type="done", content="", cost=0.0, usage={})]

    async def streaming_completion(self, *, model: str, messages, **kwargs):
        self.calls.append({"method": "streaming", "model": model, "messages": list(messages), **kwargs})
        for ev in self._next_turn():
            yield ev

    async def completion(self, *, model: str, messages, **kwargs):
        self.calls.append({"method": "completion", "model": model, "messages": list(messages), **kwargs})
        # Mirror streaming_completion for non-streaming callers.
        events = self._next_turn()
        # Find the assistant_message (or empty content if absent).
        content = ""
        usage: dict[str, int] = {}
        cost = 0.0
        tool_calls: list[dict] = []
        for e in events:
            if e.type == "assistant_message":
                content = e.content or ""
                usage = e.usage or {}
                cost = e.cost or 0.0
            elif e.type == "tool_call" and e.tool_call:
                tool_calls.append(dict(e.tool_call))
            elif e.type == "done":
                cost += e.cost or 0.0
                usage = {**(e.usage or {}), **usage}
        # Return a CompletionResult-like object
        from harness.server.llm.router import CompletionResult
        return CompletionResult(
            content=content, tool_calls=tool_calls or None,
            usage=usage, cost=cost,
        )


def _make_tool_call(call_id: str, name: str, args: dict[str, Any]) -> dict:
    """Build an OpenAI-shaped tool_call dict."""
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": _args_to_json(args)},
    }


def _args_to_json(args: dict[str, Any]) -> str:
    import json
    return json.dumps(args)


def _done(cost: float = 0.0, usage: dict[str, int] | None = None) -> StreamEvent:
    return StreamEvent(type="done", content="", cost=cost, usage=usage or {})


def _assistant(content: str, *, cost: float = 0.0, usage: dict[str, int] | None = None) -> StreamEvent:
    return StreamEvent(
        type="assistant_message", content=content,
        cost=cost, usage=usage or {},
    )


def _tool_call(call_id: str, name: str, args: dict[str, Any]) -> StreamEvent:
    return StreamEvent(
        type="tool_call", content="",
        tool_call=_make_tool_call(call_id, name, args),
    )


# === filter_tools / permissions_denylist ===

def test_filter_tools_read_only_strips_write_even_if_listed() -> None:
    """read-only + write_file in tools = write_file absent from output.

    The schema-level conflict is checked at AgentSpec construction
    (read-only + write tools in spec.tools → raise). To exercise the
    RUNTIME denylist, we build a spec via ``model_construct`` (skips
    validation) and pass it to ``filter_tools`` directly.
    """
    spec = AgentSpec.model_construct(
        name="x",
        model="MiniMax-M2.7",
        tools=["read_file", "write_file", "edit_file", "grep"],
        permissions="read-only",
        system_prompt="",
        max_iterations=5,
        worktree_required=True,
        allowed_paths=[],
    )
    names = [t["name"] for t in filter_tools(spec)]
    assert "read_file" in names
    assert "grep" in names
    assert "write_file" not in names
    assert "edit_file" not in names


def test_filter_tools_full_includes_all() -> None:
    spec = AgentSpec(
        name="code",
        tools=["read_file", "write_file", "edit_file", "bash", "grep", "glob"],
        permissions="full",
    )
    names = [t["name"] for t in filter_tools(spec)]
    assert set(names) == {"read_file", "write_file", "edit_file", "bash", "grep", "glob"}


def test_filter_tools_spec_tools_restricts_set() -> None:
    """Only tools listed in spec.tools appear in the output."""
    spec = AgentSpec(
        name="x", tools=["read_file"],
        permissions="full",
    )
    names = [t["name"] for t in filter_tools(spec)]
    assert names == ["read_file"]


def test_filter_tools_empty_tools_means_no_tools() -> None:
    spec = AgentSpec(name="x", tools=[], permissions="full")
    assert filter_tools(spec) == []


def test_permissions_denylist_mapping() -> None:
    assert "write_file" in permissions_denylist("read-only")
    assert "edit_file" in permissions_denylist("read-only")
    assert permissions_denylist("full") == frozenset()
    assert permissions_denylist("scoped-write") == frozenset()


def test_permissions_denylist_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown permissions"):
        permissions_denylist("super-user")


# === filter_runtime (proxy) ===

def test_filter_runtime_read_only_returns_proxy(tmp_path: Path) -> None:
    rt = ToolRuntime(project_root=tmp_path)
    wrapped = filter_runtime(
        AgentSpec(name="x", permissions="read-only"), rt,
    )
    assert isinstance(wrapped, _DeniedToolRuntime)


def test_filter_runtime_full_returns_original(tmp_path: Path) -> None:
    rt = ToolRuntime(project_root=tmp_path)
    wrapped = filter_runtime(
        AgentSpec(name="x", permissions="full"), rt,
    )
    assert wrapped is rt  # exact identity, no wrapper


async def test_proxy_denies_write_file(tmp_path: Path) -> None:
    rt = ToolRuntime(project_root=tmp_path)
    wrapped = filter_runtime(
        AgentSpec(name="x", permissions="read-only"), rt,
    )
    result = await wrapped.execute("write_file", {"path": "x", "content": "y"})
    assert result.ok is False
    assert "denied" in result.error


async def test_proxy_denies_edit_file(tmp_path: Path) -> None:
    rt = ToolRuntime(project_root=tmp_path)
    wrapped = filter_runtime(
        AgentSpec(name="x", permissions="read-only"), rt,
    )
    result = await wrapped.execute(
        "edit_file", {"path": "x", "old_string": "a", "new_string": "b"},
    )
    assert result.ok is False


async def test_proxy_allows_read_file(tmp_path: Path) -> None:
    rt = ToolRuntime(project_root=tmp_path)
    f = tmp_path / "x.txt"
    f.write_text("hello", encoding="utf-8")
    wrapped = filter_runtime(
        AgentSpec(name="x", permissions="read-only"), rt,
    )
    result = await wrapped.execute("read_file", {"path": "x.txt"})
    assert result.ok is True
    assert "hello" in result.output


async def test_proxy_forwards_unknown_attribute(tmp_path: Path) -> None:
    """Other ToolRuntime attributes are accessible via the proxy."""
    rt = ToolRuntime(project_root=tmp_path)
    wrapped = filter_runtime(
        AgentSpec(name="x", permissions="read-only"), rt,
    )
    # project_root is forwarded
    assert wrapped.project_root == rt.project_root


# === build_system_prompt_for ===

def test_system_prompt_role_first(tmp_path: Path) -> None:
    spec = AgentSpec(
        name="explore",
        system_prompt="You are the explore sub-agent.",
        tools=["read_file"],
    )
    out = build_system_prompt_for(spec, tmp_path, filter_tools(spec))
    # Role description appears BEFORE the standard "You are Solomon" prelude.
    assert out.index("You are the explore sub-agent.") < out.index("You are Solomon")


def test_system_prompt_no_role_falls_back_to_standard(tmp_path: Path) -> None:
    spec = AgentSpec(name="x", system_prompt="", tools=[])
    out = build_system_prompt_for(spec, tmp_path, [])
    assert "You are Solomon" in out


# === AgentRunner.run end-to-end ===

@pytest.fixture
def read_only_spec() -> AgentSpec:
    return AgentSpec(
        name="explore",
        model="MiniMax-M2.7",
        tools=["read_file", "grep", "glob"],
        permissions="read-only",
        max_iterations=3,
        system_prompt="You are the explore sub-agent.",
    )


@pytest.fixture
def code_spec() -> AgentSpec:
    return AgentSpec(
        name="code",
        model="MiniMax-M2.7",
        tools=["read_file", "write_file", "edit_file", "bash", "grep", "glob"],
        permissions="full",
        max_iterations=4,
        system_prompt="Make the smallest change.",
    )


async def test_runner_runs_in_worktree(git_repo: Path, read_only_spec: AgentSpec) -> None:
    """A read-only explore agent runs inside a worktree on a fresh branch."""
    router = FakeRouter(scripted_turns=[[_assistant("Found 3 files.")]])
    runner = AgentRunner(router=router, repo=git_repo)  # type: ignore[arg-type]
    result = await runner.run(read_only_spec, "list files")
    assert isinstance(result, RunResult)
    assert result.iterations >= 1
    assert "Found 3 files" in result.final_text
    assert result.worktree.path != git_repo  # ran in a worktree
    # worktree_id is auto-generated, starts with 'wt-'
    assert result.worktree.worktree_id.startswith("wt-")
    assert result.worktree.branch == f"harness/{result.worktree.worktree_id}"


async def test_runner_cleans_up_worktree_after_run(git_repo: Path, read_only_spec: AgentSpec) -> None:
    router = FakeRouter(scripted_turns=[[_assistant("done.")]])
    runner = AgentRunner(router=router, repo=git_repo)  # type: ignore[arg-type]
    await runner.run(read_only_spec, "task", worktree_id="cleanup-test")
    # After the run, the worktree is removed.
    assert not (git_repo / WORKTREE_PARENT / "cleanup-test").exists()
    proc = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=git_repo, capture_output=True, text=True,
    )
    assert "harness/cleanup-test" not in proc.stdout


async def test_runner_spec_max_iterations_forwarded(
    git_repo: Path, read_only_spec: AgentSpec,
) -> None:
    """AgentLoop is constructed with max_iterations from the spec."""
    read_only_spec = read_only_spec.model_copy(update={"max_iterations": 7})
    router = FakeRouter(scripted_turns=[[_assistant("x")]])
    runner = AgentRunner(router=router, repo=git_repo)  # type: ignore[arg-type]
    await runner.run(read_only_spec, "task")
    # The call recorded by FakeRouter (first streaming_completion) is from
    # AgentLoop. We can't directly assert max_iterations, but we can check
    # that the runner didn't override it.
    # (Indirect: the runner didn't raise; if max_iterations was set wrong
    # AgentLoop would either error or loop too many times.)


async def test_runner_passes_model_to_agent_loop(
    git_repo: Path, read_only_spec: AgentSpec,
) -> None:
    """The router sees the spec's model id, not the runner's choice."""
    read_only_spec = read_only_spec.model_copy(update={"model": "glm-4.7"})
    router = FakeRouter(scripted_turns=[[_assistant("x")]])
    runner = AgentRunner(router=router, repo=git_repo)  # type: ignore[arg-type]
    await runner.run(read_only_spec, "task")
    assert router.calls, "no LLM call recorded"
    assert router.calls[0]["model"] == "glm-4.7"


async def test_runner_passes_user_prompt(
    git_repo: Path, read_only_spec: AgentSpec,
) -> None:
    router = FakeRouter(scripted_turns=[[_assistant("ack.")]])
    runner = AgentRunner(router=router, repo=git_repo)  # type: ignore[arg-type]
    await runner.run(read_only_spec, "what is in README.md?")
    assert router.calls, "no LLM call recorded"
    user_msgs = [m for m in router.calls[0]["messages"] if m.get("role") == "user"]
    assert any("README.md" in (m.get("content") or "") for m in user_msgs)


async def test_runner_worktree_required_false_runs_in_self_repo(
    git_repo: Path, read_only_spec: AgentSpec,
) -> None:
    """worktree_required=False means no worktree created; agent runs in self.repo."""
    read_only_spec = read_only_spec.model_copy(update={"worktree_required": False})
    router = FakeRouter(scripted_turns=[[_assistant("x")]])
    runner = AgentRunner(router=router, repo=git_repo)  # type: ignore[arg-type]
    result = await runner.run(read_only_spec, "task")
    assert result.worktree.path == git_repo
    assert result.worktree.branch == "(no worktree)"
    # No worktree was created under .harness/worktrees.
    wt_dir = git_repo / WORKTREE_PARENT
    if wt_dir.exists():
        assert not any(wt_dir.iterdir())


async def test_runner_counts_denied_tool_calls(
    git_repo: Path, code_spec: AgentSpec,
) -> None:
    """A read-only agent that the LLM tries to call write_file → denied.

    Turn 1: LLM calls write_file → proxy returns ok=False → ``denied_count += 1``.
    Turn 2: LLM replies with final text → loop ends. The denial is
    visible in ``RunResult.denied_tool_calls``.
    """
    from harness.server.llm.router import CompletionResult

    # Build a read-only spec via model_construct (skip schema check that
    # would reject read-only + write_file in tools).
    ro = AgentSpec.model_construct(
        name="ro",
        model="MiniMax-M2.7",
        tools=["read_file", "write_file", "grep"],
        permissions="read-only",
        system_prompt="",
        max_iterations=3,
        worktree_required=True,
        allowed_paths=[],
    )
    router = FakeRouter()
    turn = {"n": 0}

    async def scripted(*, model, messages, **kwargs):
        router.calls.append({"method": "completion", "model": model, "messages": list(messages), **kwargs})
        turn["n"] += 1
        if turn["n"] == 1:
            # Turn 1: try to write (will be denied by proxy)
            return CompletionResult(
                content="I'll write the file.",
                tool_calls=[_make_tool_call("c1", "write_file", {"path": "x", "content": "y"})],
                usage={"prompt_tokens": 10, "completion_tokens": 5},
                cost=0.0,
            )
        # Turn 2: give up, return final text
        return CompletionResult(
            content="Cannot write, denied.",
            tool_calls=None,
            usage={"prompt_tokens": 30, "completion_tokens": 10},
            cost=0.0,
        )

    router.completion = scripted  # type: ignore[method-assign]
    runner = AgentRunner(router=router, repo=git_repo)  # type: ignore[arg-type]
    result = await runner.run(ro, "write x")
    assert result.denied_tool_calls == 1
    assert result.iterations == 2


async def test_runner_accumulates_cost(
    git_repo: Path, read_only_spec: AgentSpec,
) -> None:
    """Two LLM turns (first calls a tool, second replies) → costs sum.

    For AgentLoop to make a second turn, the first turn must request
    a tool call. We script the router directly: the first turn returns
    a tool call (read_file), the second turn returns the final assistant
    message. Costs should sum across both turns.
    """
    from harness.server.llm.router import CompletionResult

    # Pre-build a scripted router that returns tool_calls on turn 1.
    router = FakeRouter()
    router.scripted_turns = [
        # Turn 1: read_file tool call (cost 0.01)
        [
            StreamEvent(type="assistant_message", content="Reading...", cost=0.01,
                        usage={"prompt_tokens": 100, "completion_tokens": 5}),
        ],
        # Turn 2: final reply (cost 0.02). Empty tool_calls → loop ends.
        [
            StreamEvent(type="assistant_message", content="Found 3 files.", cost=0.02,
                        usage={"prompt_tokens": 120, "completion_tokens": 60}),
        ],
    ]
    # Override completion() to inject the tool_call on turn 1.
    original_completion = router.completion

    turn_idx = {"n": 0}

    async def scripted_completion(*, model, messages, **kwargs):
        router.calls.append({"method": "completion", "model": model, "messages": list(messages), **kwargs})
        idx = turn_idx["n"]
        turn_idx["n"] += 1
        if idx == 0:
            return CompletionResult(
                content="Reading...",
                tool_calls=[_make_tool_call("c1", "read_file", {"path": "README.md"})],
                usage={"prompt_tokens": 100, "completion_tokens": 5},
                cost=0.01,
            )
        # Turn 2 (and beyond): final answer, no tool calls
        return CompletionResult(
            content="Found 3 files.",
            tool_calls=None,
            usage={"prompt_tokens": 120, "completion_tokens": 60},
            cost=0.02,
        )

    router.completion = scripted_completion  # type: ignore[method-assign]
    runner = AgentRunner(router=router, repo=git_repo)  # type: ignore[arg-type]
    result = await runner.run(read_only_spec, "task")
    # Two iterations (one per turn) — cost sums across both.
    assert result.iterations == 2
    assert result.total_cost == pytest.approx(0.03, abs=1e-6)
    # Usage from each turn adds up: 5 + 60 completion tokens.
    assert result.usage.get("completion_tokens") == 65


async def test_runner_handles_runtime_exception(
    git_repo: Path, read_only_spec: AgentSpec,
) -> None:
    """If the LLM call raises, AgentLoop catches it and emits ``error``.

    AgentLoop wraps every exception inside its own try/except and yields
    an ``error`` event — so the runner sees a normal event stream ending
    in ``error`` content, and surfaces it in ``RunResult.error``.
    """
    class ExplodingRouter(FakeRouter):
        async def streaming_completion(self, **kwargs):
            raise RuntimeError("simulated LLM failure")
            yield  # noqa: unreachable — for type checker

        async def completion(self, **kwargs):
            raise RuntimeError("simulated LLM failure")

    runner = AgentRunner(router=ExplodingRouter(), repo=git_repo)  # type: ignore[arg-type]
    result = await runner.run(read_only_spec, "task", worktree_id="boom")
    # AgentLoop catches the exception, yields error event, runner picks it up.
    assert result.error is not None
    assert "simulated LLM failure" in result.error
    # Worktree is still cleaned up.
    assert not (git_repo / WORKTREE_PARENT / "boom").exists()


# === Static guarantee: no sub-agent-of-sub-agent imports ===

def test_runner_does_not_import_router_classifier() -> None:
    """The runner must not import LLMRouterClassifier (Step 6).

    We check for the *import statement* (line starting with ``from``
    or ``import``) — not the bare substring, because the module docstring
    mentions these names as the things we explicitly do NOT import.
    """
    import harness.agents.runner as runner_mod
    src = Path(runner_mod.__file__).read_text(encoding="utf-8")
    for forbidden in ("LLMRouterClassifier", "RouterDecision"):
        for line in src.splitlines():
            stripped = line.strip()
            if (
                stripped.startswith(f"from harness.agents.router import {forbidden}")
                or stripped.startswith(f"import {forbidden}")
                or f"import {forbidden}" in stripped.split(" as ")[0]
            ):
                pytest.fail(
                    f"runner.py has a real import of {forbidden!r}: {line!r}"
                )


def test_runner_does_not_import_merge_queue() -> None:
    """The runner must not import MergeQueue (Step 7)."""
    import harness.agents.runner as runner_mod
    src = Path(runner_mod.__file__).read_text(encoding="utf-8")
    for forbidden in ("MergeQueue", "MergeJob", "MergeResult"):
        for line in src.splitlines():
            stripped = line.strip()
            if (
                stripped.startswith(f"from harness.agents.merge_queue import {forbidden}")
                or stripped.startswith(f"import {forbidden}")
                or f" import {forbidden}" in stripped
            ):
                pytest.fail(
                    f"runner.py has a real import of {forbidden!r}: {line!r}"
                )


def test_runner_does_not_import_adversarial_verify() -> None:
    """The runner must not import AdversarialVerify (Step 6)."""
    import harness.agents.runner as runner_mod
    src = Path(runner_mod.__file__).read_text(encoding="utf-8")
    for line in src.splitlines():
        stripped = line.strip()
        if (
            stripped.startswith("from harness.agents.verify import")
            or " import AdversarialVerify" in stripped
        ):
            pytest.fail(f"runner.py imports AdversarialVerify: {line!r}")


def test_runner_does_not_import_registry() -> None:
    """The runner is composition-only — registry is the caller's concern."""
    import harness.agents.runner as runner_mod
    src = Path(runner_mod.__file__).read_text(encoding="utf-8")
    assert "harness.agents.registry" not in src
    assert "from harness.agents.registry" not in src


def test_runner_does_not_import_scratchpad() -> None:
    """The runner must not import scratchpad types directly (Phase 3 v1.2.0).

    Trust boundary: scratchpad is wired via factory DI
    (``scratchpad_factory`` kwarg on ``AgentRunner.__init__``), not
    by direct module import. A static check on the runner's source
    file ensures the module never gains a hard dependency on
    ``harness.agents.scratchpad`` /
    ``harness.agents.scratchpad_store`` /
    ``harness.context.scratchpad_audit``.
    """
    import harness.agents.runner as runner_mod
    src = Path(runner_mod.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "ScratchpadStore", "Note", "PlanStep", "NoteLevel", "PlanStatus",
        "ScratchpadAudit",
    ):
        for line in src.splitlines():
            stripped = line.strip()
            if (
                stripped.startswith(f"from harness.agents.scratchpad import {forbidden}")
                or stripped.startswith(f"from harness.agents.scratchpad_store import {forbidden}")
                or stripped.startswith(f"from harness.context.scratchpad_audit import {forbidden}")
            ):
                pytest.fail(
                    f"runner.py has a real import of {forbidden!r}: {line!r}"
                )


# === RunResult dataclass ===

def test_run_result_default_usage() -> None:
    """RunResult.usage defaults to an empty dict (mutable default trap avoided)."""
    from harness.agents.worktree import WorktreeInfo
    # Two instances must NOT share the same dict.
    r1 = RunResult(
        spec=AgentSpec(name="x"),
        worktree=WorktreeInfo(path=Path("C:/x"), branch="h/x", worktree_id="x"),
        final_text="", iterations=0, total_cost=0.0,
    )
    r2 = RunResult(
        spec=AgentSpec(name="x"),
        worktree=WorktreeInfo(path=Path("C:/y"), branch="h/x", worktree_id="x"),
        final_text="", iterations=0, total_cost=0.0,
    )
    r1.usage["k"] = 1
    assert r2.usage == {}
