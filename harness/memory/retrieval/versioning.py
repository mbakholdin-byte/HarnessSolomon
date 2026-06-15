"""Phase 3: embedding model versioning.

Stored in ``Memory.metadata.embedding_version`` so a model swap
(e.g. multilingual-e5-small -> bge-m3) can be detected. The
``DenseRetriever`` filters vectors whose version doesn't match
the current loaded model; the BM25 path is unaffected.
"""
from __future__ import annotations

#: The current embedding model version. Bump this when:
#:   - The default model changes (multilingual-e5-small -> bge-m3).
#:   - The precision changes (int8 -> fp32).
#:   - The postprocessing changes (mean-pool -> cls-pool).
#:
#: Operators with persisted vectors from a previous version can
#: re-embed via the migration tool (Phase 3.5+).
EMBEDDING_MODEL_VERSION: str = "multilingual-e5-small-int8@1"
