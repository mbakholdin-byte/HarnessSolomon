"""file adapter — L4 Markdown memory (Phase 1, Step 5).

The file layer is the L4 (file / Markdown) storage and the
**source of truth** for offline / human-review use. One Markdown
file per ``Memory`` entry (YAML frontmatter + body), plus a single
``INDEX.md`` that lists every entry.

This is the storage that maps onto Obsidian / MarkObsidian. The
on-disk format is human-readable — every entry can be opened in
Obsidian, edited manually, and committed to git.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from harness.memory.schema import (
    PROVENANCE_CHAIN_MAX,
    Memory,
    MemoryLayer,
    MemorySource,
    ProvenanceEntry,
)

logger = logging.getLogger(__name__)


# === Constants ===

#: Filename for the index file. Always at the top of ``memory_dir``.
INDEX_FILENAME: str = "INDEX.md"

#: Env var to override the default memory dir.
ENV_MEMORY_DIR: str = "SOLOMON_FILE_MEMORY_DIR"

#: Regex that pulls a single field out of the YAML frontmatter.
#: We do a tiny hand-rolled parser — full PyYAML would add a dep
#: we don't need for the simple ``key: value`` lines we use.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


# === Helpers ===

def _default_dir() -> Path:
    """Default memory dir for the file adapter.

    Priority: ``$SOLOMON_FILE_MEMORY_DIR`` → ``./data/memory`` →
    ``~/SolomonMemory`` (Windows) / ``~/.solomon/memory`` (Unix).
    """
    env = os.environ.get(ENV_MEMORY_DIR, "").strip()
    if env:
        return Path(env)
    cwd = Path.cwd() / "data" / "memory"
    if (cwd.parent).exists():
        return cwd
    home = Path(os.path.expanduser("~"))
    if os.name == "nt":
        return home / "SolomonMemory"
    return home / ".solomon" / "memory"


def _safe_filename(memory_id: str) -> str:
    """Sanitise a memory id into a safe filename stem.

    Strips path separators and ``..`` so that the resulting file
    stays inside ``memory_dir``. Empty / dot-only ids become
    ``"default"``.
    """
    safe = re.sub(r"[^\w\-.]", "_", memory_id)
    safe = safe.strip("._") or "default"
    if safe in (".", ".."):
        safe = "default"
    return safe


def _escape_yaml(s: str) -> str:
    """Make a string safe to put inside a quoted YAML scalar."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _dump_frontmatter(memory: Memory) -> str:
    """Render the YAML frontmatter block for a Memory.

    The frontmatter is intentionally simple (string scalars + lists
    of strings + inline-dict lists). Nested metadata is dumped into
    a trailing ``<!-- metadata: ... -->`` HTML comment in the body —
    human-skippable but machine-roundtrip-safe. (Full PyYAML would
    work too but adds a dep we don't need for Phase 0+.)
    """
    lines: list[str] = ["---"]
    lines.append(f'id: "{_escape_yaml(memory.id)}"')
    lines.append(f'layer: "{memory.layer}"')
    lines.append(f'source: "{memory.source}"')
    lines.append(f"confidence: {memory.confidence}")
    if memory.ttl is not None:
        lines.append(f"ttl: {memory.ttl}")
    lines.append(f'ts: "{memory.ts.isoformat()}"')
    if memory.tags:
        lines.append("tags:")
        for t in memory.tags:
            lines.append(f'  - "{_escape_yaml(t)}"')
    if memory.links:
        lines.append("links:")
        for link in memory.links:
            lines.append(f'  - "{_escape_yaml(link)}"')
    if memory.provenance:
        lines.append("provenance:")
        for p in memory.provenance:
            lines.append(
                f'  - {{layer: "{p.layer}", source: "{p.source}", id: "{_escape_yaml(p.id)}"}}'
            )
    lines.append("---")
    return "\n".join(lines)


_METADATA_HTML_RE = re.compile(
    r"<!--\s*memory-metadata\s*:\s*(\{.*?\})\s*-->", re.DOTALL
)


def _body_with_metadata(content: str, metadata: dict[str, Any]) -> str:
    """Append a hidden JSON metadata block after the body content."""
    if not metadata:
        return content
    blob = json.dumps(metadata, ensure_ascii=False)
    return f"{content}\n\n<!-- memory-metadata: {blob} -->"


def _extract_metadata(body: str) -> dict[str, Any]:
    """Pull the hidden metadata JSON block out of a body, if any."""
    m = _METADATA_HTML_RE.search(body)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Parse the simple YAML frontmatter we emit. Raises on shape mismatch."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        raise ValueError("file does not start with a YAML frontmatter block")
    yaml_block, _ = m.groups()
    out: dict[str, Any] = {}
    # Walk line by line; handle the few shapes we emit.
    list_key: str | None = None
    dict_acc: dict[str, str] | None = None
    for raw in yaml_block.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if line.startswith("  - ") and list_key is not None:
            # List item. Could be a string scalar or a {k: v, ...} dict.
            item = line[4:].strip()
            if item.startswith("{") and item.endswith("}"):
                # Inline dict
                pairs = re.findall(r'(\w+):\s*"([^"]*)"', item)
                out[list_key].append({k: v for k, v in pairs})
            elif dict_acc is not None:
                # Continuing a dict under a list item
                if ":" in item:
                    k, v = item.split(":", 1)
                    dict_acc[k.strip()] = v.strip().strip('"')
                    if len(dict_acc) == 3:  # layer + source + id
                        out[list_key].append(dict_acc)
                        dict_acc = None
            else:
                out[list_key].append(item.strip('"'))
            continue
        if ":" in line and not line.startswith(" "):
            # New top-level key
            k, v = line.split(":", 1)
            k = k.strip()
            v = v.strip()
            if v == "":
                # Could be a list, or a dict (rare). Switch to list-mode.
                list_key = k
                out[k] = []
                dict_acc = None
            else:
                list_key = None
                # Strip surrounding quotes
                v_stripped = v.strip()
                if v_stripped.startswith('"') and v_stripped.endswith('"'):
                    v_stripped = v_stripped[1:-1]
                out[k] = v_stripped
    return out


def _body(text: str) -> str:
    """Return the markdown body (text after the frontmatter)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return text
    return m.group(2)


# === Adapter ===

class FileAdapter:
    """File-backed L4 Markdown memory adapter.

    Args:
        memory_dir: Directory holding ``<id>.md`` files plus an
                    ``INDEX.md``. Created if missing.
    """

    def __init__(self, memory_dir: Path | str | None = None) -> None:
        if memory_dir is None:
            self.memory_dir = _default_dir()
        else:
            self.memory_dir = Path(memory_dir)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        logger.debug("FileAdapter: dir=%s", self.memory_dir)

    # --- internal ---

    def _path_for(self, memory_id: str) -> Path:
        return self.memory_dir / f"{_safe_filename(memory_id)}.md"

    def _index_path(self) -> Path:
        return self.memory_dir / INDEX_FILENAME

    @staticmethod
    def _append_provenance(memory: Memory) -> Memory:
        provenance = list(memory.provenance or [])
        has_hop = any(
            p.layer == "L4" and p.source == "file" and p.id == memory.id
            for p in provenance
        )
        if not has_hop:
            provenance.append(
                ProvenanceEntry(layer="L4", source="file", id=memory.id)
            )
        if len(provenance) > PROVENANCE_CHAIN_MAX:
            provenance = provenance[-PROVENANCE_CHAIN_MAX:]
        return memory.model_copy(update={"provenance": provenance})

    def _rebuild_index(self) -> None:
        """Rebuild INDEX.md from the on-disk .md files. Idempotent."""
        lines: list[str] = ["# Memory Index", ""]
        lines.append(f"Total entries: {len(self._md_files())}")
        lines.append("")
        lines.append("| id | layer | source | ts |")
        lines.append("|---|---|---|---|")
        for md in sorted(self._md_files(), key=lambda p: p.stem):
            try:
                parsed = _parse_frontmatter(md.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            lines.append(
                f'| {parsed.get("id", md.stem)} | '
                f'{parsed.get("layer", "?")} | '
                f'{parsed.get("source", "?")} | '
                f'{parsed.get("ts", "?")} |'
            )
        self._index_path().write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _md_files(self) -> list[Path]:
        """Return all .md files except INDEX.md."""
        return [
            p for p in self.memory_dir.glob("*.md")
            if p.name != INDEX_FILENAME
        ]

    # --- public API ---

    def write(self, memory: Memory) -> None:
        """Write a Memory to ``<id>.md`` and rebuild INDEX.md."""
        stamped = self._append_provenance(memory)
        body = _body_with_metadata(stamped.content, stamped.metadata or {})
        text = _dump_frontmatter(stamped) + "\n" + body + "\n"
        self._path_for(memory.id).write_text(text, encoding="utf-8")
        self._rebuild_index()
        logger.debug("FileAdapter.write: id=%s", memory.id)

    def read(self) -> list[Memory]:
        """Return every Memory in the dir, parsed from .md files."""
        out: list[Memory] = []
        for md in self._md_files():
            mem = self._parse_md(md)
            if mem is not None:
                out.append(mem)
        return out

    def get(self, memory_id: str) -> Memory | None:
        """Read one Memory by id. Returns None if absent."""
        path = self._path_for(memory_id)
        if not path.exists():
            return None
        return self._parse_md(path)

    def _parse_md(self, path: Path) -> Memory | None:
        """Parse one .md file into a Memory (best-effort)."""
        try:
            text = path.read_text(encoding="utf-8")
            parsed = _parse_frontmatter(text)
            body = _body(text).rstrip("\n")
            # Body = content + hidden metadata block (if any)
            metadata = _extract_metadata(body)
            content = _METADATA_HTML_RE.sub("", body).rstrip("\n")
            return Memory(
                id=parsed["id"],
                content=content,
                layer=parsed["layer"],  # type: ignore[arg-type]
                source=parsed["source"],  # type: ignore[arg-type]
                confidence=float(parsed.get("confidence", 1.0)),
                ttl=(
                    int(parsed["ttl"])
                    if "ttl" in parsed and parsed["ttl"] != ""
                    else None
                ),
                ts=__import__("datetime").datetime.fromisoformat(parsed["ts"]),
                tags=parsed.get("tags", []),
                links=parsed.get("links", []),
                provenance=[
                    ProvenanceEntry(**p) for p in parsed.get("provenance", [])
                ],
                metadata=metadata,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("FileAdapter: failed to parse %s: %s", path, exc)
            return None

    def search(
        self,
        query: str | None = None,
        tag: str | None = None,
    ) -> list[Memory]:
        """Filter by content substring and/or exact tag.

        ``search()`` (no args) returns everything — useful as a
        "list all" primitive.
        """
        entries = self.read()
        if query:
            q = query.lower()
            entries = [e for e in entries if q in e.content.lower()]
        if tag:
            entries = [e for e in entries if tag in (e.tags or [])]
        return entries


__all__ = [
    "FileAdapter",
    "INDEX_FILENAME",
    "ENV_MEMORY_DIR",
]
