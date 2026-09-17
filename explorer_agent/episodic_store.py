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
from typing import Optional, List, Dict, Any, Tuple
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
    if "category" not in existing_cols:
        conn.execute("ALTER TABLE findings ADD COLUMN category TEXT DEFAULT 'CORRECTNESS'")
    if "rule_scope" not in existing_cols:
        conn.execute("ALTER TABLE findings ADD COLUMN rule_scope TEXT DEFAULT 'UNIVERSAL'")
    if "industry" not in existing_cols:
        conn.execute("ALTER TABLE findings ADD COLUMN industry TEXT")
    if "fix_type" not in existing_cols:
        conn.execute("ALTER TABLE findings ADD COLUMN fix_type TEXT")
    if "auto_fix_value" not in existing_cols:
        conn.execute("ALTER TABLE findings ADD COLUMN auto_fix_value TEXT")
    if "is_anomaly" not in existing_cols:
        conn.execute("ALTER TABLE findings ADD COLUMN is_anomaly INTEGER DEFAULT 0")
    if "sub_type" not in existing_cols:
        conn.execute("ALTER TABLE findings ADD COLUMN sub_type TEXT")


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

    existing_item_cols = [row["name"] for row in conn.execute("PRAGMA table_info(finding_items)").fetchall()]
    if "duplicate_group_id" not in existing_item_cols:
        conn.execute("ALTER TABLE finding_items ADD COLUMN duplicate_group_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_finding_items_dupgroup ON finding_items(duplicate_group_id)")
    if "similarity_score" not in existing_item_cols:
        conn.execute("ALTER TABLE finding_items ADD COLUMN similarity_score REAL")
    if "match_type" not in existing_item_cols:
        conn.execute("ALTER TABLE finding_items ADD COLUMN match_type TEXT")
    if "match_reasons" not in existing_item_cols:
        conn.execute("ALTER TABLE finding_items ADD COLUMN match_reasons TEXT")
    if "is_golden_record" not in existing_item_cols:
        conn.execute("ALTER TABLE finding_items ADD COLUMN is_golden_record INTEGER DEFAULT 0")
    if "review_verdict" not in existing_item_cols:
        conn.execute("ALTER TABLE finding_items ADD COLUMN review_verdict TEXT DEFAULT 'PENDING'")
    if "suggested_action" not in existing_item_cols:
        conn.execute("ALTER TABLE finding_items ADD COLUMN suggested_action TEXT")
    if "record_data" not in existing_item_cols:
        # JSON object of the record's display fields (duplicate side-by-side comparison)
        conn.execute("ALTER TABLE finding_items ADD COLUMN record_data TEXT")
    if "decision_source" not in existing_item_cols:
        # 'REMEMBERED' when review_verdict was pre-filled from client knowledge,
        # 'HUMAN' once a reviewer sets it in this run.
        conn.execute("ALTER TABLE finding_items ADD COLUMN decision_source TEXT")


def _migrate_runs(conn: sqlite3.Connection) -> None:
    existing_cols = [row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()]
    if "client_id" not in existing_cols:
        # Client (company) the data belongs to - see explorer_agent/client_knowledge.py.
        conn.execute("ALTER TABLE runs ADD COLUMN client_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_client ON runs(client_id)")
    if "client_name" not in existing_cols:
        conn.execute("ALTER TABLE runs ADD COLUMN client_name TEXT")


def init_db():
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        _migrate_finding_items(conn)
        _migrate_runs(conn)


# --- Per-pillar review-verdict vocabulary -----------------------------------
# finding_items.review_verdict is a plain TEXT column with no DB-level CHECK
# constraint; the vocabulary below is validated in Python (update_item_verdict)
# and lets each pillar's UI show a disposition that actually fits the kind of
# decision being made, instead of one generic Approve/Reject everywhere.
ITEM_DISPOSITIONS = {
    "DUPLICATE": {"DUPLICATE", "UNIQUE", "TO_BE_CONFIRMED"},
    "ACTIVENESS": {"ALLOWED_ACTIVE", "CONFIRMED_INACTIVE"},
    "COMPLETENESS": {"MISSING_VALUE", "NOT_APPLICABLE", "INTENTIONALLY_BLANK", "REQUIRES_BUSINESS_INPUT"},
    "CORRECTNESS_RELATIONSHIP": {
        "CONFIRMED_ISSUE", "FALSE_POSITIVE", "REQUIRES_MASTER_DATA_CORRECTION",
        "REQUIRES_BUSINESS_REVIEW", "EXCLUDE_FROM_PROFILING",
    },
    "ANOMALY": {"LEGITIMATE", "NEEDS_INVESTIGATION"},
}
ALL_VALID_VERDICTS = {"PENDING", "APPROVED", "REJECTED"}.union(*ITEM_DISPOSITIONS.values())
# Verdicts meaning "still needs follow-up" keep status=PENDING; every other
# verdict resolves the item to status=APPROVED.
_OPEN_VERDICTS = {
    "PENDING", "TO_BE_CONFIRMED", "REQUIRES_BUSINESS_INPUT",
    "REQUIRES_BUSINESS_REVIEW", "REQUIRES_MASTER_DATA_CORRECTION", "NEEDS_INVESTIGATION",
}

_RELATIONSHIP_KEYWORDS = (
    "not found in", "does not exist in", "missing from", "not present in",
    "orphan", "referential", "master data", "cross-table", "tables[",
)


def _effective_sub_type(row: Dict[str, Any]) -> Optional[str]:
    """sub_type, falling back to a keyword heuristic for rows an LLM left null."""
    if row.get("category") != "CORRECTNESS":
        return None
    if row.get("sub_type"):
        return row["sub_type"]
    text = f"{row.get('hypothesis','')} {row.get('result_summary','')} {row.get('check_code','')}".lower()
    return "RELATIONSHIP_INTEGRITY" if any(k in text for k in _RELATIONSHIP_KEYWORDS) else "VALUE_ERROR"


def create_run(model: str, table_names: List[str], notes: str = "",
               client_id: Optional[str] = None, client_name: Optional[str] = None) -> str:
    run_id = str(uuid.uuid4())
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO runs (run_id, started_at, model, table_names, notes, client_id, client_name)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, datetime.now(timezone.utc).isoformat(), model, json.dumps(table_names), notes,
             client_id, client_name),
        )
    return run_id


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row else None


def set_run_client(run_id: str, client_id: str, client_name: str) -> bool:
    """Assign a client to a run created before clients existed (never re-assigns)."""
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE runs SET client_id = ?, client_name = ? WHERE run_id = ? AND client_id IS NULL",
            (client_id, client_name, run_id),
        )
        return cur.rowcount > 0


def save_finding(run_id: str, table: str, column: str, hypothesis: str, check_code: str,
                 result_summary: str, severity: str, confidence: str, reusable: bool,
                 raw_result: Any, category: str = "CORRECTNESS", rule_scope: str = "UNIVERSAL",
                 industry: Optional[str] = None, fix_type: Optional[str] = None,
                 auto_fix_value: Optional[str] = None, is_anomaly: bool = False,
                 sub_type: Optional[str] = None) -> str:
    finding_id = str(uuid.uuid4())
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO findings
            (id, run_id, table_name, column_name, hypothesis, check_code,
             result_summary, severity, confidence, reusable, raw_result,
             created_at, status, category, rule_scope, industry, fix_type, auto_fix_value, is_anomaly, sub_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?, ?, ?)""",
            (finding_id, run_id, table, column, hypothesis, check_code,
             result_summary, severity, confidence, int(bool(reusable)),
             json.dumps(raw_result, default=str),
             datetime.now(timezone.utc).isoformat(),
             category or "CORRECTNESS", rule_scope or "UNIVERSAL",
             industry, fix_type, auto_fix_value, int(bool(is_anomaly)), sub_type),
        )
    return finding_id


def _finding_filters(run_id: Optional[str] = None, status: Optional[str] = None,
                     category: Optional[str] = None, rule_scope: Optional[str] = None,
                     industry: Optional[str] = None, is_anomaly: Optional[bool] = None,
                     client_id: Optional[str] = None) -> Tuple[str, List[Any]]:
    """WHERE-clause fragment (starting with ' AND', or empty) + params shared by the findings queries."""
    clauses: List[str] = []
    params: List[Any] = []
    for column, value in (("run_id", run_id), ("status", status), ("category", category),
                          ("rule_scope", rule_scope), ("industry", industry)):
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)
    if is_anomaly is not None:
        clauses.append("is_anomaly = ?")
        params.append(1 if is_anomaly else 0)
    if client_id:
        clauses.append("run_id IN (SELECT run_id FROM runs WHERE client_id = ?)")
        params.append(client_id)
    return "".join(f" AND {c}" for c in clauses), params


def get_findings(run_id: Optional[str] = None, status: Optional[str] = None,
                 category: Optional[str] = None, rule_scope: Optional[str] = None,
                 industry: Optional[str] = None, is_anomaly: Optional[bool] = None,
                 client_id: Optional[str] = None) -> List[Dict[str, Any]]:
    where, params = _finding_filters(run_id, status, category, rule_scope, industry, is_anomaly, client_id)
    query = f"SELECT * FROM findings WHERE 1=1{where} ORDER BY created_at DESC"

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        results = [dict(r) for r in rows]
        for r in results:
            r["effective_sub_type"] = _effective_sub_type(r)
        return results


# Columns for the review app's list view - excludes the heavy check_code/
# raw_result blobs so switching pillar tabs doesn't ship every finding's full
# profile-result/check-code payload over the wire (see CR3: lazy loading).
_LIGHT_COLUMNS = (
    "id, run_id, table_name, column_name, hypothesis, result_summary, severity, "
    "confidence, reusable, created_at, status, reviewed_at, reviewer_comment, promoted_at, "
    "category, sub_type, rule_scope, industry, fix_type, auto_fix_value, is_anomaly, "
    # Row-level review progress, shown on the finding cards.
    "(SELECT COUNT(*) FROM finding_items fi WHERE fi.finding_id = findings.id) AS item_count, "
    "(SELECT COUNT(*) FROM finding_items fi WHERE fi.finding_id = findings.id "
    " AND COALESCE(fi.review_verdict, 'PENDING') != 'PENDING') AS reviewed_count, "
    "(SELECT COUNT(DISTINCT fi.duplicate_group_id) FROM finding_items fi "
    " WHERE fi.finding_id = findings.id) AS group_count"
)


def get_findings_light(run_id: Optional[str] = None, status: Optional[str] = None,
                       category: Optional[str] = None, rule_scope: Optional[str] = None,
                       industry: Optional[str] = None, is_anomaly: Optional[bool] = None,
                       client_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Same filters as get_findings(), without the heavy check_code/raw_result columns."""
    where, params = _finding_filters(run_id, status, category, rule_scope, industry, is_anomaly, client_id)
    query = f"SELECT {_LIGHT_COLUMNS} FROM findings WHERE 1=1{where} ORDER BY created_at DESC"

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
        results = [dict(r) for r in rows]
        for r in results:
            r["effective_sub_type"] = _effective_sub_type(r)
        return results


def get_finding(finding_id: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["effective_sub_type"] = _effective_sub_type(result)
        return result


def update_decision(finding_id: str, status: str, comment: str = "") -> bool:
    if status not in ("APPROVED", "REJECTED"):
        raise ValueError(f"Invalid status: {status}")
    with get_connection() as conn:
        cur = conn.execute(
            """UPDATE findings SET status = ?, reviewed_at = ?, reviewer_comment = ? WHERE id = ?""",
            (status, datetime.now(timezone.utc).isoformat(), comment, finding_id),
        )
        return cur.rowcount > 0


def get_runs(client_id: Optional[str] = None) -> List[Dict[str, Any]]:
    query, params = "SELECT * FROM runs", []
    if client_id:
        query += " WHERE client_id = ?"
        params.append(client_id)
    with get_connection() as conn:
        rows = conn.execute(query + " ORDER BY started_at DESC", params).fetchall()
        return [dict(r) for r in rows]


def get_stats(run_id: Optional[str] = None) -> Dict[str, Any]:
    with get_connection() as conn:
        # Status counts
        status_query = "SELECT status, COUNT(*) as cnt FROM findings WHERE 1=1"
        params: List[Any] = []
        if run_id:
            status_query += " AND run_id = ?"
            params.append(run_id)
        status_query += " GROUP BY status"
        rows = conn.execute(status_query, params).fetchall()
        stats = {"PENDING": 0, "APPROVED": 0, "REJECTED": 0}
        for r in rows:
            stats[r["status"]] = r["cnt"]
        stats["TOTAL"] = sum(v for k, v in stats.items())

        # Category counts
        cat_query = "SELECT category, COUNT(*) as cnt FROM findings WHERE 1=1"
        if run_id:
            cat_query += " AND run_id = ?"
        cat_query += " GROUP BY category"
        cat_rows = conn.execute(cat_query, params).fetchall()
        categories = {"ACTIVENESS": 0, "DUPLICATE": 0, "COMPLETENESS": 0, "CORRECTNESS": 0}
        for r in cat_rows:
            if r["category"] in categories:
                categories[r["category"]] = r["cnt"]
        stats["categories"] = categories

        # Anomaly count
        anom_query = "SELECT COUNT(*) as cnt FROM findings WHERE is_anomaly = 1"
        if run_id:
            anom_query += " AND run_id = ?"
        anom_row = conn.execute(anom_query, params).fetchone()
        stats["anomalies"] = anom_row["cnt"] if anom_row else 0

        # Rule scope counts
        scope_query = "SELECT rule_scope, COUNT(*) as cnt FROM findings WHERE 1=1"
        if run_id:
            scope_query += " AND run_id = ?"
        scope_query += " GROUP BY rule_scope"
        scope_rows = conn.execute(scope_query, params).fetchall()
        scopes = {"UNIVERSAL": 0, "INDUSTRY_SPECIFIC": 0, "CLIENT_SPECIFIC": 0}
        for r in scope_rows:
            if r["rule_scope"] in scopes:
                scopes[r["rule_scope"]] = r["cnt"]
        stats["rule_scopes"] = scopes

        return stats


def mark_promoted(finding_id: str) -> bool:
    with get_connection() as conn:
        cur = conn.execute(
            "UPDATE findings SET promoted_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), finding_id),
        )
        return cur.rowcount > 0


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
            verdict = item.get("review_verdict") or "PENDING"
            conn.execute(
                """INSERT INTO finding_items
                (id, finding_id, row_index, key_field, key_value, issue_detail,
                 corrected_data, status, created_at, reviewed_at, reviewer_comment,
                 duplicate_group_id, similarity_score, match_type, match_reasons,
                 is_golden_record, review_verdict, suggested_action, record_data, decision_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (item_id, finding_id, item.get("row_index"), item.get("key_field"),
                 str(item.get("key_value")), item.get("issue_detail"), item.get("corrected_data", ""),
                 # A pre-filled (remembered) verdict resolves the row like a human one would.
                 "PENDING" if verdict in _OPEN_VERDICTS else "APPROVED",
                 datetime.now(timezone.utc).isoformat(),
                 item.get("reviewed_at"),
                 item.get("reviewer_comment"),
                 item.get("duplicate_group_id"),
                 item.get("similarity_score"),
                 item.get("match_type"),
                 item.get("match_reasons"),
                 1 if item.get("is_golden_record") else 0,
                 verdict,
                 item.get("suggested_action", ""),
                 json.dumps(item["record_data"], default=str) if item.get("record_data") else None,
                 item.get("decision_source")),
            )
            ids.append(item_id)
    return ids


def get_finding_item(item_id: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM finding_items WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None


def get_finding_items(finding_id: str) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM finding_items WHERE finding_id = ? ORDER BY duplicate_group_id, is_golden_record DESC, row_index",
            (finding_id,)
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


def update_item_verdict(item_id: str, verdict: str, comment: str = "", corrected_data: str = "") -> bool:
    """Updates the pillar-appropriate review verdict on a finding item (see ITEM_DISPOSITIONS)."""
    if verdict not in ALL_VALID_VERDICTS:
        raise ValueError(f"Invalid verdict: {verdict}")

    status = "PENDING" if verdict in _OPEN_VERDICTS else "APPROVED"
    with get_connection() as conn:
        cur = conn.execute(
            """UPDATE finding_items SET review_verdict = ?, status = ?,
               corrected_data = CASE WHEN ? != '' THEN ? ELSE corrected_data END,
               reviewed_at = ?, reviewer_comment = ?, decision_source = 'HUMAN' WHERE id = ?""",
            (verdict, status, corrected_data, corrected_data,
             datetime.now(timezone.utc).isoformat(), comment, item_id),
        )
        return cur.rowcount > 0


def set_cluster_verdict(finding_id: str, group_id: str, verdict: str, comment: str = "") -> int:
    """Applies one verdict to every item in a duplicate cluster at once (e.g. cluster-level 'To Be Confirmed')."""
    if verdict not in ALL_VALID_VERDICTS:
        raise ValueError(f"Invalid verdict: {verdict}")

    status = "PENDING" if verdict in _OPEN_VERDICTS else "APPROVED"
    with get_connection() as conn:
        cur = conn.execute(
            """UPDATE finding_items SET review_verdict = ?, status = ?,
               reviewed_at = ?, reviewer_comment = ?, decision_source = 'HUMAN'
               WHERE finding_id = ? AND duplicate_group_id = ?""",
            (verdict, status, datetime.now(timezone.utc).isoformat(), comment, finding_id, group_id),
        )
        return cur.rowcount


def set_golden_record(finding_id: str, group_id: str, golden_item_id: str) -> bool:
    """Marks one record as Golden within a duplicate group, and marks siblings for merge.

    No longer called by review_app's API - the golden-record workflow was removed from the
    Duplicates UI (per-record/per-cluster verdicts replace it). Retained, along with the
    is_golden_record column, for backward compatibility with historical data: this codebase's
    SQLite migrations are additive-only (guarded ALTER TABLE, no drop/rebuild path), so old
    golden-record markings stay readable via get_duplicate_groups() rather than being discarded.
    """
    with get_connection() as conn:
        # 1. Fetch golden item key value
        golden_row = conn.execute(
            "SELECT key_value FROM finding_items WHERE id = ? AND finding_id = ?",
            (golden_item_id, finding_id),
        ).fetchone()
        if not golden_row:
            return False
        golden_key = golden_row["key_value"]

        # 2. Reset other records in the same group to non-golden
        conn.execute(
            """UPDATE finding_items
               SET is_golden_record = 0,
                   review_verdict = 'DUPLICATE',
                   status = 'APPROVED',
                   suggested_action = 'MERGE_INTO_GOLDEN (Target: ' || ? || ')',
                   reviewed_at = ?
               WHERE finding_id = ? AND duplicate_group_id = ? AND id != ?""",
            (golden_key, datetime.now(timezone.utc).isoformat(), finding_id, group_id, golden_item_id),
        )

        # 3. Mark target record as golden
        conn.execute(
            """UPDATE finding_items
               SET is_golden_record = 1,
                   review_verdict = 'DUPLICATE',
                   status = 'APPROVED',
                   suggested_action = 'RETAIN_AS_GOLDEN (Master Record)',
                   reviewed_at = ?
               WHERE id = ?""",
            (datetime.now(timezone.utc).isoformat(), golden_item_id),
        )
        return True


_MATCH_TYPE_RANK = {"EXACT": 3, "PROBABLE": 2, "SIMILAR": 1}


def get_duplicate_groups(finding_id: str) -> List[Dict[str, Any]]:
    """Returns clustered duplicate groups with their members, similarity scores, and golden record."""
    items = get_finding_items(finding_id)
    grouped: Dict[str, List[Dict[str, Any]]] = {}

    for item in items:
        try:
            item["record"] = json.loads(item["record_data"]) if item.get("record_data") else {}
        except (TypeError, ValueError):
            item["record"] = {}
        gid = item.get("duplicate_group_id") or "UNGROUPED"
        grouped.setdefault(gid, []).append(item)

    clusters = []
    for gid, members in grouped.items():
        max_score = max((m.get("similarity_score") or 0.0) for m in members)
        match_types = [m.get("match_type") for m in members if m.get("match_type")]
        primary_type = (max(match_types, key=lambda t: _MATCH_TYPE_RANK.get(t, 0)) if match_types
                        else ("EXACT" if max_score == 100.0 else "PROBABLE"))
        golden = next((m for m in members if m.get("is_golden_record") == 1), None)
        verdicts: Dict[str, int] = {}
        for m in members:
            verdict = m.get("review_verdict") or "PENDING"
            verdicts[verdict] = verdicts.get(verdict, 0) + 1

        clusters.append({
            "duplicate_group_id": gid,
            "similarity_score": max_score,
            "match_type": primary_type,
            "golden_record_id": golden["id"] if golden else None,
            "golden_record_key": golden["key_value"] if golden else None,
            "member_count": len(members),
            "verdicts": verdicts,
            "members": members,
        })

    clusters.sort(key=lambda c: (_MATCH_TYPE_RANK.get(c["match_type"], 0), c["similarity_score"]), reverse=True)
    return clusters


def apply_auto_fix(item_id: str, fix_value: str) -> bool:
    """Applies auto-fill value to an item and marks it reviewed/approved."""
    with get_connection() as conn:
        cur = conn.execute(
            """UPDATE finding_items
               SET corrected_data = ?, status = 'APPROVED',
                   reviewed_at = ?, reviewer_comment = 'Auto-fix default applied'
               WHERE id = ?""",
            (fix_value, datetime.now(timezone.utc).isoformat(), item_id),
        )
        return cur.rowcount > 0


def get_item_stats(finding_id: str) -> Dict[str, Any]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM finding_items WHERE finding_id = ? GROUP BY status",
            (finding_id,),
        ).fetchall()
        stats = {"PENDING": 0, "APPROVED": 0, "REJECTED": 0}
        for r in rows:
            stats[r["status"]] = r["cnt"]
        stats["TOTAL"] = sum(v for k, v in stats.items())

        verdict_rows = conn.execute(
            "SELECT review_verdict, COUNT(*) as cnt FROM finding_items WHERE finding_id = ? GROUP BY review_verdict",
            (finding_id,),
        ).fetchall()
        verdicts = {"PENDING": 0, "DUPLICATE": 0, "UNIQUE": 0, "TO_BE_CONFIRMED": 0}
        for r in verdict_rows:
            if r["review_verdict"] in verdicts:
                verdicts[r["review_verdict"]] = r["cnt"]
        stats["verdicts"] = verdicts

        return stats