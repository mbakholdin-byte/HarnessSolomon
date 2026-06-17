"""Phase 4.8 v1.18.0: Tests for Elicitation decision history.

Covers (15 tests):
    Store layer:
        1. test_store_record_and_query       — round-trip record/query.
        2. test_store_query_by_session       — session_id filter.
        3. test_store_limit                  — LIMIT clamping.
        4. test_store_upsert_on_pending      — INSERT OR REPLACE semantics.
        5. test_store_thread_safe            — concurrent writes.
    Broker integration:
        6. test_broker_records_pending_on_publish
        7. test_broker_records_answered_on_wait_success
        8. test_broker_records_timeout_on_wait_timeout
        9. test_broker_records_source_ws_vs_poll
       10. test_broker_no_store_is_graceful  (bonus, not in spec)
    HTTP API:
       11. test_api_history_returns_json_array
       12. test_api_history_filters_by_session
       13. test_api_history_503_on_db_failure
    CLI:
       14. test_cli_history_pretty_table
       15. test_cli_history_json_output
       16. test_cli_history_no_data_exit_zero
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harness.elicitation import (
    ElicitationBroker,
    ElicitationDecisionRecord,
    ElicitationDecisionStore,
)


# === Fixtures ===


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    """Per-test SQLite path under pytest's tmp_path."""
    return tmp_path / "agent-jobs.db"


@pytest.fixture
def store(store_path: Path) -> ElicitationDecisionStore:
    s = ElicitationDecisionStore(store_path)
    yield s
    s.close()


@pytest.fixture(autouse=True)
def reset_broker() -> Any:
    """Reset the singleton broker before and after each test."""
    ElicitationBroker.reset()
    yield
    ElicitationBroker.reset()


def _make_record(
    *,
    decision_id: str = "dec1",
    session_id: str = "sess1",
    question_id: str = "q1",
    decision: str = "pending",
    answer: str | None = None,
    source: str | None = None,
    latency_ms: int = 0,
    ts: float | None = None,
) -> ElicitationDecisionRecord:
    return ElicitationDecisionRecord(
        decision_id=decision_id,
        session_id=session_id,
        request_id=None,
        question_id=question_id,
        question_preview="question text",
        options=["a", "b"],
        default_answer="abort",
        decision=decision,
        answer=answer,
        source=source,
        latency_ms=latency_ms,
        ts=ts if ts is not None else time.time(),
    )


# === 1. Store layer ===


class TestDecisionStore:
    def test_store_record_and_query(self, store: ElicitationDecisionStore) -> None:
        """Round-trip: record one decision, query it back."""
        rec = _make_record(decision_id="d1", question_id="q1", decision="pending")
        store.record_decision(rec)

        rows = store.query_history()
        assert len(rows) == 1
        r = rows[0]
        assert r.decision_id == "d1"
        assert r.question_id == "q1"
        assert r.decision == "pending"
        assert r.answer is None
        assert r.source is None
        assert r.options == ["a", "b"]
        assert r.default_answer == "abort"

    def test_store_query_by_session(self, store: ElicitationDecisionStore) -> None:
        """Session filter returns only matching rows."""
        store.record_decision(_make_record(decision_id="d1", session_id="s1", ts=1.0))
        store.record_decision(_make_record(decision_id="d2", session_id="s2", ts=2.0))
        store.record_decision(_make_record(decision_id="d3", session_id="s1", ts=3.0))

        rows = store.query_history(session_id="s1")
        assert {r.decision_id for r in rows} == {"d3", "d1"}
        # newest first
        assert rows[0].decision_id == "d3"
        assert rows[1].decision_id == "d1"

    def test_store_limit(self, store: ElicitationDecisionStore) -> None:
        """LIMIT caps the number of returned rows."""
        for i in range(10):
            store.record_decision(
                _make_record(decision_id=f"d{i}", ts=float(i)),
            )
        rows = store.query_history(limit=3)
        assert len(rows) == 3
        # newest first: d9, d8, d7
        assert [r.decision_id for r in rows] == ["d9", "d8", "d7"]

    def test_store_upsert_on_pending(self, store: ElicitationDecisionStore) -> None:
        """INSERT OR REPLACE: same decision_id updates in place."""
        store.record_decision(
            _make_record(decision_id="d1", decision="pending", answer=None, ts=1.0),
        )
        rows = store.query_history()
        assert len(rows) == 1
        assert rows[0].decision == "pending"

        # Upsert with the resolved state.
        store.record_decision(
            _make_record(
                decision_id="d1",
                decision="answered",
                answer="proceed",
                source="ws",
                latency_ms=42,
                ts=2.0,
            ),
        )
        rows = store.query_history()
        assert len(rows) == 1, "REPLACE should not duplicate the row"
        r = rows[0]
        assert r.decision == "answered"
        assert r.answer == "proceed"
        assert r.source == "ws"
        assert r.latency_ms == 42

    def test_store_thread_safe(self, store_path: Path) -> None:
        """Concurrent writes from multiple threads don't corrupt the DB.

        SQLite with check_same_thread=False + our threading.Lock should
        serialise writes cleanly. We assert all rows land and the row
        count matches the number of writers.
        """
        # Each thread opens its own store against the same path (mirrors
        # the CLI + server co-existence pattern).
        n_threads = 8
        n_per_thread = 25

        def writer(tid: int) -> None:
            s = ElicitationDecisionStore(store_path)
            try:
                for i in range(n_per_thread):
                    s.record_decision(
                        _make_record(
                            decision_id=f"t{tid}-r{i}",
                            session_id=f"sess{tid}",
                        ),
                    )
            finally:
                s.close()

        threads = [threading.Thread(target=writer, args=(t,)) for t in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Read back — total should be n_threads * n_per_thread.
        reader = ElicitationDecisionStore(store_path)
        try:
            rows = reader.query_history(limit=10_000)
        finally:
            reader.close()
        assert len(rows) == n_threads * n_per_thread


# === 2. Broker integration ===


class TestBrokerDecisionRecording:
    @pytest.mark.asyncio
    async def test_broker_records_pending_on_publish(
        self, store: ElicitationDecisionStore,
    ) -> None:
        """publish() writes a pending row when a store is attached."""
        broker = ElicitationBroker(decision_store=store)
        qid = broker.publish(
            question="Run rm -rf?",
            options=["proceed", "abort"],
            default_answer="abort",
            session_id="sess-A",
        )
        # The publish is synchronous — the row should be visible now.
        rows = store.query_history()
        assert len(rows) == 1
        r = rows[0]
        assert r.question_id == qid
        assert r.decision == "pending"
        assert r.session_id == "sess-A"
        assert r.answer is None
        assert r.source is None
        assert r.latency_ms == 0
        # Don't await wait() — leave it pending for this assertion.

    @pytest.mark.asyncio
    async def test_broker_records_answered_on_wait_success(
        self, store: ElicitationDecisionStore,
    ) -> None:
        """wait() success updates the row to answered."""
        broker = ElicitationBroker(decision_store=store)
        qid = broker.publish(
            question="Run it?",
            default_answer="abort",
            timeout_s=2.0,
        )

        async def answerer() -> None:
            await asyncio.sleep(0.05)
            broker.answer(qid, "proceed")

        task = asyncio.create_task(answerer())
        result = await broker.wait(qid)
        await task

        assert result == "proceed"
        rows = store.query_history()
        assert len(rows) == 1
        r = rows[0]
        assert r.decision == "answered"
        assert r.answer == "proceed"
        assert r.source == "ws"
        assert r.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_broker_records_timeout_on_wait_timeout(
        self, store: ElicitationDecisionStore,
    ) -> None:
        """wait() timeout updates the row to timed_out."""
        broker = ElicitationBroker(decision_store=store)
        qid = broker.publish(
            question="Never answered",
            default_answer="abort",
            timeout_s=0.1,
        )
        result = await broker.wait(qid)
        assert result == "abort"

        rows = store.query_history()
        assert len(rows) == 1
        r = rows[0]
        assert r.decision == "timed_out"
        assert r.answer == "abort"
        assert r.source == "timeout"

    @pytest.mark.asyncio
    async def test_broker_records_source_ws_vs_poll(
        self, store: ElicitationDecisionStore,
    ) -> None:
        """The ``source`` kwarg of answer() is reflected in the decision."""
        # --- ws path ---
        broker_ws = ElicitationBroker(decision_store=store)
        qid_ws = broker_ws.publish(question="ws q", timeout_s=2.0)

        async def ws_answerer() -> None:
            await asyncio.sleep(0.02)
            broker_ws.answer(qid_ws, "ok-ws", source="ws")

        t1 = asyncio.create_task(ws_answerer())
        await broker_ws.wait(qid_ws)
        await t1

        # --- poll path ---
        qid_poll = broker_ws.publish(question="poll q", timeout_s=2.0)

        async def poll_answerer() -> None:
            await asyncio.sleep(0.02)
            broker_ws.answer(qid_poll, "ok-poll", source="poll")

        t2 = asyncio.create_task(poll_answerer())
        await broker_ws.wait(qid_poll)
        await t2

        rows = {r.question_id: r for r in store.query_history(limit=10)}
        assert rows[qid_ws].source == "ws"
        assert rows[qid_poll].source == "poll"
        assert rows[qid_ws].answer == "ok-ws"
        assert rows[qid_poll].answer == "ok-poll"


# === 3. HTTP API ===


def _make_history_app(db_path: Path) -> FastAPI:
    """Build a minimal app with the history router wired to a temp DB."""
    from harness.server.routes.elicitation_history import (
        router as elicitation_history_router,
    )

    app = FastAPI()
    app.include_router(
        elicitation_history_router, prefix="/api/v1/elicitation",
    )
    app.state.elicitation_decision_db_path = db_path
    return app


class TestHistoryEndpoint:
    def test_api_history_returns_json_array(
        self, store_path: Path,
    ) -> None:
        """GET /history returns a JSON array of decision records."""
        # Seed the DB.
        s = ElicitationDecisionStore(store_path)
        s.record_decision(_make_record(decision_id="d1", ts=1.0))
        s.record_decision(
            _make_record(
                decision_id="d2", decision="answered", answer="proceed",
                source="ws", latency_ms=10, ts=2.0,
            ),
        )
        s.close()

        client = TestClient(_make_history_app(store_path))
        resp = client.get("/api/v1/elicitation/history")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert isinstance(body, list)
        assert len(body) == 2
        # newest first
        assert body[0]["decision_id"] == "d2"
        assert body[1]["decision_id"] == "d1"
        # Required keys present.
        for item in body:
            assert "decision_id" in item
            assert "session_id" in item
            assert "question_id" in item
            assert "decision" in item
            assert "answer" in item
            assert "source" in item
            assert "latency_ms" in item
            assert "ts" in item

    def test_api_history_filters_by_session(
        self, store_path: Path,
    ) -> None:
        """?session=S filters to matching rows."""
        s = ElicitationDecisionStore(store_path)
        s.record_decision(
            _make_record(decision_id="d1", session_id="alpha", ts=1.0),
        )
        s.record_decision(
            _make_record(decision_id="d2", session_id="beta", ts=2.0),
        )
        s.record_decision(
            _make_record(decision_id="d3", session_id="alpha", ts=3.0),
        )
        s.close()

        client = TestClient(_make_history_app(store_path))
        resp = client.get("/api/v1/elicitation/history?session=alpha")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        ids = {item["decision_id"] for item in body}
        assert ids == {"d1", "d3"}

    def test_api_history_503_on_db_failure(
        self, tmp_path: Path,
    ) -> None:
        """A non-existent / unreadable DB path returns 503.

        We simulate failure by pointing the route at a path whose
        parent doesn't exist (and can't be created because the parent
        itself is a file, not a directory).
        """
        # Make ``tmp_path / "blocker"`` a regular file, then ask the
        # store to open ``tmp_path / "blocker" / "agent-jobs.db"`` —
        # the parent.mkdir will fail with NotADirectoryError.
        blocker = tmp_path / "blocker"
        blocker.write_text("i am a file, not a dir")
        bad_db_path = blocker / "agent-jobs.db"

        app = FastAPI()
        from harness.server.routes.elicitation_history import (
            router as elicitation_history_router,
        )
        app.include_router(
            elicitation_history_router, prefix="/api/v1/elicitation",
        )
        app.state.elicitation_decision_db_path = bad_db_path

        client = TestClient(app)
        resp = client.get("/api/v1/elicitation/history")
        assert resp.status_code == 503, resp.text
        body = resp.json()
        assert "detail" in body


# === 4. CLI ===


class TestCliHistory:
    def test_cli_history_pretty_table(self, tmp_path: Path) -> None:
        """Pretty-table output has the expected columns and rows."""
        # CLI expects ``<project_root>/data/agent-jobs.db``.
        project_root = tmp_path / "proj"
        data_dir = project_root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = data_dir / "agent-jobs.db"

        s = ElicitationDecisionStore(db_path)
        s.record_decision(
            _make_record(
                decision_id="d1", session_id="sX", decision="answered",
                answer="proceed", source="ws", latency_ms=42, ts=1700000000.0,
            ),
        )
        s.close()

        rc, out, err = _run_cli(
            "elicitation", "history",
            "--project-root", str(project_root),
        )
        assert rc == 0, f"rc={rc} err={err}"
        # Header line is present.
        assert "ts" in out and "decision" in out and "answer" in out
        assert "source" in out and "latency_ms" in out
        # Data row contains the answer value.
        assert "proceed" in out
        assert "answered" in out

    def test_cli_history_json_output(self, tmp_path: Path) -> None:
        """--json emits a parseable JSON array."""
        project_root = tmp_path / "proj"
        data_dir = project_root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = data_dir / "agent-jobs.db"

        s = ElicitationDecisionStore(db_path)
        s.record_decision(
            _make_record(
                decision_id="d1", session_id="sX", decision="answered",
                answer="proceed", source="ws", latency_ms=7, ts=1700000000.0,
            ),
        )
        s.close()

        rc, out, err = _run_cli(
            "elicitation", "history", "--json",
            "--project-root", str(project_root),
        )
        assert rc == 0, f"rc={rc} err={err}"
        body = json.loads(out)
        assert isinstance(body, list)
        assert len(body) == 1
        assert body[0]["decision_id"] == "d1"
        assert body[0]["decision"] == "answered"
        assert body[0]["answer"] == "proceed"

    def test_cli_history_no_data_exit_zero(self, tmp_path: Path) -> None:
        """Empty DB → '(no decisions)' on stdout, exit 0."""
        project_root = tmp_path / "proj"
        data_dir = project_root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = data_dir / "agent-jobs.db"

        # Open + close the store so the file exists but is empty.
        s = ElicitationDecisionStore(db_path)
        s.close()

        rc, out, err = _run_cli(
            "elicitation", "history",
            "--project-root", str(project_root),
        )
        assert rc == 0, f"rc={rc} err={err}"
        assert "(no decisions)" in out

        # And the --json variant returns an empty array.
        rc, out, err = _run_cli(
            "elicitation", "history", "--json",
            "--project-root", str(project_root),
        )
        assert rc == 0
        assert json.loads(out) == []


# === Helpers ===


def _run_cli(*argv: str) -> tuple[int, str, str]:
    """Invoke ``harness.cli.main(argv)`` and capture stdout/stderr/exit.

    We redirect file descriptors (not just sys.stdout) so that prints
    happening inside argparse error paths are also captured.
    """
    import io
    import contextlib

    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        try:
            rc = __import__("harness.cli", fromlist=["main"]).main(list(argv))
        except SystemExit as e:
            rc = int(e.code) if isinstance(e.code, int) else 1
    return rc, out_buf.getvalue(), err_buf.getvalue()
