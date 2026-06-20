"""WI-04: MetricsBroker — in-memory pub/sub broker for WebSocket clients.

Thread-safe (asyncio.Lock) broker with backpressure (queue > max_backlog
→ drop oldest). Topics: "metrics", "health", "audit".

Trust boundary: stdlib + asyncio only. No imports from harness.agents,
harness.server, or harness.hooks.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class _SubscriberState:
    """Per-subscriber state tracked by the broker."""

    session_id: str
    topics: set[str]
    queue: asyncio.Queue[dict[str, Any]]
    connected_at: float = field(default_factory=time.monotonic)


@dataclass
class BrokerStats:
    """Snapshot of broker internal state — returned by :meth:`MetricsBroker.stats`."""

    subscriber_count: int = 0
    topic_count: int = 0
    total_published: int = 0
    total_dropped: int = 0
    max_backlog: int = 0
    topics: dict[str, int] = field(default_factory=dict)
    uptime_seconds: float = 0.0


class MetricsBroker:
    """In-memory pub/sub broker for WebSocket clients.

    Usage::

        broker = MetricsBroker(max_backlog=100)
        broker.subscribe("session-1", ["metrics", "health"])
        broker.publish("metrics", {"cpu": 0.42})

    Thread-safe via ``asyncio.Lock``.  Backpressure: if a subscriber's
    queue exceeds ``max_backlog``, the oldest message is dropped.
    """

    def __init__(self, max_backlog: int = 100) -> None:
        if max_backlog < 1:
            raise ValueError(f"max_backlog must be >= 1, got {max_backlog}")
        self._max_backlog = max_backlog
        self._lock = asyncio.Lock()
        self._subscribers: dict[str, _SubscriberState] = {}
        self._topic_index: dict[str, set[str]] = {}  # topic → set of session_ids
        self._total_published: int = 0
        self._total_dropped: int = 0
        self._started_at: float = time.monotonic()

    # === Public API ========================================================

    async def subscribe(self, session_id: str, topics: list[str]) -> None:
        """Register or update a subscriber.

        If ``session_id`` is already subscribed, its topics are replaced.
        """
        if not session_id or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        if not topics:
            raise ValueError("topics must be non-empty")

        topic_set = set(topics)
        async with self._lock:
            old = self._subscribers.get(session_id)
            if old is not None:
                # Remove old topic bindings.
                for t in old.topics:
                    if t in self._topic_index:
                        self._topic_index[t].discard(session_id)
                        if not self._topic_index[t]:
                            del self._topic_index[t]
                old.topics = topic_set
            else:
                old = _SubscriberState(
                    session_id=session_id,
                    topics=topic_set,
                    queue=asyncio.Queue(maxsize=0),  # unbounded — we drop manually
                )
                self._subscribers[session_id] = old

            # Add new topic bindings.
            for t in topic_set:
                self._topic_index.setdefault(t, set()).add(session_id)

            logger.debug(
                "broker: %s subscribed to %s (total subscribers: %d)",
                session_id, sorted(topic_set), len(self._subscribers),
            )

    async def unsubscribe(self, session_id: str) -> None:
        """Remove a subscriber and drain its queue."""
        async with self._lock:
            sub = self._subscribers.pop(session_id, None)
            if sub is None:
                return
            for t in sub.topics:
                idx = self._topic_index.get(t)
                if idx is not None:
                    idx.discard(session_id)
                    if not idx:
                        del self._topic_index[t]
            # Drain the queue so no references linger.
            while not sub.queue.empty():
                try:
                    sub.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            logger.debug(
                "broker: %s unsubscribed (total subscribers: %d)",
                session_id, len(self._subscribers),
            )

    async def publish(self, topic: str, data: dict[str, Any]) -> int:
        """Publish a message to all subscribers of ``topic``.

        Returns the number of subscribers the message was delivered to.
        Messages are cloned per-subscriber (shallow dict copy) so
        callers can mutate ``data`` after the call.
        """
        async with self._lock:
            session_ids = self._topic_index.get(topic, set())
            if not session_ids:
                self._total_published += 1
                return 0

            delivered = 0
            for sid in list(session_ids):
                sub = self._subscribers.get(sid)
                if sub is None:
                    continue
                try:
                    # Backpressure: drop oldest if queue is over limit.
                    if sub.queue.qsize() >= self._max_backlog:
                        try:
                            sub.queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                        self._total_dropped += 1
                        logger.debug(
                            "broker: dropped oldest message for %s (qsize=%d >= %d)",
                            sid, sub.queue.qsize(), self._max_backlog,
                        )
                    sub.queue.put_nowait({"type": topic, "data": dict(data)})
                    delivered += 1
                except asyncio.QueueFull:
                    # Shouldn't happen with maxsize=0, but defensive.
                    self._total_dropped += 1
            self._total_published += 1
            return delivered

    def get_subscribers(self, topic: str) -> list[str]:
        """Return the list of session_ids subscribed to ``topic`` (snapshot)."""
        return sorted(self._topic_index.get(topic, set()))

    # === Subscriber-facing API =============================================

    async def recv(self, session_id: str, timeout: float | None = None) -> dict[str, Any] | None:
        """Wait for the next message for ``session_id``.

        Args:
            session_id: Subscriber to read from.
            timeout: If set, return ``None`` after this many seconds.

        Returns:
            The next message dict, or ``None`` on timeout.
        """
        sub = self._subscribers.get(session_id)
        if sub is None:
            return None
        try:
            if timeout is not None:
                return await asyncio.wait_for(sub.queue.get(), timeout=timeout)
            return await sub.queue.get()
        except asyncio.TimeoutError:
            return None

    def is_subscribed(self, session_id: str) -> bool:
        """Return True if ``session_id`` is currently subscribed."""
        return session_id in self._subscribers

    # === Stats =============================================================

    def stats(self) -> BrokerStats:
        """Return a snapshot of broker internal state.

        Safe to call without the lock (moment-in-time snapshot).
        """
        topic_counts = {t: len(s) for t, s in self._topic_index.items()}
        return BrokerStats(
            subscriber_count=len(self._subscribers),
            topic_count=len(self._topic_index),
            total_published=self._total_published,
            total_dropped=self._total_dropped,
            max_backlog=self._max_backlog,
            topics=topic_counts,
            uptime_seconds=round(time.monotonic() - self._started_at, 3),
        )


__all__ = ["MetricsBroker", "BrokerStats"]
