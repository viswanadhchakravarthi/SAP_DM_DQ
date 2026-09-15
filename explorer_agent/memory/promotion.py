
"""
Promotion pipeline: episodic memory (approved findings) -> procedural
memory (skill registry) + semantic memory (vector index for retrieval).

Includes near-duplicate detection: before promoting, checks if a very
similar skill already exists in the vector store. If so, skips promotion
(marks the finding as promoted/reviewed anyway, so it doesn't stay a
perpetual "pending promotion" candidate) rather than creating redundant
near-identical skills.

Deliberately conservative gating: only promotes findings that are
(a) human-APPROVED, (b) marked reusable by the LLM's own judgment,
(c) have actual captured check_code (nothing to promote otherwise).
This is the human-in-the-loop gate discussed in the architecture -
nothing gets promoted without a human approval first.
"""

from typing import Dict, Any, Optional

from .. import episodic_store as store
from . import skill_registry as registry
from . import get_memory_store
from ..config import Config


def promote_approved_findings(run_id: Optional[str] = None,
                             dedup_threshold: Optional[float] = None) -> Dict[str, Any]:
    memory_store = get_memory_store()
    candidates = store.get_promotable_findings(run_id=run_id)
    dedup_threshold = dedup_threshold if dedup_threshold is not None else Config.DEDUP_DISTANCE_THRESHOLD

    promoted_ids = []
    skipped_duplicates = []

    for f in candidates:
        description = f"{f['table_name']}.{f['column_name']}: {f['result_summary']}"

        # --- Near-duplicate check against EXISTING promoted skills ---
        existing_matches = memory_store.search(description, top_k=1)
        if existing_matches and existing_matches[0].get("distance") is not None \
                and existing_matches[0]["distance"] < dedup_threshold:
            skipped_duplicates.append({
                "finding_id": f["id"],
                "summary": f["result_summary"],
                "matched_existing_skill_id": existing_matches[0]["metadata"].get("skill_id"),
                "distance": existing_matches[0]["distance"],
            })
            store.mark_promoted(f["id"])  # prevents it from being re-evaluated forever
            continue

        skill = registry.save_skill(
            table=f["table_name"], column=f["column_name"],
            hypothesis=f.get("hypothesis", ""), description=description,
            check_code=f["check_code"], severity_example=f["severity"],
            source_finding_id=f["id"], source_run_id=f["run_id"],
        )

        memory_store.add(
            id=skill["skill_id"],
            text=description,
            metadata={
                "table": skill["table"],
                "column": skill["column"],
                "hypothesis": skill["hypothesis"],
                "severity_example": skill["severity_example"],
                "code": skill["check_code"],
                "skill_id": skill["skill_id"],
            },
        )

        store.mark_promoted(f["id"])
        promoted_ids.append(skill["skill_id"])

    return {
        "promoted_count": len(promoted_ids),
        "promoted_skill_ids": promoted_ids,
        "skipped_duplicates_count": len(skipped_duplicates),
        "skipped_duplicates": skipped_duplicates,
        "candidates_evaluated": len(candidates),
    }


if __name__ == "__main__":
    import argparse
    from .. import episodic_store as es

    parser = argparse.ArgumentParser(description="Promote approved+reusable findings to skill lib")
    parser.add_argument("--run-id", default=None, help="Optional: limit promotion to one run")
    parser.add_argument("--dedup-threshold", type=float, default=None)
    args = parser.parse_args()

    es.init_db()
    result = promote_approved_findings(run_id=args.run_id, dedup_threshold=args.dedup_threshold)
    print(f"Promoted: {result['promoted_count']} / Evaluated: {result['candidates_evaluated']}")

    print(f"Skipped as duplicates: {result['skipped_duplicates_count']}")
    for d in result["skipped_duplicates"]:
        print(f"  - Finding {d['finding_id'][:8]} (distance {d['distance']:.4f}) - "
              f"matches existing skill {d['matched_existing_skill_id']}")

    print(f"Skill IDs: {result['promoted_skill_ids']}")
