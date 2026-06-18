"""Phase 4.10 Task B: Tests for 3 security builtin hooks.

Covers:
    * ``secret_detect_hook`` — AWS / GitHub / OpenAI / PEM / JWT / password.
    * ``sql_injection_guard_hook`` — f-string / concat / %-format / .format / DELETE.
    * ``unsafe_import_block_hook`` — os.system / pickle / safe subprocess / yaml.load.

Each hook is async and takes a ``HookContext``; pytest-asyncio's
``auto`` mode (``pyproject.toml`` → ``asyncio_mode = "auto"``) wires
``async def test_*`` functions without an explicit decorator.

The trust boundary (no ``harness.agents`` / ``harness.server`` imports
inside ``harness/hooks/builtin/*.py``) is enforced separately by
``tests/test_hooks_trust_boundary.py``; this file focuses on behaviour.
"""
from __future__ import annotations

from harness.hooks import HookContext, HookDecision
from harness.hooks.builtin import (
    secret_detect_hook,
    sql_injection_guard_hook,
    unsafe_import_block_hook,
)


# ---------- helpers ---------------------------------------------------------


def _pre_tool_ctx(arguments: object, tool_name: str = "write_file") -> HookContext:
    """Build a minimal PreToolUse HookContext for testing."""
    return HookContext(
        event="PreToolUse",
        session_id="s-test",
        agent_id="a-test",
        payload={"tool_name": tool_name, "arguments": arguments},
    )


def _non_pre_ctx(arguments: object) -> HookContext:
    """Build a non-PreToolUse context (e.g. PostToolUse) to exercise skip paths."""
    return HookContext(
        event="PostToolUse",
        session_id="s-test",
        agent_id="a-test",
        payload={"tool_name": "bash", "arguments": arguments},
    )


# ---------- secret_detect ---------------------------------------------------


class TestSecretDetect:
    """secret_detect_hook — 6 block patterns + 1 allow + skip behaviour."""

    async def test_secret_detect_blocks_aws_key(self) -> None:
        # AKIA + 16 uppercase alphanumerics — canonical AWS IAM access key id.
        ctx = _pre_tool_ctx({"content": "AWS_KEY=AKIAIOSFODNN7EXAMPLE"})
        d = await secret_detect_hook(ctx)
        assert d.decision == "block"
        assert d.hook_id == "user.builtin.secret_detect"
        assert "AWS access key" in d.output["reason"]

    async def test_secret_detect_blocks_github_token(self) -> None:
        # ghp_ + 36 base62 chars — canonical GitHub personal access token.
        token = "ghp_" + "a" * 36
        ctx = _pre_tool_ctx({"content": f"GITHUB_PAT={token}"})
        d = await secret_detect_hook(ctx)
        assert d.decision == "block"
        assert "GitHub" in d.output["reason"]

    async def test_secret_detect_blocks_openai_key(self) -> None:
        # sk- + 48 base62 chars — canonical OpenAI API key.
        key = "sk-" + "A" * 48
        ctx = _pre_tool_ctx({"content": f"OPENAI_API_KEY={key}"})
        d = await secret_detect_hook(ctx)
        assert d.decision == "block"
        assert "OpenAI" in d.output["reason"]

    async def test_secret_detect_blocks_pem_key(self) -> None:
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA..."
        ctx = _pre_tool_ctx({"content": pem})
        d = await secret_detect_hook(ctx)
        assert d.decision == "block"
        assert "PEM" in d.output["reason"]

    async def test_secret_detect_blocks_jwt(self) -> None:
        # Minimal JWT-shaped string: eyJ... . eyJ... . sig
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwInS.SflKxwRJSMeKKF2QT4f"
        ctx = _pre_tool_ctx({"content": f"token={jwt}"})
        d = await secret_detect_hook(ctx)
        assert d.decision == "block"
        assert "JWT" in d.output["reason"]

    async def test_secret_detect_blocks_password_literal(self) -> None:
        # password = "..." with 8+ chars — caught by the (?i)password regex.
        ctx = _pre_tool_ctx({"content": 'password = "supersecret123"'})
        d = await secret_detect_hook(ctx)
        assert d.decision == "block"
        assert "password" in d.output["reason"].lower()

    async def test_secret_detect_allows_normal_text(self) -> None:
        # No false positive on plain Python source.
        ctx = _pre_tool_ctx({"content": "import requests\nresponse = requests.get(url)"})
        d = await secret_detect_hook(ctx)
        assert d.decision == "allow"

    async def test_secret_detect_allows_short_password_placeholder(self) -> None:
        # The password regex requires 8+ chars; short placeholders pass.
        ctx = _pre_tool_ctx({"content": 'password = "x"'})
        d = await secret_detect_hook(ctx)
        assert d.decision == "allow"

    async def test_secret_detect_non_pre_tool_use_skips(self) -> None:
        # Even a real AWS key should be ignored on PostToolUse.
        ctx = _non_pre_ctx({"content": "AKIAIOSFODNN7EXAMPLE"})
        d = await secret_detect_hook(ctx)
        assert d.decision == "allow"

    async def test_secret_detect_allows_empty_arguments(self) -> None:
        ctx = _pre_tool_ctx({})
        d = await secret_detect_hook(ctx)
        assert d.decision == "allow"


# ---------- sql_injection_guard ---------------------------------------------


class TestSqlInjectionGuard:
    """sql_injection_guard_hook — 2 block patterns + parametrised allow + skips."""

    async def test_sql_injection_blocks_fstring(self) -> None:
        # f-string with {var} interpolation next to SELECT ... FROM.
        code = 'query = f"SELECT * FROM users WHERE id={user_id}"'
        ctx = _pre_tool_ctx({"command": code}, tool_name="bash")
        d = await sql_injection_guard_hook(ctx)
        assert d.decision == "block"
        assert d.hook_id == "user.builtin.sql_injection_guard"
        assert "SQL injection" in d.output["reason"]
        assert "f-string" in d.output["reason"]

    async def test_sql_injection_blocks_concat(self) -> None:
        # String concatenation: "SELECT ..." + var
        code = 'query = "SELECT * FROM " + table_name'
        ctx = _pre_tool_ctx({"command": code}, tool_name="bash")
        d = await sql_injection_guard_hook(ctx)
        assert d.decision == "block"
        assert "concatenation" in d.output["reason"]

    async def test_sql_injection_blocks_percent_format(self) -> None:
        # %-formatting with SELECT — matches the handoff literal
        # ``%[^%]*SELECT[^%]*%[^,]+,`` (SELECT sits between two ``%``
        # markers, followed by a value and a comma).
        code = 'q = "name=%s SELECT * FROM t" % name,'
        ctx = _pre_tool_ctx({"command": code}, tool_name="bash")
        d = await sql_injection_guard_hook(ctx)
        assert d.decision == "block"
        assert "%-formatting" in d.output["reason"]

    async def test_sql_injection_blocks_format_call(self) -> None:
        # .format() with SELECT inside the format() argument list —
        # matches the handoff literal ``\.format\([^)]*SELECT``.
        code = 'q = "{}".format(SELECT * FROM t)'
        ctx = _pre_tool_ctx({"command": code}, tool_name="bash")
        d = await sql_injection_guard_hook(ctx)
        assert d.decision == "block"
        assert ".format()" in d.output["reason"]

    async def test_sql_injection_blocks_delete_concat(self) -> None:
        code = 'sql = "DELETE FROM users WHERE id=" + str(uid)'
        ctx = _pre_tool_ctx({"command": code}, tool_name="bash")
        d = await sql_injection_guard_hook(ctx)
        assert d.decision == "block"
        assert "DELETE" in d.output["reason"]

    async def test_sql_injection_allows_parametrized(self) -> None:
        # The canonical safe pattern: parameterised query.
        code = 'cursor.execute("SELECT * FROM users WHERE id=?", (user_id,))'
        ctx = _pre_tool_ctx({"command": code}, tool_name="bash")
        d = await sql_injection_guard_hook(ctx)
        assert d.decision == "allow"

    async def test_sql_injection_allows_no_sql(self) -> None:
        # Plain code with no SQL keywords — must not block.
        ctx = _pre_tool_ctx({"command": "ls -la /tmp"}, tool_name="bash")
        d = await sql_injection_guard_hook(ctx)
        assert d.decision == "allow"

    async def test_sql_injection_non_pre_tool_use_skips(self) -> None:
        ctx = _non_pre_ctx({"command": 'f"SELECT * FROM t WHERE x={v}"'})
        d = await sql_injection_guard_hook(ctx)
        assert d.decision == "allow"

    async def test_sql_injection_empty_arguments_allowed(self) -> None:
        ctx = _pre_tool_ctx({}, tool_name="bash")
        d = await sql_injection_guard_hook(ctx)
        assert d.decision == "allow"


# ---------- unsafe_import_block ---------------------------------------------


class TestUnsafeImportBlock:
    """unsafe_import_block_hook — 2 block patterns + safe subprocess allow + config."""

    async def test_unsafe_import_blocks_os_system(self) -> None:
        code = 'import os\nos.system("rm -rf /tmp/cache")'
        ctx = _pre_tool_ctx({"content": code})
        d = await unsafe_import_block_hook(ctx)
        assert d.decision == "block"
        assert d.hook_id == "user.builtin.unsafe_import_block"
        assert "os.system" in d.output["reason"]

    async def test_unsafe_import_blocks_pickle(self) -> None:
        code = "import pickle\nobj = pickle.loads(blob)"
        ctx = _pre_tool_ctx({"content": code})
        d = await unsafe_import_block_hook(ctx)
        assert d.decision == "block"
        assert "pickle" in d.output["reason"].lower()

    async def test_unsafe_import_blocks_eval(self) -> None:
        code = "result = eval(user_input)"
        ctx = _pre_tool_ctx({"content": code})
        d = await unsafe_import_block_hook(ctx)
        assert d.decision == "block"
        assert "eval" in d.output["reason"]

    async def test_unsafe_import_blocks_subprocess_shell_true(self) -> None:
        code = 'import subprocess\nsubprocess.run("ls", shell=True)'
        ctx = _pre_tool_ctx({"content": code})
        d = await unsafe_import_block_hook(ctx)
        assert d.decision == "block"
        assert "shell=True" in d.output["reason"]

    async def test_unsafe_import_blocks_yaml_load_without_safe_loader(self) -> None:
        code = "import yaml\ndata = yaml.load(stream)"
        ctx = _pre_tool_ctx({"content": code})
        d = await unsafe_import_block_hook(ctx)
        assert d.decision == "block"
        assert "yaml" in d.output["reason"].lower()
        assert "SafeLoader" in d.output["reason"]

    async def test_unsafe_import_blocks_requests_post_without_timeout(self) -> None:
        code = 'import requests\nrequests.post(url, data=payload)'
        ctx = _pre_tool_ctx({"content": code})
        d = await unsafe_import_block_hook(ctx)
        assert d.decision == "block"
        assert "timeout" in d.output["reason"].lower()

    async def test_unsafe_import_allows_safe_subprocess(self) -> None:
        # The recommended pattern: list args, shell defaults to False.
        code = 'import subprocess\nsubprocess.run(["ls", "-la"])'
        ctx = _pre_tool_ctx({"content": code})
        d = await unsafe_import_block_hook(ctx)
        assert d.decision == "allow"

    async def test_unsafe_import_allows_yaml_safe_load(self) -> None:
        code = "import yaml\ndata = yaml.safe_load(stream)"
        ctx = _pre_tool_ctx({"content": code})
        d = await unsafe_import_block_hook(ctx)
        assert d.decision == "allow"

    async def test_unsafe_import_allows_yaml_load_with_safe_loader(self) -> None:
        code = "import yaml\ndata = yaml.load(stream, Loader=yaml.SafeLoader)"
        ctx = _pre_tool_ctx({"content": code})
        d = await unsafe_import_block_hook(ctx)
        assert d.decision == "allow"

    async def test_unsafe_import_allows_requests_post_with_timeout(self) -> None:
        code = 'requests.post(url, json=payload, timeout=30)'
        ctx = _pre_tool_ctx({"content": code})
        d = await unsafe_import_block_hook(ctx)
        assert d.decision == "allow"

    async def test_unsafe_import_allows_clean_python(self) -> None:
        # Plain imports with no dangerous patterns.
        code = (
            "import json\n"
            "import pathlib\n"
            "from typing import Any\n"
            "data = json.loads(text)\n"
        )
        ctx = _pre_tool_ctx({"content": code})
        d = await unsafe_import_block_hook(ctx)
        assert d.decision == "allow"

    async def test_unsafe_import_non_pre_tool_use_skips(self) -> None:
        ctx = _non_pre_ctx({"content": "import pickle\npickle.loads(x)"})
        d = await unsafe_import_block_hook(ctx)
        assert d.decision == "allow"

    async def test_unsafe_import_empty_content_allowed(self) -> None:
        ctx = _pre_tool_ctx({"path": "/tmp/x"})  # no content field
        d = await unsafe_import_block_hook(ctx)
        assert d.decision == "allow"

    async def test_unsafe_import_respects_empty_blocklist(
        self, monkeypatch
    ) -> None:
        """When Settings blocklist is empty, the hook becomes a no-op.

        This validates the configurability contract documented in the
        handoff: operators can disable ALL checks by setting
        ``HARNESS_HOOKS_UNSAFE_IMPORTS_BLOCKLIST=`` (empty string).
        """
        from harness.hooks.builtin import unsafe_import_block as mod

        monkeypatch.setattr(mod, "_get_blocklist", lambda: ())
        ctx = _pre_tool_ctx({"content": "import pickle\npickle.loads(x)"})
        d = await unsafe_import_block_hook(ctx)
        assert d.decision == "allow"

    async def test_unsafe_import_respects_custom_blocklist(
        self, monkeypatch
    ) -> None:
        """Custom blocklist extension adds entries beyond the OWASP defaults."""
        from harness.hooks.builtin import unsafe_import_block as mod

        monkeypatch.setattr(
            mod,
            "_get_blocklist",
            lambda: ("os.system", "subprocess", "eval", "exec", "pickle",
                     "yaml.load", "requests.post", "shelve.open"),
        )
        # shelve.open hits the generic fallback path.
        ctx = _pre_tool_ctx({"content": "import shelve\ndb = shelve.open('x')"})
        d = await unsafe_import_block_hook(ctx)
        assert d.decision == "block"
        assert "shelve.open" in d.output["reason"]
