"""Composite Data Quality scorecard (Priority 5) - per table, migration object and run.

Scores are computed DURING the run from the table itself, its column mapping and
the deterministic engines' results - never from stored findings, whose row lists
are capped and which carry no denominators. LLM-proposed checks are left out on
purpose: they differ from run to run and are unreviewed, and an executive index
must be reproducible.

Pillars (each 0..1, None when the table gives it nothing to measure):

* completeness = filled mandatory cells / mandatory cells. A group of
                 alternatives (BANKN or IBAN) is one cell per row; all tax fields
                 together are one cell per row.
* correctness  = 1 - defective cells / checked cells, over the columns a
                 correctness rule actually checked (Priority 1/2). A composite
                 check (tax fields, a composite key) counts one cell per row.
* uniqueness   = 1 - redundant records / records. Redundant = group members
                 beyond the one that survives, from EXACT/PROBABLE groups, or
                 the reviewer's DUPLICATE verdicts once a group is decided -
                 including groups settled in earlier runs and no longer shown.
                 SIMILAR groups (look-alikes) are only reported as "possible".
* activeness   = records neither marked for deletion nor dormant / records.
                 Blocked records are reported, not deducted. Shown, but weighted
                 0 in the index by default: a deleted or dormant vendor is a
                 migration-scope fact, not bad data.

DQ Index = weighted average of the pillars that have a score
(``scorecard.weights``). Objects and the run aggregate their tables by summing
numerators and denominators, so large tables weigh more.

RECORD READINESS - the headline number on the dashboard: the share of in-scope
records that could be loaded as they are, with no open defect at all. A record is
not ready when any deterministic completeness/correctness check flags it, or when
it is a duplicate that will be merged away (the reviewer's DUPLICATE verdict, or
the recommendation in an EXACT/PROBABLE group - the golden record stays ready;
SIMILAR look-alikes are not counted). Records marked for deletion or dormant are
OUT OF SCOPE (they are not migrated), so they count neither way. Each not-ready
record keeps its reasons - the cleansing worklist (``/api/scorecard/not-ready``).
A cell-based index sits near 100% because most cells are fine; readiness says how
many records still need work, which is what a go/no-go decision needs.
"""

from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from .config import Config
from .logging_config import get_logger
from .rule_context import text

logger = get_logger("scorecard")

PILLARS = ("completeness", "correctness", "uniqueness", "activeness")


def _pillar(bad: int, total: int, **detail) -> Optional[Dict[str, Any]]:
    if total <= 0:
        return None
    bad = min(bad, total)
    return {"score": round(1 - bad / total, 4), "bad": int(bad), "total": int(total), **detail}


def _upper_map(df: pd.DataFrame) -> Dict[str, str]:
    return {str(c).upper(): c for c in df.columns}


def _completeness(df, columns, coverage, findings) -> Optional[Dict[str, Any]]:
    covered = {c for c, cat, _ in coverage.pairs if cat == "COMPLETENESS"}
    groups = {}
    for col, b in columns.items():
        if b["required"] and b["concept"] != "TAX_ID" and str(col).upper() in covered:
            groups.setdefault(b["required_group"] or f"__{col}", []).append(col)
    n_groups = len(groups) + (1 if any(b["concept"] == "TAX_ID" and b["required"] and str(c).upper() in covered
                                       for c, b in columns.items()) else 0)
    missing = sum(f["raw_tool_result"]["rows_flagged"] for f in findings if f["category"] == "COMPLETENESS")
    return _pillar(missing, len(df) * n_groups, fields=n_groups)


def _correctness(df, coverage, findings) -> Optional[Dict[str, Any]]:
    upper = _upper_map(df)
    checked = [upper[c] for c in {c for c, cat, _ in coverage.pairs if cat == "CORRECTNESS"} if c in upper]
    if not checked:
        return None
    total = sum(int((text(df[c]) != "").sum()) for c in checked)
    cells, extra = set(), 0
    checked_upper = {str(c).upper() for c in checked}
    for f in findings:
        if f["category"] != "CORRECTNESS":
            continue
        parts = [p.strip() for p in str(f["column"]).replace("+", " ").split() if p.strip()]
        unit = next((p for p in parts if p.upper() in checked_upper), parts[0] if parts else f["column"])
        rows = f.get("detail_rows", [])
        cells.update((r["row_index"], unit) for r in rows)
        extra += max(0, f["raw_tool_result"]["rows_flagged"] - len(rows))  # rows beyond the stored cap
    return _pillar(len(cells) + extra, total, columns=len(checked))


def _uniqueness(df, duplicate_finding, stats) -> Optional[Dict[str, Any]]:
    if not Config.DUPLICATES_ENABLED:
        return None
    redundant = int((stats or {}).get("settled_duplicates", 0))
    possible = 0
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in (duplicate_finding or {}).get("detail_rows", []):
        groups.setdefault(row["duplicate_group_id"], []).append(row)
    rank = {"EXACT": 3, "PROBABLE": 2, "SIMILAR": 1}
    for members in groups.values():
        verdicts = Counter(m.get("review_verdict") or "PENDING" for m in members)
        if verdicts["DUPLICATE"] or verdicts["UNIQUE"]:
            redundant += verdicts["DUPLICATE"]          # the reviewer's call wins
        elif max(rank.get(m.get("match_type"), 0) for m in members) >= 2:
            redundant += len(members) - 1               # all but the survivor
        else:
            possible += len(members) - 1                # SIMILAR: unconfirmed look-alikes
    return _pillar(redundant, len(df), possible_duplicates=possible,
                   decided_in_earlier_runs=int((stats or {}).get("settled_duplicates", 0)))


def _activeness(df, columns, coverage, findings) -> Optional[Dict[str, Any]]:
    deletion = [c for c, b in columns.items() if b["concept"] == "DELETION_FLAG" and c in df.columns]
    blocks = [c for c, b in columns.items() if b["concept"] == "BLOCK_FLAG" and c in df.columns]
    dormant_ran = any(f["raw_tool_result"]["rule_id"].startswith("dormant.") for f in findings) or \
        any(cat == "ACTIVENESS" and c in {str(x).upper() for x, b in columns.items() if b["concept"] == "CREATED_DATE"}
            for c, cat, _ in coverage.pairs)
    if not deletion and not dormant_ran:
        return None

    def flagged(cols):
        idx = set()
        for c in cols:
            values = {str(v).upper() for v in (columns[c].get("flag_set_values") or ["X"])}
            idx |= set(df.index[text(df[c]).str.upper().isin(values)])
        return idx

    deleted = flagged(deletion)
    dormant = {r["row_index"] for f in findings if f["raw_tool_result"]["rule_id"].startswith("dormant.")
               for r in f.get("detail_rows", [])}
    inactive = deleted | dormant
    blocked = flagged(blocks) - inactive
    return _pillar(len(inactive), len(df), marked_for_deletion=len(deleted), dormant=len(dormant - deleted),
                   blocked=len(blocked))


def _flag_rows(df, columns, concept) -> set:
    rows = set()
    for c, b in columns.items():
        if b["concept"] == concept and c in df.columns:
            values = {str(v).upper() for v in (b.get("flag_set_values") or ["X"])}
            rows |= set(df.index[text(df[c]).str.upper().isin(values)])
    return rows


def _title(finding: Dict[str, Any]) -> str:
    return str(finding.get("summary", "")).split(": ")[0] or finding["raw_tool_result"]["rule_id"]


def _readiness(df, columns, findings, duplicate_finding, duplicate_stats) -> Dict[str, Any]:
    keys = [c for c, b in columns.items() if b["part_of_key"] and c in df.columns] or [df.columns[0]]
    dormant = {r["row_index"] for f in findings if f["raw_tool_result"]["rule_id"].startswith("dormant.")
               for r in f.get("detail_rows", [])}
    out_of_scope = _flag_rows(df, columns, "DELETION_FLAG") | dormant

    reasons: Dict[Any, List[str]] = {}
    unlisted = 0
    for f in findings:
        if f["category"] not in ("COMPLETENESS", "CORRECTNESS"):
            continue
        rows = f.get("detail_rows", [])
        for r in rows:
            reasons.setdefault(r["row_index"], []).append(_title(f))
        unlisted += max(0, f["raw_tool_result"]["rows_flagged"] - len(rows))   # beyond the stored cap

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in (duplicate_finding or {}).get("detail_rows", []):
        groups.setdefault(row["duplicate_group_id"], []).append(row)
    rank = {"EXACT": 3, "PROBABLE": 2, "SIMILAR": 1}
    for members in groups.values():
        golden = next((m for m in members if m.get("is_golden_record")), None)
        strong = max(rank.get(m.get("match_type"), 0) for m in members) >= 2
        for m in members:
            verdict = m.get("review_verdict") or "PENDING"
            if verdict == "DUPLICATE" or (verdict in ("PENDING", "TO_BE_CONFIRMED") and strong
                                          and m.get("recommended_verdict") == "DUPLICATE"):
                target = golden["key_value"] if golden else "its golden record"
                reasons.setdefault(m["row_index"], []).append(f"Duplicate of {target} (merged away)")
    for r in (duplicate_stats or {}).get("settled_duplicate_rows", []):
        reasons.setdefault(r["row_index"], []).append(
            f"Duplicate of {r.get('merge_into') or 'its golden record'} (decided in an earlier review)")

    in_scope = len(df) - len(out_of_scope)
    listed = {i: rs for i, rs in reasons.items() if i not in out_of_scope and i in df.index}
    not_ready = min(len(listed) + unlisted, in_scope)
    top = Counter(t for rs in listed.values() for t in set(rs)).most_common(8)
    worklist = [{"row_index": int(i), "key": " / ".join(str(df.at[i, k]) for k in keys), "reasons": sorted(set(rs))}
                for i, rs in sorted(listed.items(), key=lambda kv: -len(kv[1]))][:Config.READINESS_MAX_WORKLIST]
    return {"records": len(df), "in_scope": in_scope, "out_of_scope": len(out_of_scope),
            "ready": in_scope - not_ready, "not_ready": not_ready, "unlisted": unlisted,
            "score": round((in_scope - not_ready) / in_scope, 4) if in_scope else None,
            "top_reasons": [{"reason": r, "records": n} for r, n in top], "worklist": worklist}


def _index(pillars: Dict[str, Optional[Dict[str, Any]]]) -> Optional[float]:
    weights = {p: w for p, w in Config.SCORECARD_WEIGHTS.items() if w > 0 and pillars.get(p)}
    if not weights:
        return None
    return round(sum(pillars[p]["score"] * w for p, w in weights.items()) / sum(weights.values()), 4)


def migration_object(table: str, mapping: Optional[Dict[str, Any]], pack: Dict[str, Any],
                     source_file: Optional[str]) -> str:
    """Vendor / Customer / Material ... from the SAP tables the table maps to (rule pack
    `objects`), else the upload sub-folder (vendor-master/LFA1.csv), else 'Other'."""
    objects = {t: name for name, tables in (pack.get("objects") or {}).items() for t in tables}
    sap_tables = Counter()
    source = (mapping or {}).get("source", "")
    if source.startswith("sap-standard") or table in objects:
        sap_tables[table] += 1
    for b in (mapping or {}).get("columns", {}).values():
        for t in b.get("targets") or ([b["target"]] if b.get("target") else []):
            sap_tables[t.split(".")[0]] += 1
    for sap_table, _ in sap_tables.most_common():
        if sap_table in objects:
            return objects[sap_table]
    parent = Path(source_file or "").parent.name
    return parent.replace("-", " ").title() if parent else "Other"


def score_table(table: str, df: pd.DataFrame, mapping: Optional[Dict[str, Any]], coverage, rule_findings,
                duplicate_finding, duplicate_stats, obj: str) -> Dict[str, Any]:
    columns = (mapping or {}).get("columns", {})
    findings = [f for f in rule_findings if (f.get("raw_tool_result") or {}).get("rule_id")]
    pillars = {
        "completeness": _completeness(df, columns, coverage, findings),
        "correctness": _correctness(df, coverage, findings),
        "uniqueness": _uniqueness(df, duplicate_finding, duplicate_stats),
        "activeness": _activeness(df, columns, coverage, findings),
    }
    return {"scope": "table", "name": table, "object": obj, "rows": len(df), "pillars": pillars,
            "dq_index": _index(pillars),
            "readiness": _readiness(df, columns, findings, duplicate_finding, duplicate_stats)}


def aggregate(name: str, scope: str, entries: List[Dict[str, Any]], obj: Optional[str] = None) -> Dict[str, Any]:
    pillars: Dict[str, Optional[Dict[str, Any]]] = {}
    for p in PILLARS:
        parts = [e["pillars"][p] for e in entries if e["pillars"].get(p)]
        pillars[p] = _pillar(sum(x["bad"] for x in parts), sum(x["total"] for x in parts)) if parts else None
    ready = {k: sum(e["readiness"][k] for e in entries) for k in ("records", "in_scope", "out_of_scope",
                                                                     "ready", "not_ready")}
    ready["score"] = round(ready["ready"] / ready["in_scope"], 4) if ready["in_scope"] else None
    top = Counter()
    for e in entries:
        for r in e["readiness"]["top_reasons"]:
            top[r["reason"]] += r["records"]
    ready["top_reasons"] = [{"reason": r, "records": n} for r, n in top.most_common(8)]
    return {"scope": scope, "name": name, "object": obj, "rows": sum(e["rows"] for e in entries),
            "tables": len(entries), "pillars": pillars, "dq_index": _index(pillars), "readiness": ready}


def build(table_scores: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Table entries + one entry per migration object + the run total."""
    by_object: Dict[str, List[Dict[str, Any]]] = {}
    for e in table_scores:
        by_object.setdefault(e["object"], []).append(e)
    objects = [aggregate(name, "object", entries, name) for name, entries in sorted(by_object.items())]
    overall = aggregate("All tables", "run", table_scores)
    for e in objects + [overall]:
        r = e["readiness"]
        logger.info("Readiness %s %-22s %s ready (%d of %d in scope, %d out of scope)", e["scope"], e["name"],
                    "n/a" if r["score"] is None else f"{r['score']:.1%}", r["ready"], r["in_scope"], r["out_of_scope"])
        logger.info("DQ %s %-22s index=%s  %s", e["scope"], e["name"],
                    "n/a" if e["dq_index"] is None else f"{e['dq_index']:.1%}",
                    "  ".join(f"{p}={'n/a' if not e['pillars'][p] else format(e['pillars'][p]['score'], '.1%')}"
                              for p in PILLARS))
    return table_scores + objects + [overall]
