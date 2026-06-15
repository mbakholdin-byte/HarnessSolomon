"""Sub-agent runner — composes WorktreeSession + AgentLoop (Phase 2.0, Step 4).

The runner is intentionally thin: it instantiates a per-run
:class:`~harness.server.agent.runtime.ToolRuntime` bound to the worktree's
``project_root``, filters :data:`~harness.server.agent.tools.TOOL_SCHEMAS`
by the spec's ``tools`` allowlist and ``permissions`` denylist, and
delegates the LLM↔tool loop to
:class:`~harness.server.agent.loop.AgentLoop`.

**Trust boundary:** the runner does NOT import
:class:`LLMRouterClassifier`, :class:`MergeQueue`, or
:class:`AdversarialVerify`. A code review pass should ``grep`` for cross
imports in this file to enforce the design constraint
(``architecture.md:86``) that sub-agents cannot spawn sub-agents.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from harness.agents.spec import AgentSpec
from harness.agents.worktree import WorktreeInfo, WorktreeSession
from harness.redaction import redact
from harness.server.agent.loop import AgentLoop, DEFAULT_MAX_ITERATIONS
from harness.server.agent.prompts import build_system_prompt
from harness.server.agent.runtime import ToolResult, ToolRuntime
from harness.server.agent.tools import TOOL_SCHEMAS
from harness.server.llm.router import LLMRouter, StreamEvent

logger = logging.getLogger(__name__)


# === Permissions → denylist ===

#: Tools stripped from the tool list when the agent has ``read-only`` perms,
#: regardless of whether they appear in ``spec.tools``. This is defence in
#: depth: a typo or hallucination cannot enable write access for an
#: agent declared as read-only.
#:
#: Phase 3 v1.2.0 also strips the 3 scratchpad *write* tools from
#: read-only agents. ``scratchpad_read_notes`` stays — a read-only
#: agent can still consult its own notes / plan.
_READ_ONLY_DENY: frozenset[str] = frozenset({
    "write_file", "edit_file",
    "scratchpad_write_note", "scratchpad_plan_step", "scratchpad_mark_done",
})


def permissions_denylist(permissions: str) -> frozenset[str]:
    """Return the set of tool names that are unconditionally denied for a
    given ``permissions`` level. ``scoped-write`` and ``full`` do not
    strip any tools at this layer — enforcement happens in the runtime
    via ``allowed_paths`` (Phase 2.1) and the existing path sandbox."""
    if permissions == "read-only":
        return _READ_ONLY_DENY
    if permissions in ("scoped-write", "full"):
        return frozenset()
    raise ValueError(f"unknown permissions level: {permissions!r}")


# === Filter helpers ===

def filter_tools(spec: AgentSpec) -> list[dict[str, Any]]:
    """Return the TOOL_SCHEMAS filtered by ``spec.tools`` and the perms denylist."""
    deny = permissions_denylist(spec.permissions)
    return [t for t in TOOL_SCHEMAS if t["name"] in spec.tools and t["name"] not in deny]


def build_system_prompt_for(
    spec: AgentSpec, project_root: Path, tools: list[dict[str, Any]],
) -> str:
    """Compose ``spec.system_prompt`` + the standard system prompt.

    The spec's prompt is the role description; the standard prompt adds
    the project_root and tool catalogue. We put the role description FIRST
    so it sets the tone before the LLM sees the tool list.
    """
    if spec.system_prompt:
        return f"{spec.system_prompt}\n\n{build_system_prompt(project_root, tools)}"
    return build_system_prompt(project_root, tools)


# === Proxy runtime (defence in depth) ===

class _DeniedToolRuntime:
    """A ``ToolRuntime``-shaped proxy that short-circuits denied tools.

    Why a proxy, not a subclass: ``ToolRuntime`` has many private methods
    and we want zero risk of breaking them. The proxy is duck-compatible:
    anything that calls ``await runtime.execute(name, args)`` works the
    same; calls to other ToolRuntime methods fall through to the wrapped
    instance.
    """

    __slots__ = ("_inner", "_denied")

    def __init__(self, inner: ToolRuntime, denied: frozenset[str]) -> None:
        self._inner = inner
        self._denied = denied

    async def execute(self, name: str, args: dict[str, Any]) -> ToolResult:
        if name in self._denied:
            return ToolResult(
                ok=False,
                error=f"tool denied by agent permissions: {name!r}",
            )
        return await self._inner.execute(name, args)

    def __getattr__(self, item: str) -> Any:
        """Forward any other attribute access to the wrapped runtime."""
        return getattr(self._inner, item)


def filter_runtime(spec: AgentSpec, runtime: ToolRuntime) -> ToolRuntime:
    """Wrap ``runtime`` in a denylist-enforcing proxy when needed.

    Returns the original runtime unchanged when the denylist is empty
    (avoids a needless wrapper layer for ``full`` and ``scoped-write``
    agents that can use any tool).
    """
    deny = permissions_denylist(spec.permissions)
    if not deny:
        return runtime
    # The proxy is duck-compatible with ToolRuntime. We type-annotate as
    # ToolRuntime for the caller's convenience; runtime checkers like
    # mypy can't tell the difference.
    return _DeniedToolRuntime(runtime, deny)  # type: ignore[return-value]


# === Runner ===

@dataclass
class RunResult:
    """Summary of a single sub-agent run."""

    spec: AgentSpec
    worktree: WorktreeInfo  # the worktree the agent ran in (or self.repo if no-worktree)
    final_text: str
    iterations: int
    total_cost: float
    usage: dict[str, int] = field(default_factory=dict)
    denied_tool_calls: int = 0
    error: str | None = None


class AgentRunner:
    """Run a sub-agent end-to-end inside a worktree.

    Args:
        router: An :class:`LLMRouter` (reuse the harness's main router —
                all sub-agents hit the same model catalog).
        repo:   The main git repo dir. Sub-agents branch off this and run
                in a worktree under ``.harness/worktrees/<id>/``.
        unified_memory_factory: Phase 2.1 — optional callable that,
                given an :class:`AgentSpec`, returns a
                :class:`~harness.memory.unified.UnifiedMemory` for
                the agent. When ``None`` (default), sub-agents don't
                have a memory handle and any in-process write would
                be a no-op. The factory is called once per spec per
                process; we cache by spec.name to avoid re-creating
                the same UnifiedMemory on every run.
    """

    def __init__(
        self,
        router: LLMRouter,
        repo: Path,
        *,
        unified_memory_factory: "Callable[[AgentSpec], Any] | None" = None,
        scratchpad_factory: "Callable[[AgentSpec, str | None], Any] | None" = None,
        scratchpad_audit: Any = None,
    ) -> None:
        self.router = router
        self.repo = Path(repo).resolve(strict=False)
        self._unified_memory_factory = unified_memory_factory
        #: Phase 3 v1.2.0: optional factory for the per-(spec, session)
        #: scratchpad store. The factory returns an uninitialised
        #: :class:`~harness.agents.scratchpad_store.ScratchpadStore`; the
        #: runner calls ``.init()`` before use. ``None`` disables
        #: scratchpad tools entirely.
        #:
        #: The factory is intentionally typed as ``Callable[..., Any]``
        #: to keep :mod:`harness.agents.runner` free of any direct
        #: import of the scratchpad module — trust boundary enforced
        #: by ``test_runner_does_not_import_scratchpad``.
        self._scratchpad_factory = scratchpad_factory
        #: Phase 3 v1.2.0: optional audit writer, forwarded to
        #: :class:`~harness.server.agent.runtime.ToolRuntime` so the 4
        #: scratchpad tool calls emit audit events.
        self._scratchpad_audit = scratchpad_audit
        # Cache of spec.name -> UnifiedMemory. Reused across runs of
        # the same spec; cleared only when the runner is replaced.
        self._unified_memories: dict[str, Any] = {}

    def get_unified_memory(self, spec: AgentSpec) -> Any:
        """Return the cached :class:`UnifiedMemory` for ``spec``.

        Falls back to ``None`` when no factory was provided. The
        caller decides whether to write — a ``None`` return is a
        no-op for memory writes.
        """
        if self._unified_memory_factory is None:
            return None
        if spec.name not in self._unified_memories:
            self._unified_memories[spec.name] = self._unified_memory_factory(spec)
        return self._unified_memories[spec.name]

    # --- public API ---

    async def run(
        self,
        spec: AgentSpec,
        prompt: str,
        *,
        worktree_id: str | None = None,
        stream: bool = False,
        external_worktree: "WorktreeInfo | None" = None,
        model_override: str | None = None,
        session_id: str | None = None,
    ) -> RunResult:
        """Run ``spec`` against ``prompt`` and return the final result.

        The worktree is created if ``spec.worktree_required`` (default),
        otherwise we run in ``self.repo`` directly.

        When ``external_worktree`` is supplied, the runner uses that
        worktree INSTEAD of opening its own. This is how the merge
        queue coordinates lifetime: it opens the worktree, calls
        ``runner.run(external_worktree=wt)`` for the code + review agents,
        and decides whether to clean up the worktree based on the
        result (success = clean up, failure = preserve for human review).

        ``stream`` controls whether the underlying AgentLoop yields
        token-level events; when ``False`` (the default — useful for
        programmatic callers) the loop emits a single ``assistant_message``
        per iteration. Streaming is intended for the WebSocket path.

        ``model_override`` (Phase 2.1): when supplied, used INSTEAD of
        ``spec.model`` for this single call. This is how the
        :class:`~harness.agents.cascade.TierSelector` injects a
        cost-aware tier choice without mutating the (frozen) spec.
        Passing ``None`` preserves the spec's model (default).

        ``session_id`` (Phase 3 v1.2.0): when supplied, the runner
        builds a per-call :class:`~harness.agents.scratchpad_store.ScratchpadStore`
        via the configured ``scratchpad_factory`` and forwards it to
        ``ToolRuntime``. When ``None`` (the default — backward-compat
        with pre-v1.2.0 callers) the scratchpad tools are not
        available on this run.
        """
        if external_worktree is not None:
            return await self._drive(
                spec, prompt, external_worktree,
                stream=stream, model_override=model_override,
                session_id=session_id,
            )
        if spec.worktree_required:
            async with WorktreeSession(self.repo, worktree_id=worktree_id) as wt:
                return await self._drive(
                    spec, prompt, wt,
                    stream=stream, model_override=model_override,
                    session_id=session_id,
                )
        # No-worktree path: synthesize a WorktreeInfo pointing at self.repo.
        wt = WorktreeInfo(
            path=self.repo, branch="(no worktree)",
            worktree_id=worktree_id or "no-wt", reused=False,
        )
        return await self._drive(
            spec, prompt, wt,
            stream=stream, model_override=model_override,
            session_id=session_id,
        )

    # --- core loop ---

    async def _drive(
        self,
        spec: AgentSpec,
        prompt: str,
        wt: WorktreeInfo,
        *,
        stream: bool,
        model_override: str | None = None,
        session_id: str | None = None,
    ) -> RunResult:
        # Phase 3 v1.2.0: build a per-(spec, session) scratchpad if the
        # runner was configured with a factory. Fail-open — a broken
        # factory or init must never break the chat loop.
        scratchpad = None
        if self._scratchpad_factory is not None and session_id is not None:
            try:
                scratchpad = self._scratchpad_factory(spec, session_id)
                if scratchpad is not None:
                    await scratchpad.init()
            except Exception as exc:  # noqa: BLE001 — fail-open: scratchpad must never break the chat loop
                logger.warning("scratchpad factory/init failed: %s", exc)
                scratchpad = None
        runtime = ToolRuntime(
            project_root=wt.path,
            scratchpad=scratchpad,
            scratchpad_audit=self._scratchpad_audit,
        )
        wrapped = filter_runtime(spec, runtime)
        tools = filter_tools(spec)
        loop = AgentLoop(
            runtime=wrapped,  # type: ignore[arg-type]
            router=self.router,
            max_iterations=spec.max_iterations or DEFAULT_MAX_ITERATIONS,
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": build_system_prompt_for(spec, wt.path, tools)},
            # Phase 3: redact the user prompt before it reaches the LLM.
            # Idempotent + cheap (~1ms); the redacted text preserves
            # the structure so the LLM can still reason about email
            # addresses, tokens, etc. (it just doesn't see the
            # original values).
            {"role": "user", "content": redact(prompt)},
        ]

        # Phase 2.1: cascade override. We pass the override (or
        # ``spec.model`` when None) into AgentLoop. Spec stays frozen.
        effective_model = model_override if model_override else spec.model

        last_text = ""
        iterations = 0
        total_cost = 0.0
        total_usage: dict[str, int] = {}
        denied_count = 0
        deny_set = permissions_denylist(spec.permissions)
        error: str | None = None

        try:
            async for event in loop.run(messages, model=effective_model, stream=stream):
                if event.type == "assistant_message":
                    iterations += 1
                    if event.content:
                        last_text = event.content
                    if event.cost:
                        total_cost += event.cost
                    if event.usage:
                        for k, v in event.usage.items():
                            total_usage[k] = total_usage.get(k, 0) + int(v)
                elif event.type == "tool_result":
                    # A tool result with ok=False from a denied tool
                    # indicates the perms proxy short-circuited the call.
                    if event.tool_call and event.tool_call.get("ok") is False:
                        name = event.tool_call.get("name", "")
                        if name in deny_set:
                            denied_count += 1
                elif event.type == "error":
                    error = event.content or "unknown error"
                    if event.cost:
                        total_cost += event.cost
                elif event.type == "done":
                    if event.cost:
                        total_cost += event.cost
                    if event.usage:
                        for k, v in event.usage.items():
                            total_usage[k] = total_usage.get(k, 0) + int(v)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            logger.exception("sub-agent %r failed", spec.name)

        return RunResult(
            spec=spec, worktree=wt, final_text=last_text,
            iterations=iterations, total_cost=total_cost,
            usage=total_usage, denied_tool_calls=denied_count, error=error,
        )

    # --- streaming variant ---

    async def stream(
        self,
        spec: AgentSpec,
        prompt: str,
        *,
        worktree_id: str | None = None,
        model_override: str | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Like :meth:`run` but yields ``StreamEvent``s live.

        Use this for WebSocket or CLI streaming output. The final event is
        ``done`` (from AgentLoop); consumers should stop after that.

        ``model_override`` (Phase 2.1): same semantics as in :meth:`run`.

        ``session_id`` (Phase 3 v1.2.0): same semantics as in :meth:`run`.
        """
        if spec.worktree_required:
            async with WorktreeSession(self.repo, worktree_id=worktree_id) as wt:
                async for e in self._stream_drive(
                    spec, prompt, wt,
                    model_override=model_override, session_id=session_id,
                ):
                    yield e
        else:
            wt = WorktreeInfo(
                path=self.repo, branch="(no worktree)",
                worktree_id=worktree_id or "no-wt", reused=False,
            )
            async for e in self._stream_drive(
                spec, prompt, wt,
                model_override=model_override, session_id=session_id,
            ):
                yield e

    async def _stream_drive(
        self,
        spec: AgentSpec,
        prompt: str,
        wt: WorktreeInfo,
        *,
        model_override: str | None = None,
        session_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        # Phase 3 v1.2.0: same scratchpad build as _drive (mirror).
        scratchpad = None
        if self._scratchpad_factory is not None and session_id is not None:
            try:
                scratchpad = self._scratchpad_factory(spec, session_id)
                if scratchpad is not None:
                    await scratchpad.init()
            except Exception as exc:  # noqa: BLE001 — fail-open
                logger.warning("scratchpad factory/init failed: %s", exc)
                scratchpad = None
        runtime = ToolRuntime(
            project_root=wt.path,
            scratchpad=scratchpad,
            scratchpad_audit=self._scratchpad_audit,
        )
        wrapped = filter_runtime(spec, runtime)
        tools = filter_tools(spec)
        loop = AgentLoop(
            runtime=wrapped,  # type: ignore[arg-type]
            router=self.router,
            max_iterations=spec.max_iterations or DEFAULT_MAX_ITERATIONS,
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": build_system_prompt_for(spec, wt.path, tools)},
            # Phase 3: see _drive() — redact the user prompt before
            # passing it to the LLM.
            {"role": "user", "content": redact(prompt)},
        ]
        effective_model = model_override if model_override else spec.model
        async for event in loop.run(messages, model=effective_model, stream=True):
            yield event
