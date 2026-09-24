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
    # Survivorship (explorer_agent/survivorship.py). is_golden_record now marks the
    # survivor - the UNIQUE record the group's duplicates merge into.
    for col, ddl in (("quality_score", "REAL"),          # 0-100 record quality score
                     ("score_breakdown", "TEXT"),        # JSON {component: 0..1}
                     ("recommended_verdict", "TEXT"),    # what was pre-selected: UNIQUE / DUPLICATE
                     ("reviewer", "TEXT"),               # who accepted (free text - no SSO yet)
                     ("undo_state", "TEXT")):            # JSON of the row before the last cluster action
        if col not in existing_item_cols:
            conn.execute(f"ALTER TABLE finding_items ADD COLUMN {col} {ddl}")


def _migrate_scorecards(conn: sqlite3.Connection) -> None:
    """Composite DQ scorecard per run (explorer_agent/scorecard.py): one row per table,
    migration object and the run total."""
    conn.execute("""
    CREATE TABLE IF NOT EXISTS scorecards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        client_id TEXT,
        scope TEXT NOT NULL,          -- table | object | run
        name TEXT NOT NULL,
        object_name TEXT,
        row_count INTEGER,
        completeness REAL, correctness REAL, uniqueness REAL, activeness REAL,
        dq_index REAL,
        details TEXT,                 -- JSON: pillars with bad/total and breakdowns, weights
        created_at TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scorecards_run ON scorecards(run_id)")
    existing = [row["name"] for row in conn.execute("PRAGMA table_info(scorecards)").fetchall()]
    for col, ddl in (("readiness", "REAL"), ("ready_records", "INTEGER"), ("in_scope_records", "INTEGER")):
        if col not in existing:
            conn.execute(f"ALTER TABLE scorecards ADD COLUMN {col} {ddl}")


def save_scorecard(run_id: str, client_id: Optional[str], entries: List[Dict[str, Any]],
                   weights: Dict[str, float]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        conn.execute("DELETE FROM scorecards WHERE run_id = ?", (run_id,))
        for e in entries:
            p = e["pillars"]
            score = lambda k: p[k]["score"] if p.get(k) else None  # noqa: E731
            conn.execute(
                """INSERT INTO scorecards (run_id, client_id, scope, name, object_name, row_count, completeness,
                   correctness, uniqueness, activeness, dq_index, details, created_at, readiness, ready_records,
                   in_scope_records)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, client_id, e["scope"], e["name"], e.get("object"), e["rows"], score("completeness"),
                 score("correctness"), score("uniqueness"), score("activeness"), e["dq_index"],
                 json.dumps({"pillars": p, "tables": e.get("tables"), "weights": weights,
                             "readiness": e.get("readiness")}, default=str), now,
                 (e.get("readiness") or {}).get("score"), (e.get("readiness") or {}).get("ready"),
                 (e.get("readiness") or {}).get("in_scope")))


def get_scorecard(run_id: str) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM scorecards WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
    out = []
    for r in rows:
        entry = dict(r)
        entry["details"] = json.loads(entry["details"]) if entry.get("details") else {}
        out.append(entry)
    return out


def scorecard_runs(client_id: str) -> List[str]:
    """Runs of this client that have a scorecard, newest first."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT s.run_id FROM scorecards s JOIN runs r ON r.run_id = s.run_id
               WHERE r.client_id = ? GROUP BY s.run_id ORDER BY MAX(r.started_at) DESC""", (client_id,)).fetchall()
    return [r["run_id"] for r in rows]


def _migrate_runs(conn: sqlite3.Connection) -> None:
    existing_cols = [row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()]
    if "client_id" not in existing_cols:
        # Client (company) the data belongs to - see explorer_agent/client_knowledge.py.
        conn.execute("ALTER TABLE runs ADD COLUMN client_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_client ON runs(client_id)")
    if "client_name" not in existing_cols:
        conn.execute("ALTER TABLE runs ADD COLUMN client_name TEXT")


def _migrate_llm_calls(conn: sqlite3.Connection) -> None:
    """One row per LLM request (explorer_agent/llm_usage.py): counts and timings only, never prompts."""
    conn.execute("""
    CREATE TABLE IF NOT EXISTS llm_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        table_name TEXT,              -- NULL for client-wide calls (column mapping)
        role TEXT NOT NULL,           -- planner | reflector | duplicate_rules | column_mapping
        model TEXT,
        input_tokens INTEGER,
        output_tokens INTEGER,
        reasoning_tokens INTEGER,     -- thinking tokens when reported (already part of the output bill)
        total_tokens INTEGER,
        seconds REAL,
        success INTEGER NOT NULL DEFAULT 1,
        error TEXT,
        created_at TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_run ON llm_calls(run_id)")


def _migrate_explanations(conn: sqlite3.Connection) -> None:
    """Plain-language explanations written by the local model, one per flagged record (explain.py). Derived
    text that may quote record values: it stays on this machine like finding_items."""
    conn.execute("""
    CREATE TABLE IF NOT EXISTS explanations (
        item_id TEXT PRIMARY KEY,
        text TEXT NOT NULL,
        model TEXT,
        input_tokens INTEGER,
        output_tokens INTEGER,
        created_at TEXT NOT NULL
    )""")


def get_finding_item(item_id: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM finding_items WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None


def get_explanation(item_id: str) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM explanations WHERE item_id = ?", (item_id,)).fetchone()
        return dict(row) if row else None


def save_explanation(item_id: str, text: str, model: str, input_tokens: Optional[int],
                     output_tokens: Optional[int]) -> None:
    with get_connection() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO explanations (item_id, text, model, input_tokens, output_tokens, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (item_id, text, model, input_tokens, output_tokens, datetime.now(timezone.utc).isoformat()))


def save_llm_call(run_id: str, record: Dict[str, Any]) -> None:
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO llm_calls (run_id, table_name, role, model, input_tokens, output_tokens,
               reasoning_tokens, total_tokens, seconds, success, error, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, record.get("table_name"), record["role"], record.get("model"), record.get("input_tokens"),
             record.get("output_tokens"), record.get("reasoning_tokens"), record.get("total_tokens"),
             record.get("seconds"), 1 if record.get("success", True) else 0, record.get("error"),
             datetime.now(timezone.utc).isoformat()))


def _usage_totals(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    ok = [r for r in rows if r["success"]]
    total = lambda key: sum(r[key] or 0 for r in ok)  # noqa: E731
    return {"calls": len(ok), "failed_calls": len(rows) - len(ok), "input_tokens": total("input_tokens"),
            "output_tokens": total("output_tokens"), "reasoning_tokens": total("reasoning_tokens"),
            "total_tokens": total("total_tokens"), "seconds": round(sum(r["seconds"] or 0 for r in rows), 1)}


def get_run_llm_usage(run_id: str) -> Dict[str, Any]:
    """Every LLM request of a run, plus totals overall and per role."""
    with get_connection() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM llm_calls WHERE run_id = ? ORDER BY id", (run_id,))]
    by_role = {role: _usage_totals([r for r in rows if r["role"] == role]) for role in sorted({r["role"] for r in rows})}
    return {"run_id": run_id, "totals": _usage_totals(rows), "by_role": by_role, "calls": rows}


def get_llm_usage_by_run() -> Dict[str, Dict[str, Any]]:
    """Token totals of every run that has any (for the run list)."""
    with get_connection() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM llm_calls")]
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        grouped.setdefault(r["run_id"], []).append(r)
    return {run_id: _usage_totals(items) for run_id, items in grouped.items()}


def init_db():
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        _migrate_finding_items(conn)
        _migrate_runs(conn)
        _migrate_scorecards(conn)
        _migrate_llm_calls(conn)
        _migrate_explanations(conn)
        _backfill_finding_status(conn)


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


# Where a finding came from, derived from what was stored (no extra column): the duplicate engine, a built-in
# rule (its check_code starts with "# Built-in SAP rule", see rule_context.finding), or a check the LLM proposed
# (also when it ran from a promoted skill).
_SOURCE_SQL = ("(CASE WHEN category = 'DUPLICATE' THEN 'DUPLICATE_ENGINE' "
               "WHEN check_code LIKE '# Built-in SAP rule%' THEN 'BUILT_IN' ELSE 'LLM' END)")
SOURCES = ("BUILT_IN", "LLM", "DUPLICATE_ENGINE")


def _finding_filters(run_id: Optional[str] = None, status: Optional[str] = None,
                     category: Optional[str] = None, rule_scope: Optional[str] = None,
                     industry: Optional[str] = None, is_anomaly: Optional[bool] = None,
                     client_id: Optional[str] = None, source: Optional[str] = None) -> Tuple[str, List[Any]]:
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
    if source:
        clauses.append(f"{_SOURCE_SQL} = ?")
        params.append(source)
    return "".join(f" AND {c}" for c in clauses), params


def get_findings(run_id: Optional[str] = None, status: Optional[str] = None,
                 category: Optional[str] = None, rule_scope: Optional[str] = None,
                 industry: Optional[str] = None, is_anomaly: Optional[bool] = None,
                 client_id: Optional[str] = None, source: Optional[str] = None) -> List[Dict[str, Any]]:
    where, params = _finding_filters(run_id, status, category, rule_scope, industry, is_anomaly, client_id, source)
    query = f"SELECT *, {_SOURCE_SQL} AS source FROM findings WHERE 1=1{where} ORDER BY created_at DESC"

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
    f"confidence, reusable, {_SOURCE_SQL} AS source, (CASE WHEN check_code IS NOT NULL AND check_code != '' THEN 1 ELSE 0 END) AS has_check_code, "
    "created_at, status, reviewed_at, reviewer_comment, promoted_at, "
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
                       client_id: Optional[str] = None, source: Optional[str] = None) -> List[Dict[str, Any]]:
    """Same filters as get_findings(), without the heavy check_code/raw_result columns."""
    where, params = _finding_filters(run_id, status, category, rule_scope, industry, is_anomaly, client_id, source)
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
        runs = [dict(r) for r in rows]
    usage = get_llm_usage_by_run()
    for run in runs:
        run["llm_usage"] = usage.get(run["run_id"])
    return runs


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
                 is_golden_record, review_verdict, suggested_action, record_data, decision_source,
                 quality_score, score_breakdown, recommended_verdict, reviewer)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                 item.get("decision_source"),
                 item.get("quality_score"), item.get("score_breakdown"), item.get("recommended_verdict"),
                 item.get("reviewer")),
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


# A finding that can never become a skill (built-in rules, duplicates) has no decision of its own:
# its status follows its records. It is marked reviewed once every record is decided, and goes back to
# pending if a decision is undone or reopened. A reusable LLM check keeps a manual Approve/Reject,
# because that status is the human gate before promotion (get_promotable_findings).
ROLLUP_NOTE = "All records reviewed"


def _sync_finding_status(conn: sqlite3.Connection, finding_id: str) -> None:
    f = conn.execute("SELECT status, reusable, check_code, reviewer_comment FROM findings WHERE id = ?",
                     (finding_id,)).fetchone()
    if not f or (f["reusable"] and f["check_code"]):
        return  # promotable: the reviewer's own decision, never rolled up
    total, pending = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(CASE WHEN status = 'PENDING' THEN 1 ELSE 0 END), 0) "
        "FROM finding_items WHERE finding_id = ?", (finding_id,)).fetchone()
    if not total:
        return  # no records to roll up from: the finding keeps its manual decision
    if pending == 0 and f["status"] == "PENDING":
        conn.execute("UPDATE findings SET status = 'APPROVED', reviewed_at = ?, reviewer_comment = ? WHERE id = ?",
                     (datetime.now(timezone.utc).isoformat(), ROLLUP_NOTE, finding_id))
    elif pending > 0 and f["status"] == "APPROVED" and f["reviewer_comment"] == ROLLUP_NOTE:
        conn.execute("UPDATE findings SET status = 'PENDING', reviewed_at = NULL, reviewer_comment = NULL "
                     "WHERE id = ?", (finding_id,))  # only undoes a roll-up, never a manual decision


def _sync_for_item(conn: sqlite3.Connection, item_id: str) -> None:
    row = conn.execute("SELECT finding_id FROM finding_items WHERE id = ?", (item_id,)).fetchone()
    if row:
        _sync_finding_status(conn, row["finding_id"])


def _backfill_finding_status(conn: sqlite3.Connection) -> None:
    """Bring findings decided before roll-up existed in line (idempotent, cheap)."""
    for row in conn.execute("SELECT DISTINCT finding_id FROM finding_items").fetchall():
        _sync_finding_status(conn, row["finding_id"])


def update_item_decision(item_id: str, status: str, corrected_data: str = "", comment: str = "") -> bool:
    if status not in ("APPROVED", "REJECTED"):
        raise ValueError(f"Invalid status: {status}")
    with get_connection() as conn:
        cur = conn.execute(
            """UPDATE finding_items SET status = ?, corrected_data = ?,
               reviewed_at = ?, reviewer_comment = ? WHERE id = ?""",
            (status, corrected_data, datetime.now(timezone.utc).isoformat(), comment, item_id),
        )
        _sync_for_item(conn, item_id)
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
        _sync_for_item(conn, item_id)
        return cur.rowcount > 0


_UNDO_FIELDS = ("review_verdict", "status", "is_golden_record", "suggested_action", "decision_source",
                "reviewed_at", "reviewer_comment", "reviewer")


def _snapshot_cluster(conn, finding_id: str, group_id: str) -> None:
    """Keep each row's review state before a cluster action, for undo_cluster (one level)."""
    rows = conn.execute(f"SELECT id, {', '.join(_UNDO_FIELDS)} FROM finding_items "
                        "WHERE finding_id = ? AND duplicate_group_id = ?", (finding_id, group_id)).fetchall()
    for r in rows:
        conn.execute("UPDATE finding_items SET undo_state = ? WHERE id = ?",
                     (json.dumps({f: r[f] for f in _UNDO_FIELDS}), r["id"]))


def undo_cluster(finding_id: str, group_id: str) -> int:
    """Put a duplicate cluster back the way it was before its last Accept / To be confirmed.
    Rows without a snapshot (decided before undo existed) go back to PENDING."""
    with get_connection() as conn:
        rows = conn.execute("SELECT id, undo_state, recommended_verdict FROM finding_items "
                            "WHERE finding_id = ? AND duplicate_group_id = ?", (finding_id, group_id)).fetchall()
        for r in rows:
            if r["undo_state"]:
                before = json.loads(r["undo_state"])
            else:
                before = {"review_verdict": "PENDING", "status": "PENDING",
                          "is_golden_record": 1 if r["recommended_verdict"] == "UNIQUE" else 0,
                          "decision_source": None, "reviewed_at": None, "reviewer_comment": None, "reviewer": None}
            fields = [f for f in _UNDO_FIELDS if f in before]
            conn.execute(f"UPDATE finding_items SET {', '.join(f'{f} = ?' for f in fields)}, undo_state = NULL "
                         "WHERE id = ?", [before[f] for f in fields] + [r["id"]])
        _sync_finding_status(conn, finding_id)
        return len(rows)


def accept_cluster(finding_id: str, group_id: str, survivor_id: Optional[str], separate_ids: List[str],
                   reviewer: str = "", comment: str = "") -> int:
    """Accept a duplicate group's decision: the survivor is UNIQUE (is_golden_record=1),
    records in ``separate_ids`` are UNIQUE separate entities (look-alikes), every other
    record is a DUPLICATE of the survivor. With no survivor, every record must be in
    ``separate_ids`` ("none of these are duplicates"). Returns rows updated (0 = unknown group)."""
    now = datetime.now(timezone.utc).isoformat()
    with get_connection() as conn:
        rows = conn.execute("SELECT id, key_value, suggested_action FROM finding_items "
                            "WHERE finding_id = ? AND duplicate_group_id = ?", (finding_id, group_id)).fetchall()
        ids = {r["id"] for r in rows}
        if not rows or (survivor_id and survivor_id not in ids) or not set(separate_ids) <= ids:
            return 0
        if not survivor_id and set(separate_ids) != ids:
            raise ValueError("Choose a survivor, or mark every record Unique")
        survivor_key = next((r["key_value"] for r in rows if r["id"] == survivor_id), None)
        _snapshot_cluster(conn, finding_id, group_id)
        for r in rows:
            if r["id"] == survivor_id:
                verdict, golden, action = "UNIQUE", 1, "GOLDEN_RECORD"
            elif r["id"] in separate_ids:
                verdict, golden, action = "UNIQUE", 0, "SEPARATE_ENTITY"
            else:
                verdict, golden = "DUPLICATE", 0
                proposed = r["suggested_action"] or ""
                # Keep the recommended kind of action, re-targeted at the chosen survivor.
                action = (f"BLOCK_AND_DELETE (duplicate of {survivor_key})" if proposed.startswith("BLOCK")
                          else f"MERGE_INTO {survivor_key}")
            conn.execute(
                """UPDATE finding_items SET review_verdict = ?, status = 'APPROVED', is_golden_record = ?,
                   suggested_action = ?, reviewed_at = ?, reviewer_comment = ?, reviewer = ?,
                   decision_source = 'HUMAN' WHERE id = ?""",
                (verdict, golden, action, now, comment, reviewer or None, r["id"]))
        _sync_finding_status(conn, finding_id)
        return len(rows)


def set_cluster_verdict(finding_id: str, group_id: str, verdict: str, comment: str = "", reviewer: str = "") -> int:
    """Applies one verdict to every item in a duplicate cluster at once (e.g. cluster-level 'To Be Confirmed')."""
    if verdict not in ALL_VALID_VERDICTS:
        raise ValueError(f"Invalid verdict: {verdict}")

    status = "PENDING" if verdict in _OPEN_VERDICTS else "APPROVED"
    with get_connection() as conn:
        _snapshot_cluster(conn, finding_id, group_id)
        cur = conn.execute(
            """UPDATE finding_items SET review_verdict = ?, status = ?,
               reviewed_at = ?, reviewer_comment = ?, reviewer = COALESCE(NULLIF(?, ''), reviewer),
               decision_source = 'HUMAN' WHERE finding_id = ? AND duplicate_group_id = ?""",
            (verdict, status, datetime.now(timezone.utc).isoformat(), comment, reviewer, finding_id, group_id),
        )
        _sync_finding_status(conn, finding_id)
        return cur.rowcount


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
        recommended = next((m for m in members if m.get("recommended_verdict") == "UNIQUE"), None)
        decided = all((m.get("review_verdict") or "PENDING") in ("UNIQUE", "DUPLICATE") for m in members)
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
            # Survivorship: the pre-selected survivor, and - once decided - whether the
            # reviewer kept it (the online quality signal for the scoring).
            "recommended_survivor_id": recommended["id"] if recommended else None,
            "accepted_as_recommended": (bool(golden and recommended and golden["id"] == recommended["id"])
                                        if decided and recommended else None),
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
        _sync_for_item(conn, item_id)
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