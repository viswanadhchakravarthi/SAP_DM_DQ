
"""
Procedural memory: a human-readable, auditable registry of promoted
skills (verified, reusable checks). This is the SOURCE OF TRUTH.

The vector store (Chroma etc.) is a derived search index built FROM this
registry - never the other way around. If the vector index is ever lost
or you switch backends, `reindex.py` rebuilds it entirely from these files.
"""

import json
import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

from ..config import Config

REGISTRY_DIR = Path(Config.MEMORY_BASE_DIR) / "procedural"
SKILLS_DIR = REGISTRY_DIR / "skills"
REGISTRY_FILE = REGISTRY_DIR / "skill_registry.json"


def _ensure_dirs() -> None:
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)


def _load_registry() -> List[Dict[str, Any]]:
    if not REGISTRY_FILE.exists():
        return []
    with open(REGISTRY_FILE) as f:
        return json.load(f)


def _save_registry(entries: List[Dict[str, Any]]) -> None:
    _ensure_dirs()
    with open(REGISTRY_FILE, "w") as f:
        json.dump(entries, f, indent=2, default=str)


def save_skill(table: str, column: str, hypothesis: str, description: str,
               check_code: str, severity_example: str, source_finding_id: str,
               source_run_id: str, tags: Optional[List[str]] = None) -> Dict[str, Any]:
    """Persists a new skill: writes the code file + appends a registry entry. Returns the entry."""
    _ensure_dirs()
    skill_id = str(uuid.uuid4())
    code_filename = f"{table.lower()}_{column.lower()}_{skill_id[:8]}.py"
    code_path = SKILLS_DIR / code_filename

    header = (
        f"# Auto-promoted skill - DO NOT edit table/column context without updating registry\n"
        f"# Table: {table} | Column: {column}\n"
        f"# Hypothesis: {hypothesis}\n"
        f"# Promoted from finding: {source_finding_id} (run: {source_run_id})\n\n"
    )
    code_path.write_text(header + check_code)

    entry = {
        "skill_id": skill_id,
        "table": table,
        "column": column,
        "tags": tags or [],
        "hypothesis": hypothesis,
        "description": description,
        "check_code": check_code,         # duplicated inline too - convenient for retrieval/prompt injection
        "code_path": str(code_path),
        "severity_example": severity_example,
        "source_finding_id": source_finding_id,
        "source_run_id": source_run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "reuse_count": 0,
    }

    entries = _load_registry()
    entries.append(entry)
    _save_registry(entries)
    return entry


def get_all_skills() -> List[Dict[str, Any]]:
    return _load_registry()


def get_skill(skill_id: str) -> Optional[Dict[str, Any]]:
    return next((e for e in _load_registry() if e["skill_id"] == skill_id), None)


def increment_reuse(skill_id: str) -> None:
    entries = _load_registry()
    for e in entries:
        if e["skill_id"] == skill_id:
            e["reuse_count"] = e.get("reuse_count", 0) + 1
    _save_registry(entries)


# adding exact-match lookup
def get_skills_for_table_column(table: str, column: str) -> List[Dict[str, Any]]:
    """
    EXACT match lookup (case-insensitive) - used by the cache fast path.
    Unlike SkillRetriever (semantic/fuzzy search, used during exploration
    to SUGGEST hints), this requires an exact table+column match, because
    the fast path needs deterministic reuse of a proven check, not a
    "kind of similar" suggestion.
    """
    entries = _load_registry()
    return [
        e for e in entries
        if e["table"].upper() == table.upper() and e["column"].upper() == column.upper()
    ]