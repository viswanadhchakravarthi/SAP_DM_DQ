"""One-time move of runtime state from the old top-level folders into ``storage/``.

Everything the agent creates on the fly now lives under one root, split by how much it matters:

    storage/perm/   back this up: episodic_memory.db (+ backups), chroma/ (the vector index;
                    derived, rebuilt by ``python -m src.agents.memory.reindex``), handoff/
    storage/tmp/    safe to delete at any time: logs/

Rule state and datasets have since moved out of ``storage/`` entirely - to ``rules/`` and
``data/`` respectively - so this migration only ever concerns the pre-``storage/`` folders.
A move is still skipped whenever the destination already exists, which it now always does.

Before that split the same things sat in the project root. ``migrate_legacy_layout`` moves them,
non-destructively and idempotently, the first time the new layout starts: a folder is moved only
when the old one exists and the new one does not (an empty new folder is replaced). Nothing is
deleted. If a move fails - typically because a running app still holds a file open on Windows -
it raises instead of letting the app start on an empty store that looks like lost data.

This module must not import ``config``: the paths are passed in.
"""

import logging
import shutil
from pathlib import Path
from typing import Iterable, List, Tuple

logger = logging.getLogger(__name__)


class StorageMigrationError(RuntimeError):
    pass


def _is_empty_dir(path: Path) -> bool:
    return path.is_dir() and not any(path.iterdir())


def _move(old: Path, new: Path, moved: List[str]) -> None:
    if not old.exists():
        return
    if old.resolve() == new.resolve():
        return
    if new.exists():
        if _is_empty_dir(new):
            new.rmdir()
        else:
            logger.warning("Storage layout: both %s and %s exist; leaving %s where it is. "
                           "Merge or remove one of them by hand.", old, new, old)
            return
    new.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.move(str(old), str(new))
    except OSError as exc:
        raise StorageMigrationError(
            f"Could not move {old} to {new}: {exc}. Stop the review app and any running explorer job, "
            f"then start again (the move is retried at startup)."
        ) from exc
    moved.append(f"{old.name} -> {new}")


def migrate_legacy_layout(project_root: Path, *, chroma_dir: Path, memory_dir: Path, client_data_dir: Path,
                          handoff_dir: Path, log_dir: Path, episodic_db: Path) -> List[str]:
    """Move the old top-level folders to their configured new places; returns what was moved."""
    root = Path(project_root)
    moved: List[str] = []
    # The vector index used to sit inside memory_store/: take it out first, so memory_store/ can move whole.
    _move(root / "memory_store" / "chroma", Path(chroma_dir), moved)
    _move(root / "memory_store", Path(memory_dir), moved)
    _move(root / "client_data", Path(client_data_dir), moved)
    _move(root / "handoff", Path(handoff_dir), moved)
    _move(root / "logs", Path(log_dir), moved)
    # The SQLite file plus its backups and journal/WAL files, all named episodic_memory*.
    target_dir = Path(episodic_db).parent
    for old in sorted(root.glob("episodic_memory*.db*")):
        _move(old, target_dir / old.name, moved)
    if moved:
        logger.info("Storage layout: moved %d legacy item(s) into storage/: %s", len(moved), "; ".join(moved))
    return moved
