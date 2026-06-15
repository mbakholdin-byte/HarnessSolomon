"""WebSocket chat endpoint (Шаг 7).

Endpoint: WS /api/chat/ws?session_id=...&model=...

Flow:
  1. Client connects with session_id + model query params.
  2. Server validates both (unknown → error event + close).
  3. Server builds an ``AgentLoop`` + ``ChatSession`` pair and waits
     for messages from the client.
  4. For each ``{"type": "user_message", "content": "..."}`` the
     server:
       * persists the user message,
       * loads the full history,
       * runs ``AgentLoop.run(history, model)``,
       * forwards every event to the client via ``send_json``,
       * persists assistant + tool messages as they stream,
       * closes the turn with a synthetic ``session_done`` event.
  5. The WebSocket stays open — the client can send more user messages
     in the same connection. ``WebSocketDisconnect`` is handled silently.

Safety: the ``AgentLoop`` and ``ToolRuntime`` are used as-is (no
shortcuts). The bash denylist + path sandbox remain in force.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from harness.config import settings
from harness.server.agent.loop import AgentLoop
from harness.server.agent.runtime import ToolRuntime
from harness.server.agent.session import ChatSession
from harness.server.db import sqlite as db_sqlite
from harness.server.llm.models import list_models
from harness.server.llm.router import LLMRouter

logger = logging.getLogger(__name__)

router = APIRouter()


# === Helpers ===

# Cache the set of valid model ids at module load. The catalog is
# static (3 entries); recomputing per-WS-connect would be wasteful.
_MODEL_IDS: frozenset[str] = frozenset(spec.id for spec in list_models())


# === WebSocket endpoint ===

@router.websocket("/ws")
async def chat_ws(websocket: WebSocket, session_id: str, model: str) -> None:
    """WebSocket chat loop.

    Query params:
      session_id: existing session UUID
      model:      model id from the catalog

    Closes the connection on validation failure (after sending one
    error event). Stays open across multiple user messages.
    """
    await websocket.accept()
    logger.info("WS connect: session_id=%s model=%s", session_id, model)

    # --- validate ---
    if model not in _MODEL_IDS:
        await websocket.send_json(
            {"type": "error", "content": f"unknown model: {model}"}
        )
        await websocket.close()
        return

    session_row = await db_sqlite.get_session(session_id)
    if session_row is None:
        await websocket.send_json(
            {"type": "error", "content": f"unknown session: {session_id}"}
        )
        await websocket.close()
        return

    # --- per-connection objects ---
    # Phase 3 v1.4.0: collect SessionEvents as the session progresses
    # so end-of-session reflection can see them. The list is wired
    # into the runtime below; SessionLifecycle.__aexit__ reads it.
    events_collector: list[Any] = []
    # Resolve the reflection loop via the lifespan-wired factory.
    # We import inside the handler to keep the route's module-level
    # import surface small (and to avoid pulling the reflection
    # module into a no-reflection deployment).
    reflection_factory = getattr(
        websocket.app.state, "reflection_factory", None,
    )
    reflection = None
    if reflection_factory is not None:
        try:
            reflection = reflection_factory(
                spec=None, session_id=session_id, scratchpad=None,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort wiring
            logger.warning(
                "WS bootstrap: reflection_factory failed: %s", exc,
            )
            reflection = None
    try:
        # Phase 3 v1.5.0: pick up the privacy_zones filter from
        # ``websocket.app.state`` (set in lifespan). When ``None`` the
        # runtime is a no-op for privacy (backward compat). The filter
        # is shared across all chat sessions and read-only.
        privacy_zones = getattr(websocket.app.state, "privacy_zones", None)
        runtime = ToolRuntime(
            project_root=settings.project_root,
            reflection=reflection,
            events_collector=events_collector,
            privacy_zones=privacy_zones,
        )
        llm_router = LLMRouter()
        # Phase 3: pick up the compactor from app.state (set in
        # ``lifespan``). The compactor is process-wide and safe to
        # share across concurrent WebSocket connections (it carries
        # only config + a router reference; no per-session state).
        compactor = getattr(websocket.app.state, "compactor", None)
        loop_obj = AgentLoop(
            runtime=runtime, router=llm_router, compactor=compactor,
        )
    except Exception as exc:  # noqa: BLE001 - bootstrap must not kill the server
        logger.exception("WS bootstrap failed: session_id=%s", session_id)
        try:
            await websocket.send_json(
                {"type": "error", "content": f"bootstrap failed: {exc}"}
            )
        finally:
            await websocket.close()
        return

    # Phase 3 v1.4.0: the SessionLifecycle wraps the entire receive
    # loop. On disconnect / error / normal close the lifecycle's
    # __aexit__ fires end-of-session reflection (best-effort,
    # fail-open — see ``SessionLifecycle``).
    from harness.server.agent.lifecycle import SessionLifecycle
    lifecycle = SessionLifecycle(
        runtime=runtime,
        events=events_collector,
        settings=settings,
        audit=getattr(websocket.app.state, "compactor", None),
    )

    chat_session = ChatSession(
        session_id=session_id,
        model=model,
        db=db_sqlite,
        project_root=settings.project_root,
        compactor=getattr(websocket.app.state, "compactor", None),
    )

    # --- receive loop ---
    try:
        # Phase 3 v1.4.0: enter the lifecycle context. On exit
        # (disconnect, error, or normal close) ``__aexit__`` fires
        # end-of-session reflection.
        async with lifecycle:
            while True:
                try:
                    data = await websocket.receive_json()
                except WebSocketDisconnect:
                    logger.info("WS client disconnected: session_id=%s", session_id)
                    return

                if not isinstance(data, dict):
                    await websocket.send_json(
                        {"type": "error", "content": "expected JSON object"}
                    )
                    continue

                msg_type = data.get("type")
                # Phase 3 v1.4.0: explicit ``compact`` message type.
                # Client sends ``{"type": "compact", "bypass_cache": false}``
                # to trigger a manual /compact. Server replies with a
                # ``compact_done`` event carrying the saved_tokens count.
                if msg_type == "compact":
                    await _handle_compact_message(
                        websocket=websocket,
                        session_id=session_id,
                        chat_session=chat_session,
                        compact_trigger=getattr(
                            websocket.app.state, "compact_trigger", None,
                        ),
                        bypass_cache=bool(data.get("bypass_cache", False)),
                    )
                    continue
                if msg_type != "user_message":
                    # Silently ignore non-user messages for now (e.g. pings).
                    continue

                content = data.get("content", "")
                if not isinstance(content, str):
                    await websocket.send_json(
                        {"type": "error", "content": "'content' must be a string"}
                    )
                    continue

                try:
                    await _run_one_turn(
                        websocket=websocket,
                        chat_session=chat_session,
                        loop_obj=loop_obj,
                        model=model,
                        user_content=content,
                    )
                except WebSocketDisconnect:
                    logger.info(
                        "WS client disconnected mid-turn: session_id=%s",
                        session_id,
                    )
                    return
                except Exception as exc:  # noqa: BLE001 - surface to client
                    logger.exception("WS turn failed: session_id=%s", session_id)
                    try:
                        await websocket.send_json(
                            {"type": "error", "content": f"{type(exc).__name__}: {exc}"}
                        )
                    except Exception:  # noqa: BLE001 - socket may already be closed
                        pass
                    # Continue serving — don't kill the connection on a single bad turn.

                # Mark end of this turn. WS stays open for the next user message.
                try:
                    await websocket.send_json({"type": "session_done"})
                except WebSocketDisconnect:
                    return
    except WebSocketDisconnect:
        logger.info("WS outer disconnect: session_id=%s", session_id)
        return


# === Compact message handler (Phase 3 v1.4.0) ===

async def _handle_compact_message(
    *,
    websocket: WebSocket,
    session_id: str,
    chat_session: ChatSession,
    compact_trigger: Any,
    bypass_cache: bool,
) -> None:
    """Handle a WS ``{"type": "compact"}`` message.

    Phase 3 v1.4.0: clients can ask for a manual /compact mid-session.
    We load the full history, hand it to the ``CompactTrigger``, and
    send a ``compact_done`` event with the saved tokens + cache hit
    flag. On failure we send ``compact_failed`` so the client can
    retry.
    """
    if compact_trigger is None:
        try:
            await websocket.send_json({
                "type": "compact_failed",
                "error": "compact trigger not wired on server",
            })
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        history = await chat_session.load_history()
    except Exception as exc:  # noqa: BLE001
        try:
            await websocket.send_json({
                "type": "compact_failed",
                "error": f"load_history: {type(exc).__name__}: {exc}",
            })
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        result = await compact_trigger.compact_now(
            history,
            model=getattr(chat_session, "model", "qwen3:8b") or "qwen3:8b",
            session_id=session_id,
            bypass_cache=bypass_cache,
        )
    except Exception as exc:  # noqa: BLE001
        try:
            await websocket.send_json({
                "type": "compact_failed",
                "error": f"{type(exc).__name__}: {exc}",
            })
        except Exception:  # noqa: BLE001
            pass
        return
    if result is None:
        try:
            await websocket.send_json({
                "type": "compact_failed",
                "error": "trigger returned None (see audit log)",
            })
        except Exception:  # noqa: BLE001
            pass
        return
    try:
        await websocket.send_json({
            "type": "compact_done",
            "session_id": session_id,
            "original_tokens": result.original_tokens,
            "compacted_tokens": result.compacted_tokens,
            "saved_tokens": result.saved_tokens,
            "cache_hit": result.cache_hit,
        })
    except Exception:  # noqa: BLE001 — socket may already be closed
        pass


# === Per-turn driver ===

async def _run_one_turn(
    *,
    websocket: WebSocket,
    chat_session: ChatSession,
    loop_obj: AgentLoop,
    model: str,
    user_content: str,
) -> None:
    """Persist user message → run agent loop → persist assistant/tool messages.

    The agent loop yields ``StreamEvent``s (``assistant_message``,
    ``tool_result``, ``error``, ``done``). We forward each one to the
    client over the WebSocket and persist the relevant ones to the DB.
    """
    # 1. Persist user message
    await chat_session.add_message(role="user", content=user_content)

    # 2. Build history and run the loop
    history = await chat_session.load_history()

    async for event in loop_obj.run(messages=history, model=model):
        payload: dict[str, Any] = event.model_dump(exclude_none=True)
        try:
            await websocket.send_json(payload)
        except WebSocketDisconnect:
            # Client went away mid-stream; stop the loop.
            raise

        # 3. Persist assistant / tool messages.
        if event.type == "assistant_message":
            from harness.server.db.models import MessageUsage

            usage_obj: MessageUsage | None = None
            if event.usage:
                usage_obj = MessageUsage(
                    input_tokens=int(event.usage.get("prompt_tokens", 0)),
                    output_tokens=int(event.usage.get("completion_tokens", 0)),
                    cost=float(event.cost or 0.0),
                )
            # If the agent loop also recorded tool_calls on this event
            # they would arrive as a follow-up tool_result; we don't
            # have them here, so we just persist the text.
            await chat_session.add_message(
                role="assistant",
                content=event.content,
                usage=usage_obj,
            )
        elif event.type == "tool_result":
            tc = event.tool_call or {}
            tool_call_id = tc.get("id")
            tool_name = tc.get("name")
            await chat_session.add_message(
                role="tool",
                content=event.content,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
            )
        # 'error' and 'done' events are forwarded to the client but not
        # persisted as separate messages — the assistant message (if
        # any) already carries the actual text.


__all__ = ["router"]
