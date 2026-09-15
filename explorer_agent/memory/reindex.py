
"""
Rebuilds the vector index (semantic memory) entirely from the procedural
registry (source of truth). Use this:
  - After switching vector backends (Chroma -> Qdrant etc.)
  - If the vector store gets corrupted/deleted
  - To verify registry <-> vector index consistency
"""

from . import skill_registry as registry
from . import get_memory_store


def rebuild_index() -> int:
    memory_store = get_memory_store()
    skills = registry.get_all_skills()

    for skill in skills:
        memory_store.add(
            id=skill["skill_id"],
            text=skill["description"],
            metadata={
                "table": skill["table"], "column": skill["column"],
                "hypothesis": skill["hypothesis"], "severity_example": skill["severity_example"],
                "code": skill["check_code"], "skill_id": skill["skill_id"],
            },
        )
    return len(skills)


if __name__ == "__main__":
    count = rebuild_index()
    print(f"Reindexed {count} skill(s) into the configured vector backend.")
