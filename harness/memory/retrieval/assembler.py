"""Context assembler (Phase 1, Step 7).

The final stage of the pipeline: take the top-K reranked
memories and format them into an LLM-ready string. We add a
``[id]`` header per memory so the LLM can cite by id, and we
truncate the whole thing at a character budget (so the prompt
doesn't blow up if the retriever returns verbose entries).
"""
from __future__ import annotations

from harness.memory.schema import Memory

#: Default maximum output length (chars). Phase 1 picks 4 KB which
#: fits comfortably in a single LLM context window with the system
#: prompt + user message.
DEFAULT_MAX_CHARS: int = 4_000


class ContextAssembler:
    """Concatenate reranked memories into a single LLM-ready string.

    Each memory gets a one-line ``[id]`` header, then the body.
    The total output is capped at ``max_chars``; when the cap is
    hit, a truncation marker is appended.
    """

    def __init__(self, max_chars: int = DEFAULT_MAX_CHARS) -> None:
        if max_chars <= 0:
            raise ValueError(f"max_chars must be > 0, got {max_chars}")
        self.max_chars = max_chars

    def assemble(
        self,
        query: str,
        items: list[tuple[Memory, float]],
    ) -> str:
        """Format the reranked items into a single string.

        Output shape::

            [id-1]
            content 1
            [id-2]
            content 2
            ...

        When truncated, appends ``\n[... truncated at N chars]``.
        """
        if not items:
            return ""
        chunks: list[str] = []
        for mem, _score in items:
            chunks.append(f"[{mem.id}]\n{mem.content}")
        joined = "\n\n".join(chunks)
        if len(joined) <= self.max_chars:
            return joined
        # Truncate, leave room for the marker
        marker = f"\n[... truncated at {self.max_chars} chars]"
        budget = self.max_chars - len(marker)
        if budget <= 0:
            return marker[: self.max_chars]
        return joined[:budget].rstrip() + marker


__all__ = ["ContextAssembler", "DEFAULT_MAX_CHARS"]
