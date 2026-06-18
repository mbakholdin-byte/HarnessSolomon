"""Phase 4.10 Task C: Smoke tests for all 8 hook patterns.

Full-flow integration tests exercising each pattern end-to-end. The
6 patterns owned by Task A (auto_format, license_check,
complexity_check) and Task B (secret_detect, sql_injection_guard,
unsafe_import_block) are imported from their real implementations —
by the time this test module ships, Task A/B are expected to have
landed their files. The 2 patterns owned by this task
(test_required, docs_required) are imported from the local modules.

``auto_format`` is a subprocess hook (standalone script), so the
smoke test spawns it as a child process with a JSON payload on
stdin and verifies the exit code.

The final ``test_smoke_all_8_patterns_via_dispatcher`` registers
all 8 hooks in a fresh ``HookRegistry`` and verifies that a single
``HookRunner.fire`` dispatches to every registered hook.

Trust boundary: tests may import anything (they live outside
``harness/hooks/``).
"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from harness.hooks.context import HookContext, HookDecision
from harness.hooks.events import EventType
from harness.hooks.registry import HookRegistry, HookSpec
from harness.hooks.runner import HookRunner

# === Task A: advisory hooks (builtin transport) ============================
from harness.hooks.builtin.complexity_check import complexity_check_hook
from harness.hooks.builtin.license_check import license_check_hook

# === Task B: security hooks (builtin transport, user.builtin.* ids) ========
from harness.hooks.builtin.secret_detect import secret_detect_hook
from harness.hooks.builtin.sql_injection_guard import sql_injection_guard_hook
from harness.hooks.builtin.unsafe_import_block import unsafe_import_block_hook

# === Task C (this task): workflow hooks (builtin transport) ================
# Aliases to prevent pytest from collecting the imported ``test_*``
# callable as a test function (pytest collects top-level ``test_*``).
from harness.hooks.builtin.docs_required import (
    docs_required_hook as _docs_required_hook,
)
from harness.hooks.builtin.test_required import (
    test_required_hook as _test_required_hook,
)

_docs_required_hook.__test__ = False  # type: ignore[attr-defined]
_test_required_hook.__test__ = False  # type: ignore[attr-defined]

docs_required_hook = _docs_required_hook
test_required_hook = _test_required_hook

# auto_format lives under harness/hooks/patterns/ and is a CLI script.
_AUTO_FORMAT_SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "harness"
    / "hooks"
    / "patterns"
    / "auto_format.py"
)


# === Map of all 8 patterns for the dispatcher test =========================
# Each entry: hook_id used for registration → (callable, event).
# Note: license_check and complexity_check emit ``builtin.<name>`` as
# their internal hook_id (Task A wired them into BUILTIN_HOOKS), but
# the registry uses the hook_id WE supply at registration time, so we
# normalise on ``user.builtin.<name>`` for the dispatcher test.
ALL_8_PATTERNS: dict[str, tuple[Any, EventType]] = {
    "user.builtin.auto_format":         (None, EventType.POST_TOOL_USE),  # subprocess — no callable
    "user.builtin.license_check":       (license_check_hook,         EventType.PRE_TOOL_USE),
    "user.builtin.complexity_check":    (complexity_check_hook,      EventType.POST_TOOL_USE),
    "user.builtin.secret_detect":       (secret_detect_hook,         EventType.PRE_TOOL_USE),
    "user.builtin.sql_injection_guard": (sql_injection_guard_hook,   EventType.PRE_TOOL_USE),
    "user.builtin.unsafe_import_block": (unsafe_import_block_hook,   EventType.PRE_TOOL_USE),
    "user.builtin.test_required":       (test_required_hook,         EventType.PRE_TOOL_USE),
    "user.builtin.docs_required":       (docs_required_hook,         EventType.POST_TOOL_USE),
}


# === 1. auto_format (subprocess transport) =================================


class TestSmokeAutoFormat:
    def test_smoke_auto_format_full_flow(self, tmp_path: Path) -> None:
        """Spawn auto_format.py with a PostToolUse .py payload; expect exit 0."""
        py = tmp_path / "demo.py"
        py.write_text("x=1\n", encoding="utf-8")
        ctx_payload = {
            "event": "PostToolUse",
            "session_id": "s1",
            "agent_id": "",
            "payload": {
                "tool_name": "write_file",
                "ok": True,
                "arguments": {"path": str(py)},
            },
        }
        proc = subprocess.run(
            [sys.executable, str(_AUTO_FORMAT_SCRIPT)],
            input=json.dumps(ctx_payload),
            capture_output=True,
            text=True,
            timeout=10,
        )
        # The hook NEVER blocks — even if ruff is missing (CI), exit is 0.
        assert proc.returncode == 0, (
            f"auto_format exit={proc.returncode} stderr={proc.stderr!r}"
        )


# === 2. license_check ======================================================


class TestSmokeLicenseCheck:
    async def test_smoke_license_check_blocks_gpl_full_flow(self) -> None:
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={
                "tool_name": "write_file",
                "arguments": {"path": "x.py", "content": "import gpl3_library\n"},
            },
        )
        d = await license_check_hook(ctx)
        assert d.decision == "block"
        assert "forbidden-license" in d.output["reason"]


# === 3. complexity_check ===================================================


class TestSmokeComplexityCheck:
    async def test_smoke_complexity_check_warns_high_complexity(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Construct a function with many branches to trip complexity > 10.
        # complexity_check parses ``arguments.content`` directly (no file I/O),
        # so we pass the source inline.
        branches = "\n".join(
            f"    if x == {i}:\n        return {i}" for i in range(15)
        )
        source = f"def f(x):\n{branches}\n    return -1\n"

        logger_name = "harness.hooks.builtin.complexity_check"
        with caplog.at_level(logging.WARNING, logger=logger_name):
            ctx = HookContext(
                event="PostToolUse",
                session_id="s1",
                agent_id="",
                payload={
                    "tool_name": "write_file",
                    "arguments": {"path": "high.py", "content": source},
                },
            )
            d = await complexity_check_hook(ctx)
        # Advisory: never blocks.
        assert d.decision == "allow"
        # The high-complexity function ``f`` must trigger a warning.
        assert any(
            "complexity" in r.message.lower() and " f " in (" " + r.message + " ")
            or "f has complexity" in r.message
            for r in caplog.records
        ), [r.message for r in caplog.records]


# === 4. secret_detect ======================================================


class TestSmokeSecretDetect:
    async def test_smoke_secret_detect_blocks_aws_full_flow(self) -> None:
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={
                "tool_name": "write_file",
                "arguments": {
                    "path": "x.py",
                    "content": "key = 'AKIAIOSFODNN7EXAMPLE'\n",
                },
            },
        )
        d = await secret_detect_hook(ctx)
        assert d.decision == "block"
        # Task B's reason text mentions the secret type.
        assert "AWS" in d.output.get("reason", "") or "key" in d.output.get(
            "reason", ""
        ).lower()


# === 5. sql_injection_guard ================================================


class TestSmokeSqlInjection:
    async def test_smoke_sql_injection_blocks_full_flow(self) -> None:
        # sql_injection_guard looks for Python f-string patterns in the
        # arguments (e.g. ``f"SELECT ... FROM ... {var}"``). We embed
        # such a snippet in the content of a write_file call.
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={
                "tool_name": "write_file",
                "arguments": {
                    "path": "x.py",
                    "content": (
                        'query = f"SELECT * FROM users WHERE id={user_id}"\n'
                    ),
                },
            },
        )
        d = await sql_injection_guard_hook(ctx)
        assert d.decision == "block"
        assert "sql" in d.output.get("reason", "").lower() or "injection" in d.output.get(
            "reason", ""
        ).lower()


# === 6. unsafe_import_block ================================================


class TestSmokeUnsafeImport:
    async def test_smoke_unsafe_import_blocks_pickle_full_flow(self) -> None:
        # unsafe_import_block matches ``pickle.load(`` / ``pickle.loads(``,
        # not a bare ``import pickle``. We use the dangerous call form.
        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={
                "tool_name": "write_file",
                "arguments": {
                    "path": "x.py",
                    "content": "import pickle\npickle.load(open('data'))\n",
                },
            },
        )
        d = await unsafe_import_block_hook(ctx)
        assert d.decision == "block"
        assert "pickle" in d.output.get("reason", "").lower()


# === 7. test_required (Task C, real impl) ==================================


class TestSmokeTestRequired:
    async def test_smoke_test_required_blocks_commit_no_tests(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(cmd, *args, **kwargs):  # noqa: ANN001
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout="src/app.py\nsrc/util.py\n", stderr=""
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.chdir(tmp_path)

        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={
                "tool_name": "bash",
                "arguments": {"command": "git commit -m 'wip'"},
            },
        )
        d = await test_required_hook(ctx)
        assert d.decision == "block"
        assert "tests required" in d.output["reason"]
        assert len(d.output["staged_py_files"]) == 2

    async def test_smoke_test_required_allows_commit_with_tests(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_run(cmd, *args, **kwargs):  # noqa: ANN001
            return subprocess.CompletedProcess(
                cmd, returncode=0, stdout="src/app.py\n", stderr=""
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        monkeypatch.chdir(tmp_path)

        ctx = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={
                "tool_name": "bash",
                "arguments": {"command": "pytest && git commit -m 'ok'"},
            },
        )
        d = await test_required_hook(ctx)
        assert d.decision == "allow"


# === 8. docs_required (Task C, real impl) ==================================


class TestSmokeDocsRequired:
    async def test_smoke_docs_required_warns_missing_docstring(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        py = tmp_path / "mod.py"
        py.write_text(
            "def public_fn():\n    pass\n\ndef _private_fn():\n    pass\n",
            encoding="utf-8",
        )
        ctx = HookContext(
            event="PostToolUse",
            session_id="s1",
            agent_id="",
            payload={
                "tool_name": "write_file",
                "arguments": {"path": str(py)},
            },
        )
        with caplog.at_level(
            logging.WARNING, logger="harness.hooks.builtin.docs_required"
        ):
            d = await docs_required_hook(ctx)
        assert d.decision == "allow"
        assert "public_fn" in d.output["missing_docstrings"]
        assert any("public_fn" in r.message for r in caplog.records)

    async def test_smoke_docs_required_skips_private(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        py = tmp_path / "mod.py"
        py.write_text("def _helper():\n    pass\n", encoding="utf-8")
        ctx = HookContext(
            event="PostToolUse",
            session_id="s1",
            agent_id="",
            payload={
                "tool_name": "write_file",
                "arguments": {"path": str(py)},
            },
        )
        with caplog.at_level(
            logging.WARNING, logger="harness.hooks.builtin.docs_required"
        ):
            d = await docs_required_hook(ctx)
        assert d.decision == "allow"
        assert d.output["missing_docstrings"] == []
        assert not any("_helper" in r.message for r in caplog.records)


# === Final: all 8 patterns via dispatcher ==================================


class TestSmokeAll8ViaDispatcher:
    async def test_smoke_all_8_patterns_via_dispatcher(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Register all 8 patterns and verify dispatch fires each one."""
        # Mock git diff so test_required sees staged .py files.
        def fake_run(cmd, *args, **kwargs):  # noqa: ANN001
            # Only mock the git-diff probe; leave other subprocess
            # callers untouched.
            if isinstance(cmd, list) and cmd[:2] == ["git", "diff"]:
                return subprocess.CompletedProcess(
                    cmd, returncode=0, stdout="src/x.py\n", stderr=""
                )
            return subprocess.run(cmd, *args, **kwargs)

        monkeypatch.setattr(subprocess, "run", fake_run)

        # Register all 8 hooks. auto_format is subprocess-only so we
        # skip it in the in-process dispatcher (it has no callable);
        # we still register the other 7 and assert their hook_ids
        # appear in the dispatch result.
        registry = HookRegistry()
        registered = 0
        for hook_id, (callable_, event) in ALL_8_PATTERNS.items():
            if callable_ is None:
                # auto_format — subprocess transport, exercised in its
                # own smoke test above.
                continue
            spec = HookSpec(
                hook_id=hook_id,
                event=event,
                transport="builtin",
                callable=callable_,
            )
            await registry.register(spec)
            registered += 1
        assert registered == 7  # 8 patterns − 1 subprocess-only auto_format

        runner = HookRunner(registry, default_timeout_ms=2000)

        # Fire a PreToolUse that should trigger all 5 Pre hooks.
        # We craft the payload so every Pre blocker's signature is present:
        #   - license_check: import gpl3
        #   - secret_detect: AWS key
        #   - sql_injection_guard: f"SELECT ... {var}"  (Python f-string)
        #   - unsafe_import_block: pickle.load(
        #   - test_required: git commit + staged .py (mocked)
        ctx_pre = HookContext(
            event="PreToolUse",
            session_id="s1",
            agent_id="",
            payload={
                "tool_name": "write_file",
                "arguments": {
                    "path": "evil.py",
                    "content": (
                        "import pickle\n"
                        "pickle.load(open('d'))\n"
                        "import gpl3_lic\n"
                        "key = 'AKIAIOSFODNN7EXAMPLE'\n"
                        'q = f"SELECT * FROM t WHERE id={user_id}"\n'
                    ),
                    # ``command`` carries the git-commit tail that
                    # test_required keys on.
                    "command": "git commit -m evil",
                },
            },
        )
        agg_pre = await runner.fire(ctx_pre)

        # Every Pre hook must have produced a decision (block or allow).
        pre_decisions = {d.hook_id: d for d in agg_pre.decisions}
        assert "user.builtin.license_check" in pre_decisions
        assert "user.builtin.secret_detect" in pre_decisions
        assert "user.builtin.sql_injection_guard" in pre_decisions
        assert "user.builtin.unsafe_import_block" in pre_decisions
        assert "user.builtin.test_required" in pre_decisions

        # Each of the 5 Pre hooks should have BLOCKED on this payload.
        for hid in (
            "user.builtin.license_check",
            "user.builtin.secret_detect",
            "user.builtin.sql_injection_guard",
            "user.builtin.unsafe_import_block",
            "user.builtin.test_required",
        ):
            assert pre_decisions[hid].decision == "block", (
                f"{hid} expected block, got {pre_decisions[hid].decision} "
                f"(output={pre_decisions[hid].output!r})"
            )
        assert agg_pre.final_decision == "block"

        # Fire a PostToolUse that triggers the 2 Post hooks:
        # complexity_check + docs_required (auto_format is subprocess).
        # complexity_check parses ``arguments.content`` directly;
        # docs_required reads the file from ``arguments.path``. We
        # supply BOTH so each hook sees what it needs.
        py = tmp_path / "post_target.py"
        py_content = "def public_no_doc():\n    pass\n"
        py.write_text(py_content, encoding="utf-8")
        ctx_post = HookContext(
            event="PostToolUse",
            session_id="s1",
            agent_id="",
            payload={
                "tool_name": "write_file",
                "ok": True,
                "arguments": {"path": str(py), "content": py_content},
            },
        )
        agg_post = await runner.fire(ctx_post)
        post_hook_ids = {d.hook_id for d in agg_post.decisions}
        assert "user.builtin.complexity_check" in post_hook_ids
        assert "user.builtin.docs_required" in post_hook_ids
        # Post hooks are advisory → final = allow.
        assert agg_post.final_decision == "allow"

        # docs_required should have flagged public_no_doc.
        docs_dec = next(
            d for d in agg_post.decisions
            if d.hook_id == "user.builtin.docs_required"
        )
        assert "public_no_doc" in docs_dec.output.get("missing_docstrings", [])

        # Verify the dispatcher saw all 7 registered hooks across the
        # two events (5 Pre + 2 Post).
        all_seen = {d.hook_id for d in agg_pre.decisions} | {
            d.hook_id for d in agg_post.decisions
        }
        assert all_seen == {
            "user.builtin.license_check",
            "user.builtin.secret_detect",
            "user.builtin.sql_injection_guard",
            "user.builtin.unsafe_import_block",
            "user.builtin.test_required",
            "user.builtin.complexity_check",
            "user.builtin.docs_required",
        }
