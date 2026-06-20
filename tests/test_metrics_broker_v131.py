"""WI-04: Tests for MetricsBroker — pub/sub, backpressure, topic filtering.

Mirrors the broker patterns from :class:`ElicitationBroker` tests
(test_elicitation_broker.py), but for the MetricsBroker pub/sub model.
"""
from __future__ import annotations

import asyncio

import pytest

from harness.observability.metrics_broker import BrokerStats, MetricsBroker


class TestMetricsBroker:
    """Unit tests for MetricsBroker."""

    # ── 1. subscribe + publish → subscriber receives message ──────────

    @pytest.mark.asyncio
    async def test_subscribe_publish_receive(self) -> None:
        """Subscriber receives published message on matching topic."""
        broker = MetricsBroker(max_backlog=10)
        await broker.subscribe("session-1", ["metrics"])

        await broker.publish("metrics", {"cpu": 0.42, "mem": 0.73})
        msg = await broker.recv("session-1", timeout=0.5)
        assert msg is not None
        assert msg["type"] == "metrics"
        assert msg["data"] == {"cpu": 0.42, "mem": 0.73}

    # ── 2. unsubscribe → stops receiving ──────────────────────────────

    @pytest.mark.asyncio
    async def test_unsubscribe_stops_receiving(self) -> None:
        """After unsubscribe(), subscriber no longer receives messages."""
        broker = MetricsBroker(max_backlog=10)
        await broker.subscribe("session-1", ["metrics"])
        await broker.unsubscribe("session-1")

        await broker.publish("metrics", {"cpu": 0.42})
        msg = await broker.recv("session-1", timeout=0.2)
        assert msg is None  # Not subscribed anymore.

    # ── 3. backpressure → drops oldest when queue full ────────────────

    @pytest.mark.asyncio
    async def test_backpressure_drops_oldest(self) -> None:
        """When queue exceeds max_backlog, oldest message is dropped."""
        broker = MetricsBroker(max_backlog=3)
        await broker.subscribe("session-1", ["metrics"])

        # Fill queue: 3 messages.
        for i in range(3):
            await broker.publish("metrics", {"seq": i})

        # Queue is full (size 3). Next publish should drop seq=0.
        await broker.publish("metrics", {"seq": 3})

        # Read all messages. The first should be seq=1 (seq=0 dropped).
        msgs: list[int] = []
        for _ in range(3):
            msg = await broker.recv("session-1", timeout=0.2)
            if msg is not None:
                msgs.append(msg["data"]["seq"])

        assert msgs == [1, 2, 3], f"expected seq [1,2,3], got {msgs}"

    # ── 4. multiple subscribers → all receive broadcast ───────────────

    @pytest.mark.asyncio
    async def test_multiple_subscribers_all_receive(self) -> None:
        """Multiple subscribers on same topic all get the message."""
        broker = MetricsBroker(max_backlog=10)
        for sid in ("s1", "s2", "s3"):
            await broker.subscribe(sid, ["metrics"])

        await broker.publish("metrics", {"broadcast": True})

        for sid in ("s1", "s2", "s3"):
            msg = await broker.recv(sid, timeout=0.2)
            assert msg is not None, f"{sid} did not receive message"
            assert msg["data"]["broadcast"] is True

    # ── 5. topic filtering → only matching topics received ────────────

    @pytest.mark.asyncio
    async def test_topic_filtering(self) -> None:
        """Subscriber only receives messages on topics they subscribed to."""
        broker = MetricsBroker(max_backlog=10)
        await broker.subscribe("s-health", ["health"])
        await broker.subscribe("s-metrics", ["metrics"])

        await broker.publish("metrics", {"cpu": 0.5})
        await broker.publish("health", {"status": "ok"})

        # s-health should receive only the health message.
        msg = await broker.recv("s-health", timeout=0.2)
        assert msg is not None
        assert msg["type"] == "health"

        # s-health should NOT receive a metrics message.
        msg2 = await broker.recv("s-health", timeout=0.1)
        assert msg2 is None

        # s-metrics should receive only the metrics message.
        msg3 = await broker.recv("s-metrics", timeout=0.2)
        assert msg3 is not None
        assert msg3["type"] == "metrics"

    # ── 6. broker stats tracking ──────────────────────────────────────

    def test_broker_stats(self) -> None:
        """stats() returns a BrokerStats with correct counters."""
        broker = MetricsBroker(max_backlog=10)
        stats = broker.stats()
        assert isinstance(stats, BrokerStats)
        assert stats.subscriber_count == 0
        assert stats.topic_count == 0
        assert stats.total_published == 0
        assert stats.total_dropped == 0
        assert stats.max_backlog == 10
        assert stats.uptime_seconds >= 0.0

    @pytest.mark.asyncio
    async def test_stats_after_activity(self) -> None:
        """stats() reflects publish and subscriber activity."""
        broker = MetricsBroker(max_backlog=10)
        await broker.subscribe("s1", ["metrics", "health"])
        await broker.publish("metrics", {"v": 1})
        await broker.publish("metrics", {"v": 2})
        await broker.publish("health", {"v": 3})

        stats = broker.stats()
        assert stats.subscriber_count == 1
        assert stats.topic_count == 2  # metrics + health
        assert stats.total_published == 3

    def test_get_subscribers(self) -> None:
        """get_subscribers() returns session_ids for a topic."""
        broker = MetricsBroker(max_backlog=10)
        # Sync subscribe via manual setup.
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(broker.subscribe("s1", ["metrics"]))
            loop.run_until_complete(broker.subscribe("s2", ["metrics", "health"]))
        finally:
            loop.close()

        subs = broker.get_subscribers("metrics")
        assert set(subs) == {"s1", "s2"}
        assert broker.get_subscribers("health") == ["s2"]
        assert broker.get_subscribers("audit") == []

    def test_is_subscribed(self) -> None:
        """is_subscribed() returns True iff subscriber is registered."""
        broker = MetricsBroker(max_backlog=10)
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(broker.subscribe("s1", ["metrics"]))
        finally:
            loop.close()

        assert broker.is_subscribed("s1") is True
        assert broker.is_subscribed("s-nonexistent") is False

    @pytest.mark.asyncio
    async def test_recv_timeout_returns_none(self) -> None:
        """recv() with timeout returns None when no message arrives."""
        broker = MetricsBroker(max_backlog=10)
        await broker.subscribe("s1", ["metrics"])
        msg = await broker.recv("s1", timeout=0.05)
        assert msg is None

    @pytest.mark.asyncio
    async def test_resubscribe_updates_topics(self) -> None:
        """Re-subscribing updates the topic set for an existing subscriber."""
        broker = MetricsBroker(max_backlog=10)
        await broker.subscribe("s1", ["metrics"])
        await broker.subscribe("s1", ["health"])  # update topics

        await broker.publish("metrics", {"v": 1})
        # s1 should NOT receive metrics (topic was changed)
        msg = await broker.recv("s1", timeout=0.1)
        assert msg is None

        await broker.publish("health", {"v": 2})
        msg2 = await broker.recv("s1", timeout=0.2)
        assert msg2 is not None
        assert msg2["type"] == "health"
