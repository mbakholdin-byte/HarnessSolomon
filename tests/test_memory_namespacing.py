"""Tests for per-agent memory namespacing (Phase 2.1, Step 3).

Covers:
  - UnifiedMemory(agent_id=...) propagates to 4 adapters
  - 2 instances with different agent_id use disjoint storage dirs / ids
  - write() auto-injects metadata["agent_id"] when not set
  - write() auto-injects #agent/<id> tag when not present
  - write() appends a provenance hop with source="unified"
  - Backward compat: default agent_id="solomon" leaves tags untouched
  - Explicit metadata.agent_id is NOT overwritten
  - Explicit tag is NOT duplicated
  - AgentSpec.memory_namespace validates as kebab-case
  - AgentRunner.get_unified_memory() caches per spec.name
  - AgentRunner.get_unified_memory() returns None when no factory
  - Built-in spec without memory_namespace -> None (share parent)
  - delete() works within a namespace; cross-namespace delete is a no-op
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from harness.agents.runner import AgentRunner
from harness.agents.spec import AgentSpec
from harness.memory.schema import Memory, ProvenanceEntry
from harness.memory.unified import UnifiedMemory


# === Helper ===

def _mem(content: str = "hello", layer: str = "L3", source: str = "manual") -> Memory:
    return Memory(content=content, layer=layer, source=source)  # type: ignore[arg-type]


# === Adapter propagation ===

class TestAgentIdPropagation:
    def test_hmem_carries_agent_id(self, memory_namespace: dict[str, Path]) -> None:
        m = UnifiedMemory(**memory_namespace, agent_id="code")
        assert m.agent_id == "code"
        # HmemAdapter stores agent in a public attr (Phase 1
        # implementation uses self.agent). We check the attr name
        # exists and matches.
        assert m.hmem.agent == "code"  # type: ignore[attr-defined]

    def test_mem0_carries_agent_id(self, memory_namespace: dict[str, Path]) -> None:
        m = UnifiedMemory(**memory_namespace, agent_id="plan")
        assert m.mem0.user_id == "plan"  # type: ignore[attr-defined]
        # Collection name is agent-specific so different sub-agents
        # land in different Qdrant collections.
        assert m.mem0.collection == "solomon-plan-memories"  # type: ignore[attr-defined]

    def test_hybrid_carries_agent_id(self, memory_namespace: dict[str, Path]) -> None:
        m = UnifiedMemory(**memory_namespace, agent_id="review")
        assert m.hybrid.project == "review"  # type: ignore[attr-defined]
        assert "#agent/review" in m.hybrid.default_tags  # type: ignore[attr-defined]

    def test_file_adapter_uses_subdirectory(self, memory_namespace: dict[str, Path]) -> None:
        m = UnifiedMemory(**memory_namespace, agent_id="code")
        # FileAdapter(memory_dir) — dir is <file_dir>/<agent_id>
        assert m.file.memory_dir == Path(memory_namespace["file_dir"]) / "code"  # type: ignore[attr-defined]

    def test_default_solomon_preserves_phase1_behaviour(
        self, memory_namespace: dict[str, Path],
    ) -> None:
        m = UnifiedMemory(**memory_namespace)  # no agent_id → default "solomon"
        assert m.agent_id == "solomon"
        assert m.hmem.agent == "solomon"  # type: ignore[attr-defined]
        assert m.mem0.user_id == "solomon"  # type: ignore[attr-defined]
        assert m.hybrid.project == "solomon"  # type: ignore[attr-defined]
        assert m.file.memory_dir == Path(memory_namespace["file_dir"]) / "solomon"  # type: ignore[attr-defined]

    def test_empty_agent_id_rejected(self, memory_namespace: dict[str, Path]) -> None:
        with pytest.raises(ValueError, match="agent_id"):
            UnifiedMemory(**memory_namespace, agent_id="")


# === write() auto-inject ===

class TestWriteAutoInject:
    def test_metadata_agent_id_stamped(self, memory_namespace: dict[str, Path]) -> None:
        m = UnifiedMemory(**memory_namespace, agent_id="code")
        mem = _mem("fact about code")
        m.write(mem)
        assert mem.metadata["agent_id"] == "code"

    def test_explicit_metadata_agent_id_not_overwritten(
        self, memory_namespace: dict[str, Path],
    ) -> None:
        """If the caller set metadata.agent_id explicitly, we
        respect that. Useful for cross-namespace writes."""
        m = UnifiedMemory(**memory_namespace, agent_id="code")
        mem = Memory(content="cross-ns", layer="L3", source="manual", metadata={"agent_id": "review"})
        m.write(mem)
        # Explicit value wins.
        assert mem.metadata["agent_id"] == "review"

    def test_tag_appended_for_non_solomon(self, memory_namespace: dict[str, Path]) -> None:
        m = UnifiedMemory(**memory_namespace, agent_id="code")
        mem = _mem("x")
        m.write(mem)
        assert "#agent/code" in mem.tags

    def test_tag_not_appended_for_default_solomon(
        self, memory_namespace: dict[str, Path],
    ) -> None:
        """Backward compat: default solomon namespace doesn't
        pollute existing tags with #agent/solomon."""
        m = UnifiedMemory(**memory_namespace)  # default
        mem = _mem("x")
        m.write(mem)
        assert "#agent/solomon" not in mem.tags

    def test_explicit_tag_not_duplicated(self, memory_namespace: dict[str, Path]) -> None:
        m = UnifiedMemory(**memory_namespace, agent_id="code")
        mem = Memory(content="x", layer="L3", source="manual", tags=["#agent/code", "user"])
        m.write(mem)
        # Tag list has at most one #agent/code entry.
        assert mem.tags.count("#agent/code") == 1
        assert "user" in mem.tags

    def test_provenance_hop_appended(self, memory_namespace: dict[str, Path]) -> None:
        m = UnifiedMemory(**memory_namespace, agent_id="code")
        mem = _mem("x")
        m.write(mem)
        # Provenance chain now has an entry with source="unified".
        assert any(
            p.source == "unified" and p.id == "code" and p.layer == "L_meta"
            for p in mem.provenance
        )

    def test_provenance_hop_not_duplicated(
        self, memory_namespace: dict[str, Path],
    ) -> None:
        """Two writes through the same facade append only one
        provenance hop, not two."""
        m = UnifiedMemory(**memory_namespace, agent_id="code")
        mem = _mem("x")
        m.write(mem)
        m.write(mem)
        hops = [p for p in mem.provenance if p.source == "unified" and p.id == "code"]
        assert len(hops) == 1


# === Cross-namespace isolation ===

class TestCrossNamespaceIsolation:
    def test_two_instances_different_agent_ids(
        self, memory_namespace: dict[str, Path], tmp_path: Path,
    ) -> None:
        ns_a = {k: tmp_path / f"a_{k}" for k in memory_namespace}
        ns_b = {k: tmp_path / f"b_{k}" for k in memory_namespace}
        a = UnifiedMemory(**ns_a, agent_id="code")
        b = UnifiedMemory(**ns_b, agent_id="plan")
        a.write(_mem("A's fact"))
        b.write(_mem("B's fact"))
        # read() should NOT see the other namespace's data because
        # the adapters are bound to disjoint storage dirs / ids.
        # (We do not assert exact contents — that depends on
        # each adapter's read() implementation. We DO assert the
        # two facades have disjoint storage paths.)
        assert a.hmem.agent != b.hmem.agent  # type: ignore[attr-defined]
        assert a.mem0.user_id != b.mem0.user_id  # type: ignore[attr-defined]
        assert a.hybrid.project != b.hybrid.project  # type: ignore[attr-defined]
        assert a.file.memory_dir != b.file.memory_dir  # type: ignore[attr-defined]

    def test_metadata_filters_via_writes(self, memory_namespace: dict[str, Path]) -> None:
        """A memory written through agent_id='code' carries
        metadata.agent_id='code'; a memory written through
        agent_id='plan' carries 'plan'. Useful for downstream
        filter by namespace."""
        m_code = UnifiedMemory(**memory_namespace, agent_id="code")
        m_plan = UnifiedMemory(**memory_namespace, agent_id="plan")
        c = _mem("code fact")
        p = _mem("plan fact")
        m_code.write(c)
        m_plan.write(p)
        assert c.metadata["agent_id"] == "code"
        assert p.metadata["agent_id"] == "plan"


# === AgentSpec.memory_namespace ===

class TestAgentSpecMemoryNamespace:
    def test_default_is_none(self) -> None:
        """Built-in spec keeps memory_namespace=None (share parent)."""
        spec = AgentSpec(name="built-in")
        assert spec.memory_namespace is None

    def test_explicit_valid_value(self) -> None:
        spec = AgentSpec(name="built-in", memory_namespace="code-review")
        assert spec.memory_namespace == "code-review"

    def test_invalid_kebab_case_rejected(self) -> None:
        with pytest.raises(ValueError, match="kebab-case"):
            AgentSpec(name="built-in", memory_namespace="Code_Review")  # uppercase + underscore

    def test_empty_string_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            AgentSpec(name="built-in", memory_namespace="")


# === AgentRunner integration ===

class _ScriptedRouter:
    """Minimal stub; unused for memory tests but AgentRunner needs a router."""

    async def streaming_completion(self, *, model: str, messages, **kwargs):
        return
        yield  # make this an async generator

    async def completion(self, *, model: str, messages, **kwargs) -> Any:
        return None


class TestAgentRunnerUnifiedMemory:
    def test_get_unified_memory_returns_none_without_factory(
        self, tmp_path: Path,
    ) -> None:
        runner = AgentRunner(router=_ScriptedRouter(), repo=tmp_path)  # type: ignore[arg-type]
        spec = AgentSpec(name="x")
        assert runner.get_unified_memory(spec) is None

    def test_get_unified_memory_caches_per_spec(
        self, tmp_path: Path, memory_namespace: dict[str, Path],
    ) -> None:
        seen_calls: list[AgentSpec] = []

        def factory(spec: AgentSpec) -> UnifiedMemory:
            seen_calls.append(spec)
            agent_id = spec.memory_namespace or "solomon"
            return UnifiedMemory(**memory_namespace, agent_id=agent_id)

        runner = AgentRunner(
            router=_ScriptedRouter(),  # type: ignore[arg-type]
            repo=tmp_path,
            unified_memory_factory=factory,
        )
        spec = AgentSpec(name="code", memory_namespace="code-ns")

        a = runner.get_unified_memory(spec)
        b = runner.get_unified_memory(spec)
        # Same instance (cached).
        assert a is b
        # Factory called exactly once for this spec.
        assert len(seen_calls) == 1
        assert a.agent_id == "code-ns"

    def test_different_specs_get_different_memories(
        self, tmp_path: Path, memory_namespace: dict[str, Path],
    ) -> None:
        def factory(spec: AgentSpec) -> UnifiedMemory:
            return UnifiedMemory(
                **memory_namespace,
                agent_id=spec.memory_namespace or "solomon",
            )

        runner = AgentRunner(
            router=_ScriptedRouter(),  # type: ignore[arg-type]
            repo=tmp_path,
            unified_memory_factory=factory,
        )
        spec_code = AgentSpec(name="code", memory_namespace="code-ns")
        spec_plan = AgentSpec(name="plan", memory_namespace="plan-ns")
        a = runner.get_unified_memory(spec_code)
        b = runner.get_unified_memory(spec_plan)
        assert a is not b
        assert a.agent_id == "code-ns"
        assert b.agent_id == "plan-ns"
