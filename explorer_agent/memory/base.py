
"""
Abstract MemoryStore contract. Every vector-backend implementation
(Chroma, Qdrant, Weaviate, FAISS, ...) must satisfy this interface.

RULE: No other module in this codebase should import chromadb (or any
other vector library) directly. Always go through this interface, so
swapping backends later means writing ONE new adapter class, not
touching promotion.py / retriever.py / main.py.
"""

from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional


class MemoryStore(ABC):
    @abstractmethod
    def add(self, id: str, text: str, metadata: Dict[str, Any]) -> None:
        """Embed `text` and store it, keyed by `id`, with `metadata` attached."""
        ...

    @abstractmethod
    def search(self, query: str, top_k: int = 3,
               filter: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """
        Returns a list of: {"id": str, "text": str, "metadata": dict, "distance": float|None}
        ordered by relevance (most relevant first).
        """
        ...

    @abstractmethod
    def delete(self, id: str) -> None:
        ...

    @abstractmethod
    def count(self) -> int:
        ...
