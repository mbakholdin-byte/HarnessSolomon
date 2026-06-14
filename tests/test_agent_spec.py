"""Tests for harness.agents.spec (Phase 2.0, Step 1).

Covers:
  - Pydantic AgentSpec field validation (name, model, tools, permissions)
  - read-only denylist consistency (write_file/edit_file rejected)
  - Hand-rolled YAML frontmatter parser (inline lists, scalars, comments)
  - Error paths: missing frontmatter, unknown fields, nested keys, bad model
  - Defaults (empty model → settings.subagent_default_model, body → system_prompt)
  - File-system edge cases: CRLF, UTF-8 with replacement, body-only files
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from harness.agents.spec import (
    AgentSpec,
    FrontmatterParseError,
    _parse_frontmatter_block,
    _parse_value,
    _split_list,
    parse_agent_md,
)


# === _parse_value / _split_list unit tests (pure functions) ===

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("hello", "hello"),
        ('"quoted"', "quoted"),
        ("'single'", "single"),
        ("42", 42),
        ("-1", -1),
        ("3.14", 3.14),
        ("true", True),
        ("False", False),
        ("yes", True),
        ("no", False),
        ("", ""),
        ("[]", []),
        ("[a, b, c]", ["a", "b", "c"]),
        ('[a, "b c", d]', ["a", "b c", "d"]),
        ("[1, 2, 3]", [1, 2, 3]),
    ],
)
def test_parse_value_shapes(raw: str, expected: object) -> None:
    assert _parse_value(raw) == expected


def test_split_list_respects_quotes() -> None:
    """Commas inside quoted strings do not split the list."""
    parts = _split_list('a, "b, c", d')
    assert parts == ["a", '"b, c"', "d"]


# === AgentSpec field validators ===

def test_agent_spec_minimal_valid() -> None:
    """All-default AgentSpec is constructable."""
    spec = AgentSpec(name="explore")
    assert spec.name == "explore"
    assert spec.model == ""
    assert spec.tools == ["read_file", "grep", "glob"]
    assert spec.permissions == "read-only"
    assert spec.max_iterations == 5
    assert spec.worktree_required is True
    assert spec.allowed_paths == []
    assert spec.system_prompt == ""


def test_agent_spec_full_valid() -> None:
    spec = AgentSpec(
        name="code",
        model="MiniMax-M2.7",
        tools=["read_file", "write_file", "bash"],
        permissions="full",
        system_prompt="Make the smallest change.",
        max_iterations=10,
        worktree_required=False,
        allowed_paths=["src/**"],
    )
    assert spec.name == "code"
    assert spec.permissions == "full"
    assert spec.worktree_required is False


def test_agent_spec_name_must_be_kebab_case() -> None:
    with pytest.raises(ValidationError, match="kebab-case"):
        AgentSpec(name="Explore")  # uppercase
    with pytest.raises(ValidationError, match="kebab-case"):
        AgentSpec(name="explore agent")  # space
    with pytest.raises(ValidationError, match="kebab-case"):
        AgentSpec(name="1-explore")  # starts with digit
    with pytest.raises(ValidationError, match="kebab-case"):
        AgentSpec(name="explore_subagent")  # underscore not allowed


def test_agent_spec_model_must_be_in_catalog() -> None:
    with pytest.raises(ValidationError, match="unknown model"):
        AgentSpec(name="x", model="gpt-9000")


def test_agent_spec_empty_model_allowed() -> None:
    """Empty model is a valid sentinel — parse_agent_md substitutes the default."""
    spec = AgentSpec(name="x", model="")
    assert spec.model == ""


def test_agent_spec_read_only_rejects_write_tools() -> None:
    """permissions=read-only + write_file/edit_file in tools is a contradiction."""
    with pytest.raises(ValidationError, match="read-only conflicts"):
        AgentSpec(
            name="x",
            permissions="read-only",
            tools=["read_file", "write_file"],
        )


def test_agent_spec_scoped_write_allows_write_tools() -> None:
    spec = AgentSpec(
        name="x",
        permissions="scoped-write",
        tools=["read_file", "write_file"],
    )
    assert spec.permissions == "scoped-write"


def test_agent_spec_tools_dedupe_preserves_order() -> None:
    spec = AgentSpec(name="x", tools=["read_file", "grep", "read_file", "glob"])
    assert spec.tools == ["read_file", "grep", "glob"]


def test_agent_spec_tools_empty_allowed() -> None:
    """Review-style agent can be tool-less (talks only via LLM)."""
    spec = AgentSpec(name="x", tools=[])
    assert spec.tools == []


def test_agent_spec_max_iterations_bounds() -> None:
    with pytest.raises(ValidationError):
        AgentSpec(name="x", max_iterations=0)
    with pytest.raises(ValidationError):
        AgentSpec(name="x", max_iterations=21)


def test_agent_spec_frozen() -> None:
    """Specs are immutable — editing raises."""
    spec = AgentSpec(name="x")
    with pytest.raises(ValidationError):
        spec.name = "y"  # type: ignore[misc]


def test_agent_spec_extra_forbid() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        AgentSpec(name="x", unknown_field=42)  # type: ignore[call-arg]


# === _parse_frontmatter_block ===

def test_parse_block_basic() -> None:
    block = "name: explore\nmax_iterations: 8\n"
    assert _parse_frontmatter_block(block) == {
        "name": "explore",
        "max_iterations": 8,
    }


def test_parse_block_skips_blank_and_comments() -> None:
    block = "# top comment\nname: x\n\n# side comment\nmax_iterations: 5\n"
    assert _parse_frontmatter_block(block) == {"name": "x", "max_iterations": 5}


def test_parse_block_rejects_malformed_line() -> None:
    with pytest.raises(FrontmatterParseError, match="malformed frontmatter line"):
        _parse_frontmatter_block("name: ok\nthis is not yaml\n")


def test_parse_block_rejects_nested_keys() -> None:
    with pytest.raises(FrontmatterParseError, match="nested keys are not supported"):
        _parse_frontmatter_block("name: ok\nparent.child: x\n")


def test_parse_block_rejects_duplicate_key() -> None:
    with pytest.raises(FrontmatterParseError, match="duplicate key"):
        _parse_frontmatter_block("name: a\nname: b\n")


# === parse_agent_md integration ===

def _write_agent(tmp_path, name: str, body: str, *, front: str | None = None) -> str:
    """Helper: write an agent .md file. If front is None, body is treated as the full file."""
    if front is None:
        content = body
    else:
        content = f"---\n{front}\n---\n{body}"
    p = tmp_path / f"{name}.md"
    p.write_text(content, encoding="utf-8")
    return str(p)


def test_parse_minimal_frontmatter(tmp_path) -> None:
    p = _write_agent(tmp_path, "explore", "You are the explore agent.", front="name: explore\n")
    spec = parse_agent_md(p)
    assert spec.name == "explore"
    assert spec.system_prompt == "You are the explore agent."
    assert spec.model  # substituted from settings.subagent_default_model


def test_parse_full_frontmatter(tmp_path) -> None:
    front = (
        "name: code\n"
        "model: MiniMax-M2.7\n"
        "tools: [read_file, write_file, bash]\n"
        "permissions: full\n"
        "max_iterations: 10\n"
        "worktree_required: false\n"
        "allowed_paths: [src/**, tests/**]"
    )
    p = _write_agent(tmp_path, "code", "Make the smallest change.", front=front)
    spec = parse_agent_md(p)
    assert spec.name == "code"
    assert spec.model == "MiniMax-M2.7"
    assert spec.permissions == "full"
    assert spec.tools == ["read_file", "write_file", "bash"]
    assert spec.max_iterations == 10
    assert spec.worktree_required is False
    assert spec.allowed_paths == ["src/**", "tests/**"]


def test_parse_missing_frontmatter_raises(tmp_path) -> None:
    p = _write_agent(tmp_path, "x", "No frontmatter here at all.")
    with pytest.raises(FrontmatterParseError, match="missing the required"):
        parse_agent_md(p)


def test_parse_unknown_field_raises(tmp_path) -> None:
    front = "name: x\nmax_iterrations: 5\n"  # typo
    p = _write_agent(tmp_path, "x", "body", front=front)
    with pytest.raises(FrontmatterParseError, match="unknown frontmatter fields"):
        parse_agent_md(p)


def test_parse_unknown_model_raises(tmp_path) -> None:
    front = "name: x\nmodel: gpt-9999\n"
    p = _write_agent(tmp_path, "x", "body", front=front)
    with pytest.raises(FrontmatterParseError, match="unknown model"):
        parse_agent_md(p)


def test_parse_read_only_with_write_tools_raises(tmp_path) -> None:
    front = "name: x\ntools: [read_file, write_file]\npermissions: read-only\n"
    p = _write_agent(tmp_path, "x", "body", front=front)
    with pytest.raises(FrontmatterParseError, match="read-only conflicts"):
        parse_agent_md(p)


def test_parse_max_iterations_zero_raises(tmp_path) -> None:
    front = "name: x\nmax_iterations: 0\n"
    p = _write_agent(tmp_path, "x", "body", front=front)
    with pytest.raises(FrontmatterParseError, match="validation"):
        parse_agent_md(p)


def test_parse_body_becomes_system_prompt_verbatim(tmp_path) -> None:
    body = "Line 1.\n\nLine 3 has  multiple   spaces.\n"
    p = _write_agent(tmp_path, "x", body, front="name: x\n")
    spec = parse_agent_md(p)
    assert spec.system_prompt == body.strip()


def test_parse_crlf_line_endings_tolerated(tmp_path) -> None:
    """Windows CRLF must not break the parser."""
    p = tmp_path / "x.md"
    p.write_bytes(b"---\r\nname: x\r\nmax_iterations: 5\r\n---\r\nBody.\r\n")
    spec = parse_agent_md(p)
    assert spec.name == "x"
    assert spec.system_prompt == "Body."


def test_parse_nested_keys_raises(tmp_path) -> None:
    front = "name: x\nparent.child: nope\n"
    p = _write_agent(tmp_path, "x", "body", front=front)
    with pytest.raises(FrontmatterParseError, match="nested keys"):
        parse_agent_md(p)


def test_parse_utf8_with_replacement(tmp_path) -> None:
    """Bytes that aren't valid UTF-8 are replaced, not crashed."""
    p = tmp_path / "x.md"
    p.write_bytes(b"---\nname: x\n---\nBody with \xff bad byte.\n")
    spec = parse_agent_md(p)
    assert spec.name == "x"
    assert "bad byte" in spec.system_prompt


def test_parse_round_trip(tmp_path) -> None:
    """Spec → model_dump → re-parse keeps all fields."""
    original = AgentSpec(
        name="explore",
        model="MiniMax-M2.7",
        tools=["read_file", "grep", "glob"],
        permissions="read-only",
        system_prompt="You are the explore agent.",
        max_iterations=8,
        worktree_required=True,
        allowed_paths=[],
    )
    # Write to disk and re-parse. Note: model_dump is JSON-safe; we can't
    # roundtrip via model_dump directly, so we write canonical frontmatter.
    front = (
        f"name: {original.name}\n"
        f"model: {original.model}\n"
        f"tools: [{', '.join(original.tools)}]\n"
        f"permissions: {original.permissions}\n"
        f"max_iterations: {original.max_iterations}\n"
        f"worktree_required: {str(original.worktree_required).lower()}\n"
        f"allowed_paths: [{', '.join(original.allowed_paths)}]\n"
    )
    p = _write_agent(tmp_path, original.name, original.system_prompt, front=front)
    reparsed = parse_agent_md(p)
    assert reparsed.name == original.name
    assert reparsed.model == original.model
    assert reparsed.tools == original.tools
    assert reparsed.permissions == original.permissions
    assert reparsed.max_iterations == original.max_iterations
    assert reparsed.worktree_required == original.worktree_required
    assert reparsed.allowed_paths == original.allowed_paths
    assert reparsed.system_prompt == original.system_prompt


def test_parse_file_not_found(tmp_path) -> None:
    with pytest.raises(FileNotFoundError):
        parse_agent_md(tmp_path / "does-not-exist.md")
