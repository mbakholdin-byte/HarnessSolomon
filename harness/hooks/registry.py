"""Phase 4.0: Hook registry — event → [hooks] mapping.

The registry stores ``HookSpec`` objects keyed by ``EventType``. It
exposes ``register`` / ``unregister`` / ``for_event`` / ``all_specs``
methods. Specs are immutable (frozen dataclass); the registry itself
is thread-safe for read (uses a copy-on-read snapshot), and uses
a per-instance ``_lock`` for writes.

A ``HookSpec`` is the registration record. The actual hook callable
may be:
    - in-process: ``callable: HookContext -> Awaitable[HookDecision]``
    - subprocess: ``script_path: str`` (JSON via stdin/stdout)
    - http: ``url: str`` (POST + JSON)
    - llm: ``model: str`` + ``prompt: str`` (DI to LLMRouter)

Spec is parsed from a settings string format (see ``parse_spec``).

Trust boundary: stdlib + dataclasses only.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Union

from harness.hooks.events import EventType


HookTransport = Literal["builtin", "subprocess", "http", "llm"]


# A builtin hook is just a Python async callable.
BuiltinHook = Callable[["HookContext"], Awaitable["HookDecision"]]


@dataclass(frozen=True)
class HookSpec:
    """Registration record for a single hook.

    Attributes:
        hook_id: Unique identifier. Format: ``<transport>.<name>``
            where ``<name>`` is auto-generated or user-supplied.
        event: The event this hook listens to.
        transport: One of ``"builtin"`` / ``"subprocess"`` / ``"http"`` / ``"llm"``.
        enabled: If False, the hook is skipped on dispatch.
        timeout_ms: Per-hook timeout. Default ``settings.hooks_default_max_ms``.
        priority: Lower = runs first (default 100). Block decisions short-circuit
            higher-priority hooks.
        matcher: Optional glob-style filter on context fields. Format
            ``"<field>=<pattern>,<field>=<pattern>"``. See
            ``harness.hooks.filter_chain`` for the matching rules.
        # Builtin-only
        callable: Async function (only for transport=builtin).
        # Subprocess-only
        script_path: Path to the script (only for transport=subprocess).
        # HTTP-only
        url: Full URL (only for transport=http).
        headers: HTTP headers (only for transport=http).
        # LLM-only
        model: LLM model id (only for transport=llm).
        prompt: Prompt template (only for transport=llm). Supports
            ``{event}`` and ``{payload}`` placeholders.
    """

    hook_id: str
    event: EventType
    transport: HookTransport
    enabled: bool = True
    timeout_ms: int | None = None
    priority: int = 100
    matcher: str = ""
    # Builtin
    callable: BuiltinHook | None = None
    # Subprocess
    script_path: str = ""
    # HTTP
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    # LLM
    model: str = ""
    prompt: str = ""


# === Spec string parsing ===
# Format: "<EventType>:<transport>:<args...>[:<timeout_ms>]"
# Examples:
#   "PreToolUse:builtin:validate"   -> builtin hook named "validate"
#   "PreToolUse:subprocess:/path/to/hook.py:3000"
#   "OnRoutingDecision:http:https://example.com/hook:5000:Bearer abc"
#   "OnRoutingDecision:llm:qwen3:8b:3000:Decide whether to override"
_SPEC_STRING_RE = re.compile(
    r"^(?P<event>[A-Za-z_]+):(?P<transport>builtin|subprocess|http|llm):(?P<rest>.+)$"
)


def parse_spec(spec_string: str, *, hook_id_prefix: str = "user") -> HookSpec:
    """Parse a settings string into a ``HookSpec``.

    Format (canonical):
        ``<EventType>:<transport>:<args...>[:<timeout_ms>]``

    - builtin:    ``PreToolUse:builtin:log``                       (1 arg)
    - subprocess: ``PreToolUse:subprocess:/path/to/hook.py:3000``  (1 arg + timeout)
    - http:       ``PreToolUse:http:https://example.com/h:5000`` (timeout)
                  ``PreToolUse:http:https://example.com/h:Bearer abc`` (auth)
                  (URL is recognised by the ``://`` separator; tail is
                  EITHER a timeout OR an auth header, NOT both — timeout
                  must be last digit-token in the tail)
    - llm:        ``OnRoutingDecision:llm:qwen3:8b:3000:Decide whether to override``
                  (model + timeout + prompt; prompt may contain ``:``)

    Raises ``ValueError`` on malformed input.
    """
    m = _SPEC_STRING_RE.match(spec_string)
    if not m:
        raise ValueError(
            f"Invalid hook spec: {spec_string!r}. "
            f"Expected '<EventType>:<transport>:<args...>[:<timeout_ms>]'"
        )
    event = EventType(m.group("event"))
    transport: HookTransport = m.group("transport")  # type: ignore[assignment]
    rest = m.group("rest")

    def _pop_timeout(parts: list[str]) -> tuple[list[str], int | None]:
        if parts and parts[-1].isdigit():
            return parts[:-1], int(parts[-1])
        return parts, None

    if transport == "builtin":
        parts = rest.split(":")
        parts, timeout_ms = _pop_timeout(parts)
        if len(parts) != 1:
            raise ValueError(
                f"Invalid builtin spec: {spec_string!r}. "
                f"Expected 'PreToolUse:builtin:<hook_name>'"
            )
        return HookSpec(
            hook_id=f"{hook_id_prefix}.builtin.{parts[0]}",
            event=event,
            transport=transport,
            timeout_ms=timeout_ms,
        )
    if transport == "subprocess":
        parts = rest.split(":")
        parts, timeout_ms = _pop_timeout(parts)
        if len(parts) != 1:
            raise ValueError(
                f"Invalid subprocess spec: {spec_string!r}. "
                f"Expected 'PreToolUse:subprocess:<script_path>[:<timeout_ms>]'"
            )
        return HookSpec(
            hook_id=f"{hook_id_prefix}.subprocess.{len(parts[0])}",
            event=event,
            transport=transport,
            timeout_ms=timeout_ms,
            script_path=parts[0],
        )
    if transport == "http":
        # URL contains '://', so split around it.
        if "://" not in rest:
            raise ValueError(
                f"Invalid http spec: {spec_string!r}. "
                f"Expected 'PreToolUse:http:<url>[:<timeout_ms>][:<auth>]'"
            )
        scheme_end = rest.index("://") + 3
        url_end = rest.find(":", scheme_end)
        if url_end == -1:
            url = rest
            tail = ""
        else:
            url = rest[:url_end]
            tail = rest[url_end + 1 :]
        tail_parts = tail.split(":") if tail else []
        tail_parts, timeout_ms = _pop_timeout(tail_parts)
        headers: dict[str, str] = {}
        if tail_parts:
            headers["Authorization"] = ":".join(tail_parts)
        return HookSpec(
            hook_id=f"{hook_id_prefix}.http.{url}",
            event=event,
            transport=transport,
            timeout_ms=timeout_ms,
            url=url,
            headers=headers,
        )
    if transport == "llm":
        # First ':' separates model. Then we expect
        # ``<timeout_ms>:<prompt>`` where prompt may contain ':'.
        first_colon = rest.find(":")
        if first_colon == -1:
            raise ValueError(
                f"Invalid llm spec: {spec_string!r}. "
                f"Expected 'OnRoutingDecision:llm:<model>:<timeout_ms>:<prompt>'"
            )
        model = rest[:first_colon]
        tail = rest[first_colon + 1 :]
        # Timeout is the FIRST numeric token in tail.
        tail_parts = tail.split(":")
        timeout_ms: int | None = None
        prompt_parts: list[str] = []
        seen_timeout = False
        for part in tail_parts:
            if not seen_timeout and part.isdigit():
                timeout_ms = int(part)
                seen_timeout = True
            else:
                prompt_parts.append(part)
        if timeout_ms is None:
            raise ValueError(
                f"Invalid llm spec: {spec_string!r}. Missing timeout_ms."
            )
        if not prompt_parts:
            raise ValueError(
                f"Invalid llm spec: {spec_string!r}. Missing prompt."
            )
        prompt = ":".join(prompt_parts)
        return HookSpec(
            hook_id=f"{hook_id_prefix}.llm.{model}",
            event=event,
            transport=transport,
            timeout_ms=timeout_ms,
            model=model,
            prompt=prompt,
        )

    raise ValueError(f"Unknown transport: {transport!r}")


class HookRegistry:
    """In-memory registry of ``HookSpec`` keyed by event.

    Thread-safety: reads return snapshots; writes are serialised
    via an asyncio lock (so the registry can be modified from
    FastAPI startup/lifespan without races).

    Example::

        registry = HookRegistry()
        registry.register(HookSpec(
            hook_id="builtin.log",
            event=EventType.PRE_TOOL_USE,
            transport="builtin",
            callable=log_hook,
        ))
        specs = registry.for_event(EventType.PRE_TOOL_USE)
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._specs: dict[EventType, list[HookSpec]] = {}

    async def register(self, spec: HookSpec) -> None:
        """Add a spec. Existing spec with same ``hook_id`` is replaced."""
        async with self._lock:
            self._specs.setdefault(spec.event, [])
            # Replace if hook_id exists, else append.
            existing = self._specs[spec.event]
            for i, s in enumerate(existing):
                if s.hook_id == spec.hook_id:
                    existing[i] = spec
                    return
            existing.append(spec)
            existing.sort(key=lambda s: s.priority)

    async def unregister(self, hook_id: str) -> bool:
        """Remove a spec by id. Returns True if removed."""
        async with self._lock:
            for event, specs in self._specs.items():
                for i, s in enumerate(specs):
                    if s.hook_id == hook_id:
                        del specs[i]
                        return True
            return False

    async def set_enabled(self, hook_id: str, enabled: bool) -> bool:
        """Enable or disable a hook in place. Returns True if found."""
        async with self._lock:
            for specs in self._specs.values():
                for i, s in enumerate(specs):
                    if s.hook_id == hook_id:
                        self._specs[s.event][i] = s.__class__(
                            **{**s.__dict__, "enabled": enabled}
                        )
                        return True
            return False

    def for_event(self, event: EventType) -> list[HookSpec]:
        """Snapshot of specs for an event (sorted by priority, ascending)."""
        return list(self._specs.get(event, []))

    def all_specs(self) -> list[HookSpec]:
        """Snapshot of all registered specs."""
        out: list[HookSpec] = []
        for specs in self._specs.values():
            out.extend(specs)
        return out

    def __len__(self) -> int:
        return sum(len(s) for s in self._specs.values())

    def __contains__(self, hook_id: str) -> bool:
        return any(s.hook_id == hook_id for s in self.all_specs())


# === Phase 4.4: process-level singleton for the CLI ===

# Lazy import to avoid a top-level ``harness.hooks.builtin`` import
# (which would in turn import observability helpers from some hooks).
# CLI callers should use :func:`get_registry` to obtain the singleton
# (with all builtin specs pre-loaded). The server uses its own
# ``app_state["hook_registry"]`` and does NOT call this helper.

_instance: "HookRegistry | None" = None
_builtin_loaded: bool = False


def _load_builtin_specs(registry: "HookRegistry") -> None:
    """Register all 7 builtin hooks as ``HookSpec`` into ``registry``.

    Sync registration because the CLI is not async. The builtin
    hooks themselves are still async callables — we just attach
    their references to the spec for ``hooks show`` introspection.

    Idempotent: a second call is a no-op (``_builtin_loaded`` guard).
    """
    global _builtin_loaded
    if _builtin_loaded:
        return
    # Lazy import — the builtin hooks may transitively import
    # observability helpers.
    from harness.hooks.builtin import BUILTIN_HOOKS

    # Mirror the wiring documented in ``harness/hooks/builtin/__init__.py``
    # and exercised by the harness test suite. Priority is the
    # registry default (100). Each spec gets a unique hook_id.
    defaults = {
        "log":              ("PreToolUse",  "log"),
        "validate":         ("PreToolUse",  "validate"),
        "block_dangerous":  ("PreToolUse",  "block_dangerous"),
        "inject_context":   ("UserPromptSubmit", "inject_context"),
        "autosave":         ("SessionEnd",  "autosave"),
        "confirm_dangerous":("Elicitation", "confirm_dangerous"),
        "notify_terminal":  ("Notification","notify_terminal"),
    }
    for name, (event_name, hook_name) in defaults.items():
        callable_obj = BUILTIN_HOOKS.get(hook_name)
        spec = HookSpec(
            hook_id=f"builtin.{hook_name}",
            event=EventType(event_name),
            transport="builtin",
            enabled=True,
            priority=100,
            matcher="",
            callable=callable_obj,
        )
        # Sync register (we are in a CLI process; no running loop).
        existing = registry._specs.setdefault(spec.event, [])
        for i, s in enumerate(existing):
            if s.hook_id == spec.hook_id:
                existing[i] = spec
                break
        else:
            existing.append(spec)
            existing.sort(key=lambda s: s.priority)
    _builtin_loaded = True


def get_registry() -> "HookRegistry":
    """Return the process-level HookRegistry singleton.

    The singleton starts empty. Builtin specs are loaded on the
    first call (sync, no event loop). Use :func:`reset_registry`
    in tests to clear state.

    The server does NOT use this helper — it constructs its own
    ``HookRegistry`` and holds it in ``app_state``. This singleton
    is for the CLI's local inspection use case only.
    """
    global _instance
    if _instance is None:
        _instance = HookRegistry()
        _load_builtin_specs(_instance)
    return _instance


def reset_registry() -> None:
    """Reset the singleton. For tests only."""
    global _instance, _builtin_loaded
    _instance = None
    _builtin_loaded = False


__all__ = [
    "HookTransport",
    "BuiltinHook",
    "HookSpec",
    "HookRegistry",
    "parse_spec",
    "get_registry",
    "reset_registry",
]
