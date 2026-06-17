"""Phase 4.8 v1.18.0: Per-hook rate limiter + circuit breaker.

Two complementary defences against hook-induced DoS:

1. **TokenBucket** / **HookRateLimiter** — per-hook token bucket that
   caps the sustained dispatch rate. Each ``consume(n)`` decrements
   the bucket; refill happens lazily on the next ``consume`` based
   on wall-clock elapsed time. With defaults (capacity=60,
   refill_per_sec=1.0), a hook can burst up to 60 dispatches, then
   sustain 1/sec.

2. **CircuitBreaker** / **HookCircuitBreaker** — per-hook circuit
   breaker that opens after N consecutive failures, then half-opens
   after a cooldown. In ``half_open`` the breaker allows ONE probe
   call: a success closes the circuit, a failure re-opens it for
   another cooldown window.

Both wrappers are thread-safe (``threading.Lock``) and intended to
be invoked from ``HookRunner._dispatch_one`` BEFORE the actual
hook transport fires. When either defence skips a hook, the
runner records a metric and proceeds to the NEXT hook (skips do
NOT abort the whole dispatch — other hooks for the same event
still fire).

Trust boundary: stdlib + dataclasses + threading only. NO
``harness.agents`` or ``harness.server`` imports.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)


# === Token Bucket ===


@dataclass
class TokenBucket:
    """Single-bucket token rate limiter.

    Attributes:
        capacity: Maximum tokens the bucket can hold (burst size).
        refill_per_sec: Sustained refill rate (tokens / second).
        tokens: Current token count (float — partial tokens allowed).
        last_refill: ``time.monotonic()`` timestamp of last refill.

    Thread-safety: the bucket itself is NOT thread-safe. Callers
    must hold the owning ``HookRateLimiter._lock`` when mutating.
    """

    capacity: float
    refill_per_sec: float
    tokens: float = field(default=0.0)
    last_refill: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        # Start full so the first burst is permitted.
        if self.tokens <= 0.0:
            self.tokens = self.capacity

    def _refill(self, *, now: float | None = None) -> None:
        """Lazily add tokens based on elapsed wall-clock time.

        Idempotent: calling twice in the same instant yields the
        same result (elapsed=0 → no tokens added).
        """
        current = now if now is not None else time.monotonic()
        elapsed = current - self.last_refill
        if elapsed <= 0.0:
            # Clock went backwards or no time passed — keep last_refill.
            return
        self.tokens = min(
            self.capacity,
            self.tokens + elapsed * self.refill_per_sec,
        )
        self.last_refill = current

    def consume(self, n: float = 1.0, *, now: float | None = None) -> bool:
        """Try to consume ``n`` tokens.

        Returns ``True`` if the bucket had enough tokens (and they
        were decremented), ``False`` otherwise (bucket unchanged
        except for the lazy refill).
        """
        self._refill(now=now)
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False


# === Circuit Breaker ===


CircuitState = Literal["closed", "open", "half_open"]
CircuitDecision = Literal["allow", "skip"]
CircuitSkipReason = Literal["circuit_open", "half_open"]


@dataclass
class CircuitBreaker:
    """Per-target circuit breaker with three states.

    States:
        closed    — normal operation; failures increment the count.
        open      — all calls skipped until cooldown elapses.
        half_open — ONE probe call allowed; outcome decides next state.

    Thread-safety: the breaker itself is NOT thread-safe. Callers
    must hold the owning ``HookCircuitBreaker._lock`` when mutating.
    """

    threshold: int
    cooldown_s: float
    state: CircuitState = "closed"
    failure_count: int = 0
    opened_at: float = 0.0

    def _check_state_transition(self, *, now: float | None = None) -> None:
        """Transition ``open`` → ``half_open`` after cooldown elapses.

        Called from ``check()`` before reading ``state``. No-op in
        ``closed`` / ``half_open``.
        """
        if self.state != "open":
            return
        current = now if now is not None else time.monotonic()
        if current - self.opened_at >= self.cooldown_s:
            self.state = "half_open"
            logger.debug(
                "Circuit transitioned open -> half_open after %.2fs cooldown",
                current - self.opened_at,
            )

    def check(
        self, *, now: float | None = None
    ) -> tuple[CircuitDecision, str]:
        """Decide whether the caller may proceed.

        Returns:
            ("allow", "")           — caller may proceed.
            ("skip", "circuit_open") — circuit is open; skip.
            ("skip", "half_open")   — circuit is half-open and the
              single probe slot is already taken this round; skip.
              (The caller that received ``("allow", "")`` from a
              half-open circuit IS the probe.)
        """
        self._check_state_transition(now=now)
        if self.state == "closed":
            return ("allow", "")
        if self.state == "half_open":
            # Allow exactly one probe call per half-open window.
            # We distinguish "the probe" from "concurrent callers"
            # by returning allow for the first and skip for the rest.
            # Since the lock is held by the caller (HookCircuitBreaker),
            # serialisation is guaranteed. We mark the probe as taken
            # by transitioning to a sentinel: the probe's outcome
            # (record_success / record_failure) closes or reopens.
            # To keep the API simple, the first check() in half_open
            # returns allow and flips an internal flag.
            if not getattr(self, "_probe_taken", False):
                self._probe_taken = True  # type: ignore[attr-defined]
                return ("allow", "")
            return ("skip", "half_open")
        # state == "open"
        return ("skip", "circuit_open")

    def record_failure(self, *, now: float | None = None) -> None:
        """Record a failure. Opens the circuit if threshold is reached.

        In ``half_open`` a single failure immediately re-opens the
        circuit (resets the cooldown window).
        """
        current = now if now is not None else time.monotonic()
        if self.state == "half_open":
            # Probe failed — re-open with a fresh cooldown.
            self.state = "open"
            self.opened_at = current
            self._probe_taken = False  # type: ignore[attr-defined]
            logger.debug("Circuit half_open -> open (probe failed)")
            return
        self.failure_count += 1
        if self.state == "closed" and self.failure_count >= self.threshold:
            self.state = "open"
            self.opened_at = current
            logger.debug(
                "Circuit closed -> open after %d failures (threshold=%d)",
                self.failure_count,
                self.threshold,
            )

    def record_success(self) -> None:
        """Record a success. Closes the circuit if half-open.

        In ``closed`` state this resets the failure count (a success
        streak clears the accumulated failures).
        """
        if self.state == "half_open":
            self.state = "closed"
            self.failure_count = 0
            self._probe_taken = False  # type: ignore[attr-defined]
            logger.debug("Circuit half_open -> closed (probe succeeded)")
            return
        # closed: reset failure count on success.
        if self.failure_count > 0:
            self.failure_count = 0


# === Per-hook wrappers (thread-safe) ===


class HookRateLimiter:
    """Thread-safe per-hook rate limiter.

    Buckets are created lazily on first ``check()`` for a given
    ``hook_id``. All buckets share the same capacity / refill rate
    (set at construction from ``settings.hooks_rate_limit_*``).
    """

    def __init__(self, *, capacity: float, refill_per_sec: float) -> None:
        self._capacity = capacity
        self._refill_per_sec = refill_per_sec
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def check(self, hook_id: str, *, now: float | None = None) -> bool:
        """Try to consume 1 token for ``hook_id``.

        Returns ``True`` if allowed, ``False`` if rate-limited.
        """
        with self._lock:
            bucket = self._buckets.get(hook_id)
            if bucket is None:
                bucket = TokenBucket(
                    capacity=self._capacity,
                    refill_per_sec=self._refill_per_sec,
                )
                self._buckets[hook_id] = bucket
            return bucket.consume(now=now)

    def get_bucket(self, hook_id: str) -> TokenBucket | None:
        """Inspect a bucket (for tests). Returns None if not yet created."""
        with self._lock:
            return self._buckets.get(hook_id)


class HookCircuitBreaker:
    """Thread-safe per-hook circuit breaker.

    Circuits are created lazily on first ``check()`` / ``record_*()``
    for a given ``hook_id``. All circuits share the same threshold /
    cooldown (set at construction from
    ``settings.hooks_circuit_breaker_*``).
    """

    def __init__(self, *, threshold: int, cooldown_s: float) -> None:
        self._threshold = threshold
        self._cooldown_s = cooldown_s
        self._circuits: dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def _get_or_create(self, hook_id: str) -> CircuitBreaker:
        circuit = self._circuits.get(hook_id)
        if circuit is None:
            circuit = CircuitBreaker(
                threshold=self._threshold,
                cooldown_s=self._cooldown_s,
            )
            self._circuits[hook_id] = circuit
        return circuit

    def check(
        self, hook_id: str, *, now: float | None = None
    ) -> tuple[CircuitDecision, str]:
        """Check whether ``hook_id`` may dispatch.

        Returns ``("allow", "")`` or ``("skip", <reason>)``.
        """
        with self._lock:
            circuit = self._get_or_create(hook_id)
            return circuit.check(now=now)

    def record_failure(self, hook_id: str, *, now: float | None = None) -> None:
        with self._lock:
            circuit = self._get_or_create(hook_id)
            circuit.record_failure(now=now)

    def record_success(self, hook_id: str) -> None:
        with self._lock:
            circuit = self._get_or_create(hook_id)
            circuit.record_success()

    def get_circuit(self, hook_id: str) -> CircuitBreaker | None:
        """Inspect a circuit (for tests). Returns None if not yet created."""
        with self._lock:
            return self._circuits.get(hook_id)


__all__ = [
    "TokenBucket",
    "CircuitBreaker",
    "HookRateLimiter",
    "HookCircuitBreaker",
    "CircuitState",
    "CircuitDecision",
    "CircuitSkipReason",
]
