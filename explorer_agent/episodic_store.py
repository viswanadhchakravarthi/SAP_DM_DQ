"""
Episodic memory store - SQLite-backed persistent log of every finding
produced by the Explorer agent, plus human review decisions.

This is the FIRST piece of memory that survives across runs. Week 1's
findings_log.json was ephemeral/per-run and discarded after. This module
is also what Week 3's promotion pipeline will read from later to build
procedural/semantic memory - so schema decisions here matter downstream.
"""

import sqlite3
import uuid
import json
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from contextlib import contextmanager

from .config import Config

DB_PATH = Path(Config.EPISODIC_DB_PATH)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    model TEXT,
    table_names TEXT,    -- JSON list
    notes TEXT
);

CREATE TABLE IF NOT EXISTS findings (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    table_name TEXT NOT NULL,
    column_name TEXT NOT NULL,
    hypothesis TEXT,
    check_code TEXT,
    result_summary TEXT,
    severity TEXT,
    confidence TEXT,
    reusable INTEGER,             -- 0/1, LLM's opinion - human confirms via review
    raw_result TEXT,              -- JSON string, already privacy-sanitized upstream
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING / APPROVED / REJECTED
    reviewed_at TEXT,
    reviewer_comment TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_findings_status ON findings(status);
CREATE INDEX IF NOT EXISTS idx_findings_run ON findings(run_id);
"""


@contextmanager
def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Add to explorer_agent/episodic_store.py

def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent schema migration - safe to call on every startup."""
    existing_cols = [row["name"] for row in conn.execute("PRAGMA table_info(findings)").fetchall()]
    if "promoted_at" not in existing_cols:
        conn.execute("ALTER TABLE findings ADD COLUMN promoted_at TEXT")


def _migrate_finding_items(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS finding_items (
        id TEXT PRIMARY KEY,
        finding_id TEXT NOT NULL,
        row_index INTEGER,
        key_field TEXT,
        key_value TEXT,
        issue_detail TEXT,
        corrected_data TEXT,
        status TEXT NOT NULL DEFAULT 'PENDING',
        created_at TEXT NOT NULL,
        reviewed_at TEXT,
        reviewer_comment TEXT,
        FOREIGN KEY (finding_id) REFERENCES findings(id)
    );

    CREATE INDEX IF NOT EXISTS idx_finding_items_finding ON finding_items(finding_id);
    """)


def init_db():
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)  # addition line for Week 3
        _migrate_finding_items(conn)  # add this call


def create_run(model: str, table_names: List[str], notes: str = "") -> str:
    run_id = str(uuid.uuid4())
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO runs (run_id, started_at, model, table_names, notes) VALUES (?, ?, ?, ?, ?)",
            (run_id, datetime.now(timezone.utc).isoformat(), model, json.dumps(table_names), notes),
        )
    return run_id


def save_finding(run_id: str, table: str, column: str, hypothesis: str, check_code: str,
                 result_summary: str, severity: str, confidence: str, reusable: bool,
                 raw_result: Any) -> str:
    finding_id = str(uuid.uuid4())
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO findings
            (id, run_id, table_name, column_name, hypothesis, check_code,
             result_summary, severity, confidence, reusable, raw_result,
             created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')""",
            (finding_id, run_id, table, column, hypothesis, check_code,
             result_summary, severity, confidence, int(bool(reusable)),
             json.dumps(raw_result, default=str),
             datetime.now(timezone.utc).isoformat()),
        )
    return finding_id


def get_findings(run_id: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
    query = "SELECT * FROM findings WHERE 1=1"
    params: List[Any] = []
    if run_id:
        query += " AND run_id = ?"
        params.append(run_id)
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY created_at DESC"

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def get_finding(finding_id: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
        return dict(row) if row else None


def update_decision(finding_id: str, status: str, comment: str = "") -> bool:
    if status not in ("APPROVED", "REJECTED"):
        raise ValueError(f"Invalid status: {status}")
    with get_connection() as conn:
        cur = conn.execute(
            """UPDATE findings SET status = ?, reviewed_at = ?, reviewer_comment = ? WHERE id = ?""",
            (status, datetime.now(timezone.utc).isoformat(), comment, finding_id),
        )
        return cur.rowcount > 0


def get_runs() -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM runs ORDER BY started_at DESC").fetchall()
        return [dict(r) for r in rows]


def get_stats(run_id: Optional[str] = None) -> Dict[str, int]:
    query = "SELECT status, COUNT(*) as cnt FROM findings"
    params: List[Any] = []
    if run_id:
        query += " WHERE run_id = ?"
        params.append(run_id)
    query += " GROUP BY status"
    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        stats = {"PENDING": 0, "APPROVED": 0, "REJECTED": 0}
        for r in rows:
            stats[r["status"]] = r["cnt"]
        stats["TOTAL"] = sum(v for k, v in stats.items())
        return stats


# Addition for Week 3
def mark_promoted(finding_id: str) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE findings SET promoted_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), finding_id),
        )
        return cur.rowcount > 0


# Addition for Week 3
def get_promotable_findings(run_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Approved + marked reusable by LLM + not yet promoted + has captured code."""
    query = """SELECT * FROM findings
               WHERE status = 'APPROVED' AND reusable = 1
                 AND promoted_at IS NULL AND check_code IS NOT NULL AND check_code != ''"""
    params: List[Any] = []
    if run_id:
        query += " AND run_id = ?"
        params.append(run_id)
    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def save_finding_items(finding_id: str, items: List[Dict[str, Any]]) -> List[str]:
    ids = []
    with get_connection() as conn:
        for item in items:
            item_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO finding_items
                (id, finding_id, row_index, key_field, key_value, issue_detail,
                 corrected_data, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)""",
                (item_id, finding_id, item.get("row_index"), item.get("key_field"),
                 str(item.get("key_value")), item.get("issue_detail"), item.get("corrected_data", ""),
                 datetime.now(timezone.utc).isoformat()),
            )
            ids.append(item_id)
    return ids


def get_finding_items(finding_id: str) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM finding_items WHERE finding_id = ? ORDER BY row_index", (finding_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def update_item_decision(item_id: str, status: str, corrected_data: str = "", comment: str = "") -> bool:
    if status not in ("APPROVED", "REJECTED"):
        raise ValueError(f"Invalid status: {status}")
    with get_connection() as conn:
        cur = conn.execute(
            """UPDATE finding_items SET status = ?, corrected_data = ?,
               reviewed_at = ?, reviewer_comment = ? WHERE id = ?""",
            (status, corrected_data, datetime.now(timezone.utc).isoformat(), comment, item_id),
        )
        return cur.rowcount > 0


def get_item_stats(finding_id: str) -> Dict[str, int]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM finding_items WHERE finding_id = ? GROUP BY status",
            (finding_id,),
        ).fetchall()
        stats = {"PENDING": 0, "APPROVED": 0, "REJECTED": 0}
        for r in rows:
            stats[r["status"]] = r["cnt"]
        stats["TOTAL"] = sum(v for k, v in stats.items())
        return stats