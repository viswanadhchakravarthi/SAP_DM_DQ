
"""
Factory for the configured MemoryStore backend.

To switch backends later (Qdrant/Weaviate/FAISS): implement a new
MemoryStore subclass, then change ONLY the branch below (or the
Config.VECTOR_BACKEND value). No other file needs to change.
"""

from .base import MemoryStore
from ..config import Config

_instance: MemoryStore = None


def get_memory_store() -> MemoryStore:
    global _instance
    if _instance is not None:
        return _instance

    backend = Config.VECTOR_BACKEND
    if backend == "chroma":
        from .chroma_store import ChromaMemoryStore
        _instance = ChromaMemoryStore()
    else:
        raise ValueError(f"Unknown VECTOR_BACKEND: {backend}")

    # A backend may discard an index built with a different embedding model;
    # semantic memory is derived, so rebuild it from the procedural registry.
    if getattr(_instance, "needs_reindex", False):
        from .reindex import rebuild_index
        _instance.needs_reindex = False
        rebuild_index()

    return _instance
