
"""
Domain-specific retrieval wrapper on top of the generic MemoryStore
adapter. This is intentionally a separate layer from `memory_store`
itself: `memory_store.search(...)` is generic vector search;
`SkillRetriever.retrieve_hints(...)` knows HOW to build a good query
for "column signature -> relevant past skills" and how to format
results for prompt injection.
"""

from typing import List, Dict, Any, Optional

from . import get_memory_store
from .base import MemoryStore
from ..config import Config


class SkillRetriever:
    def __init__(self, memory_store: Optional[MemoryStore] = None):
        self.memory_store = memory_store or get_memory_store()

    def retrieve_hints(self, table: str, column: str, dtype: str,
                       business_meaning: str, top_k: Optional[int] = None) -> List[Dict[str, Any]]:
        top_k = top_k or Config.RETRIEVAL_TOP_K
        query = f"Table: {table}, Column: {column}, dtype: {dtype}, business meaning: {business_meaning}"

        raw_results = self.memory_store.search(query, top_k=top_k)

        hints = []
        for r in raw_results:
            meta = r["metadata"]
            hints.append({
                "skill_id": meta.get("skill_id"),
                "source_table_column": f"{meta.get('table')}.{meta.get('column')}",
                "hypothesis": meta.get("hypothesis", ""),
                "code": meta.get("code", ""),
                "severity_example": meta.get("severity_example", ""),
                "distance": r.get("distance"),
            })
        return hints

    def format_hints_for_prompt(self, hints: List[Dict[str, Any]]) -> str:
        if not hints:
            return "No relevant memory hints found for this column."

        lines = ["Relevant checks from PAST projects (verify relevance before trusting - "
                 "you are free to ignore, modify, or extend these; they are suggestions, not rules):"]
        for h in hints:
            lines.append(
                f"\n- From {h['source_table_column']} (typical severity: {h['severity_example']}):\n"
                f"  Hypothesis: {h['hypothesis']}\n"
                f"  Code:\n  {h['code'].replace(chr(10), chr(10) + '  ')}"
            )
        return "\n".join(lines)
