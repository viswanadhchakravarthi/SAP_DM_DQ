"""Record quality score and recommended survivor per duplicate group - no LLM.

For every member of a duplicate group (``duplicate_detector``) this computes a
0-100 quality score from four components, each 0..1, weighted by
``survivorship.weights``:

* completeness - share of the record's important fields (by the column mapping:
                 required, TAX_ID, contact, address, name) that are filled AND
                 not flagged by a Priority 1/2 rule on that row. A 'TBD' tax
                 number or an 'n/a' e-mail is not completeness.
* active       - 1 without flags, 0.5 when blocked, 0 when marked for deletion.
* usage        - share of the org-level child tables (a table referencing this
                 one with an ORG_UNIT key: company code, purchasing org, ...) the
                 record is extended to. In a migration the record the business
                 actually uses - open items and orders point at it - survives.
* recency      - creation date ranked within the group (newest 1, oldest 0);
                 an absolute date says nothing, only the order inside the group.

A component that cannot be computed for the table is left out and the others
re-weighted. The highest score is the recommended golden record - the survivor
(review verdict UNIQUE); the others are recommended DUPLICATE with an action: MERGE_INTO the
survivor when they have org-level data to move over, else BLOCK_AND_DELETE.
A survivor remembered from an earlier review wins over the score.

Only EXACT/PROBABLE groups get a recommendation (``survivorship.recommend_for``);
SIMILAR groups contain look-alikes, so their members are scored but nothing is
pre-selected. This is a SUGGESTION: rows keep review_verdict PENDING until a
reviewer accepts (review_app ``/accept``) - the human gate stays closed.
"""

import json
from typing import Any, Dict, List, Optional

import pandas as pd

from .config import Config
from .logging_config import get_logger
from .rule_context import text, upper

logger = get_logger("survivorship")

_IMPORTANT = ("KEY", "LEGAL_NAME", "COUNTRY", "POSTAL_CODE", "CITY", "STREET", "TAX_ID", "EMAIL", "PHONE",
              "SEARCH_TERM", "CURRENCY", "ORG_UNIT")
_MATCH_RANK = {"EXACT": 3, "PROBABLE": 2, "SIMILAR": 1}


def _set_values(binding: Dict[str, Any]) -> set:
    return {str(v).upper() for v in (binding.get("flag_set_values") or ["X"])}


def _flagged_cells(rule_findings: List[Dict[str, Any]]) -> Dict[int, set]:
    """{row_index: columns a Priority 1/2 rule flagged on that row} (completeness/correctness only)."""
    flagged: Dict[int, set] = {}
    for f in rule_findings:
        if f.get("category") not in ("COMPLETENESS", "CORRECTNESS"):
            continue
        cols = {c.strip() for c in str(f.get("column", "")).replace("+", " ").split()}
        for row in f.get("detail_rows", []):
            flagged.setdefault(row["row_index"], set()).update(cols)
    return flagged


def _children(table: str, own_key: Optional[str], mappings: Dict[str, Dict[str, Any]],
              tables: Dict[str, pd.DataFrame]) -> List[set]:
    """Key sets of the org-level child tables of `table` that are loaded."""
    if not own_key:
        return []
    out = []
    for name, m in mappings.items():
        if name == table or name not in tables:
            continue
        cols = m["columns"]
        refs = [c for c, b in cols.items() if b["references"] == f"{table}.{own_key}"]
        if refs and any(b["concept"] == "ORG_UNIT" and b["part_of_key"] for b in cols.values()):
            out.append(set(text(tables[name][refs[0]])))
    return out


def annotate(finding: Dict[str, Any], table: str, df: pd.DataFrame, mappings: Dict[str, Dict[str, Any]],
             tables: Dict[str, pd.DataFrame], rule_findings: List[Dict[str, Any]]) -> None:
    """Add quality_score / score_breakdown / recommended_verdict / suggested_action to the
    duplicate finding's rows in place. Leaves review_verdict untouched."""
    if not Config.SURVIVORSHIP_ENABLED or not finding or not finding.get("detail_rows"):
        return
    columns = (mappings.get(table) or {}).get("columns", {})
    important = [c for c, b in columns.items() if c in df.columns and (b["concept"] in _IMPORTANT or b["required"])]
    deletion = [c for c, b in columns.items() if b["concept"] == "DELETION_FLAG" and c in df.columns]
    blocks = [c for c, b in columns.items() if b["concept"] == "BLOCK_FLAG" and c in df.columns]
    created = next((c for c, b in columns.items() if b["concept"] == "CREATED_DATE" and c in df.columns), None)
    own_key = next((c for c, b in columns.items()
                    if b["concept"] == "KEY" and b["part_of_key"] and not b["references"]), None)
    children = _children(table, own_key, mappings, tables)
    flagged = _flagged_cells(rule_findings)
    dates = pd.to_datetime(text(df[created]), errors="coerce", format="mixed") if created else None

    weights = dict(Config.SURVIVORSHIP_WEIGHTS)
    if not important:
        weights.pop("completeness", None)
    if not (deletion or blocks):
        weights.pop("active", None)
    if not children:
        weights.pop("usage", None)
    if dates is None:
        weights.pop("recency", None)
    if not weights:
        logger.info("[%s] survivorship skipped - no mapped columns to score on (e.g. a --duplicates-only run)", table)
        return
    total_weight = sum(weights.values())

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for row in finding["detail_rows"]:
        groups.setdefault(row["duplicate_group_id"], []).append(row)

    recommended = 0
    for gid, members in groups.items():
        # Recency: rank of the creation date inside this group.
        ranks = {}
        if dates is not None:
            known = sorted({dates[m["row_index"]] for m in members if pd.notna(dates.get(m["row_index"]))})
            for m in members:
                d = dates.get(m["row_index"])
                ranks[m["row_index"]] = (0.5 if pd.isna(d) or len(known) < 2
                                         else known.index(d) / (len(known) - 1))
        for m in members:
            idx = m["row_index"]
            rec = df.loc[idx]
            parts: Dict[str, float] = {}
            if "completeness" in weights:
                bad = flagged.get(idx, set())
                good = [c for c in important if str(rec[c] if pd.notna(rec[c]) else "").strip() and c not in bad]
                parts["completeness"] = len(good) / len(important)
            if "active" in weights:
                if any(str(rec[c]).strip().upper() in _set_values(columns[c]) for c in deletion):
                    parts["active"] = 0.0
                elif any(str(rec[c]).strip().upper() in _set_values(columns[c]) for c in blocks):
                    parts["active"] = 0.5
                else:
                    parts["active"] = 1.0
            key_value = str(rec[own_key]).strip() if own_key else ""
            extensions = sum(1 for keys in children if key_value in keys)
            if "usage" in weights:
                parts["usage"] = extensions / len(children)
            if "recency" in weights:
                parts["recency"] = ranks.get(idx, 0.5)
            score = round(100 * sum(weights[k] * v for k, v in parts.items()) / total_weight, 1)
            m["quality_score"] = score
            m["score_breakdown"] = json.dumps({k: round(v, 2) for k, v in parts.items()})
            m["_extensions"] = extensions

        strong = max((_MATCH_RANK.get(m.get("match_type"), 0) for m in members), default=0)
        remembered = next((m for m in members if m.get("is_golden_record")), None)
        if remembered is None and strong < min(_MATCH_RANK[t] for t in Config.SURVIVORSHIP_RECOMMEND_MATCH_TYPES):
            for m in members:
                m["suggested_action"] = "CONFIRM_DUPLICATE_FIRST"
                m.pop("_extensions", None)
            continue
        # Human decision from an earlier run first, then score, usage, completeness, key.
        survivor = remembered or max(members, key=lambda m: (
            m["quality_score"], m["_extensions"], json.loads(m["score_breakdown"]).get("completeness", 0),
            -len(str(m["key_value"])), str(m["key_value"])))
        recommended += 1
        for m in members:
            if m is survivor:
                m["recommended_verdict"], m["suggested_action"] = "UNIQUE", "GOLDEN_RECORD"
                m["is_golden_record"] = True
            else:
                m["recommended_verdict"] = "DUPLICATE"
                m["suggested_action"] = (f"MERGE_INTO {survivor['key_value']}" if m["_extensions"]
                                         else f"BLOCK_AND_DELETE (duplicate of {survivor['key_value']})")
                m["is_golden_record"] = False
            m.pop("_extensions", None)
    logger.info("[%s] survivorship: %d group(s) scored, %d with a recommended survivor (weights %s)",
                table, len(groups), recommended, {k: round(v / total_weight, 2) for k, v in weights.items()})
