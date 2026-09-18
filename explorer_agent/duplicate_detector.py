"""Deterministic duplicate detection - the matching engine, not the rule author.

Duplicate matching used to depend on the planner LLM remembering to call a
clustering helper from generated ``detail_code``; in practice it rarely did,
so DUPLICATE findings arrived with no rows to review while still costing
planner/reflector tokens. Duplicates are now detected here for every table,
by the deterministic engine below, and the planner is told not to propose
DUPLICATE checks at all.

WHICH columns the engine may use is a separate question with a separate answer:
the LLM drafts that rule spec once per client and schema from the data
dictionary and sanitized statistics (``duplicate_rule_planner``), it is saved
(``memory.duplicate_rule_store``), and ``duplicate_rules.resolve_rules`` hands
it to this module on every later run without any LLM call. Nothing in this file
decides what a column means.

Every table is checked for identical rows, and for repeated values of a key the
dictionary marks as a primary key (both EXACT). Beyond that, matching is
evidence based. Two records (with different business keys) are linked only
when at least one of these holds:

* EXACT    - they share a strong identifier (tax ID, e-mail, phone, IBAN,
             bank key + account, ...), or have the same normalized name AND
             agree on at least two location fields.
* PROBABLE - same normalized name and at least one matching location field,
             or a fuzzy name match >= 90% corroborated by a location field.
* SIMILAR  - fuzzy name match between the configured threshold and 90% with a
             matching location field, or the same name in a different place.

Guards against the noise the old helper produced:

* Names whose numbers differ ("Plant 1" vs "Plant 2") never fuzzy-match.
* Placeholder identifiers ("INVALIDIBAN", "N/A", "0000000", ...) and values
  shared by more than ``max_identifier_share`` records are ignored - a value
  that many records share is a default, not an identity.
* Candidates come from blocking (identifier / name / location indexes), so
  every candidate pair is compared regardless of row order, without an
  all-pairs scan.

Row-level output goes only to the local SQLite store and the review UI - it is
never sent to an LLM (same rule as ``detail_code``).
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from . import client_knowledge, duplicate_rules
from .config import Config
from .logging_config import get_logger
from .profiler_primitives import fuzzy_token_similarity, normalize_text

logger = get_logger("duplicate_detector")

MATCH_RANK = {"EXACT": 3, "PROBABLE": 2, "SIMILAR": 1}
_PROBABLE_NAME_SIMILARITY = 90.0

_PLACEHOLDER_RE = re.compile(
    r"^(invalid.*|n/?a|none|null|nil|tbd|unknown|test.*|dummy.*|x+|0+|9+|(.)\2+)$",
    re.IGNORECASE,
)
_MIN_IDENTIFIER_LEN = 5
_MIN_NAME_LEN = 4


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def _clean(value: Any) -> str:
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return ""
    return str(value).strip()


def _normalize_identifier(value: Any) -> str:
    raw = _clean(value)
    if not raw:
        return ""
    norm = re.sub(r"[\s\-./()+_]", "", raw).upper()
    if len(norm) < _MIN_IDENTIFIER_LEN or _PLACEHOLDER_RE.match(raw) or _PLACEHOLDER_RE.match(norm):
        return ""
    return norm


def _normalize_location(value: Any) -> str:
    return normalize_text(_clean(value))


def _numeric_tokens(norm_name: str) -> frozenset:
    return frozenset(re.findall(r"\d+", norm_name))


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, i: int, j: int) -> None:
        ri, rj = self.find(i), self.find(j)
        if ri != rj:
            self.parent[ri] = rj


def find_duplicate_groups(df: pd.DataFrame, rules: Dict[str, Any],
                          decisions: Optional[Dict[str, Dict[str, Any]]] = None,
                          ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return (detail rows for finding_items, summary stats) for one table.

    ``decisions`` are the client's remembered duplicate decisions for this table
    (client_knowledge.load_duplicate_decisions): pairs reviewed as UNIQUE are not
    linked, and DUPLICATE / TO_BE_CONFIRMED verdicts are pre-filled.
    """
    decisions = decisions or {}
    key_cols = [c for c in rules["key"] if c in df.columns]
    key_label = " + ".join(key_cols) or "Row"
    location_cols = [c for c in rules["location"] if c in df.columns]
    # A name match needs location fields to confirm it - without them, names aren't used.
    name_col = rules["name"] if rules["name"] in df.columns and location_cols else None
    identifiers = [cols for cols in rules["identifiers"] if all(c in df.columns for c in cols)]
    display_cols = [c for c in (rules["display"] or list(df.columns)) if c in df.columns and c not in key_cols]

    max_share = Config.DUPLICATE_MAX_IDENTIFIER_SHARE
    fuzzy_threshold = Config.DUPLICATE_FUZZY_NAME_THRESHOLD
    max_block = Config.DUPLICATE_MAX_BLOCK_SIZE

    records = []
    for pos, (idx, row) in enumerate(df.iterrows()):
        norm_name = normalize_text(_clean(row[name_col])) if name_col else ""
        key_value = " / ".join(_clean(row[c]) for c in key_cols) if key_cols else ""
        records.append({
            "pos": pos,
            "row_index": int(idx) if str(idx).lstrip("-").isdigit() else pos,
            "key": key_value if key_value.strip(" /") else f"row {pos + 1}",
            "name": _clean(row[name_col]) if name_col else "",
            "norm_name": norm_name if len(norm_name) >= _MIN_NAME_LEN else "",
            "numbers": _numeric_tokens(norm_name),
            "location": {c: _normalize_location(row[c]) for c in location_cols},
            "identifiers": {
                " + ".join(cols): "|".join(_normalize_identifier(row[c]) for c in cols)
                if all(_normalize_identifier(row[c]) for c in cols) else ""
                for cols in identifiers
            },
            "display": {c: _clean(row[c]) for c in display_cols},
            "raw_identifiers": {" + ".join(cols): " / ".join(_clean(row[c]) for c in cols) for cols in identifiers},
        })

    # pair (i, j) -> evidence
    evidence: Dict[Tuple[int, int], Dict[str, Any]] = {}

    def add_pair(i: int, j: int) -> Optional[Dict[str, Any]]:
        if records[i]["key"] == records[j]["key"]:
            return None  # same business object (e.g. two bank rows of one vendor)
        pair = (min(i, j), max(i, j))
        return evidence.setdefault(pair, {"identifiers": []})

    skipped_shared: Dict[str, int] = {}

    # 1. Shared strong identifiers
    for label in (" + ".join(cols) for cols in identifiers):
        index: Dict[str, List[int]] = {}
        for r in records:
            if r["identifiers"][label]:
                index.setdefault(r["identifiers"][label], []).append(r["pos"])
        for members in index.values():
            distinct_keys = {records[m]["key"] for m in members}
            if len(distinct_keys) < 2:
                continue
            if len(distinct_keys) > max_share:
                skipped_shared[label] = skipped_shared.get(label, 0) + 1
                continue
            for a_pos, a in enumerate(members):
                for b in members[a_pos + 1:]:
                    ev = add_pair(a, b)
                    if ev is not None:
                        ev["identifiers"].append(label)

    # 2. Same normalized name (any row order, no window)
    if name_col:
        name_index: Dict[str, List[int]] = {}
        for r in records:
            if r["norm_name"]:
                name_index.setdefault(r["norm_name"], []).append(r["pos"])
        for norm_name, members in name_index.items():
            distinct_keys = {records[m]["key"] for m in members}
            if len(distinct_keys) < 2:
                continue
            if len(distinct_keys) > max_share:
                # A name that many records share ("Orphan Vendor", "One-time vendor")
                # is a generic placeholder, not evidence of duplication.
                skipped_shared[name_col] = skipped_shared.get(name_col, 0) + 1
                continue
            for a_pos, a in enumerate(members):
                for b in members[a_pos + 1:]:
                    add_pair(a, b)

    # 3. Fuzzy names, only within a shared location block
    if name_col and location_cols:
        blocks: Dict[Tuple[str, str], List[int]] = {}
        for r in records:
            if not r["norm_name"]:
                continue
            for c in location_cols:
                if r["location"][c]:
                    blocks.setdefault((c, r["location"][c]), []).append(r["pos"])
        oversized = 0
        for members in blocks.values():
            if len(members) < 2:
                continue
            if len(members) > max_block:
                # Sub-block by first letter of the name to keep comparisons bounded.
                sub: Dict[str, List[int]] = {}
                for m in members:
                    sub.setdefault(records[m]["norm_name"][0], []).append(m)
                groups = list(sub.values())
            else:
                groups = [members]
            for group in groups:
                if len(group) > max_block:
                    oversized += 1
                    continue
                for a_pos, a in enumerate(group):
                    for b in group[a_pos + 1:]:
                        if records[a]["numbers"] != records[b]["numbers"]:
                            continue
                        if (min(a, b), max(a, b)) in evidence:
                            continue
                        if fuzzy_token_similarity(records[a]["norm_name"], records[b]["norm_name"]) >= fuzzy_threshold:
                            add_pair(a, b)
        if oversized:
            logger.warning("Skipped fuzzy name matching in %d oversized location block(s) (> %d rows)",
                           oversized, max_block)

    for label, count in skipped_shared.items():
        logger.info("Ignored %d %s value(s) shared by more than %d records (treated as placeholders)",
                    count, label, max_share)

    for r in records:
        r["record_id"] = client_knowledge.record_id(r["key"], r["display"])

    # Classify each candidate pair, skipping pairs a reviewer already called unique
    links: Dict[Tuple[int, int], Tuple[str, float, str]] = {}
    suppressed = 0

    def link(i: int, j: int, classified: Tuple[str, float, str]) -> None:
        nonlocal suppressed
        if client_knowledge.is_known_not_duplicate(decisions, records[i]["record_id"], records[j]["record_id"]):
            suppressed += 1
            return
        pair = (min(i, j), max(i, j))
        if pair not in links or MATCH_RANK[classified[0]] >= MATCH_RANK[links[pair][0]]:
            links[pair] = classified

    for (i, j), ev in evidence.items():
        classified = _classify_pair(records[i], records[j], ev, location_cols, fuzzy_threshold)
        if classified:
            link(i, j, classified)

    # Row-level duplicates that don't depend on names/identifiers, so every table
    # gets them. These bypass the same-key rule on purpose (star-linked to the
    # first row so a large group doesn't create n^2 pairs).
    if rules.get("key_unique") and key_cols:
        key_groups = df.groupby(key_cols, dropna=True, sort=False).indices
        for key_value, positions in key_groups.items():
            if len(positions) > 1:
                shown = " / ".join(map(str, key_value)) if isinstance(key_value, tuple) else key_value
                for p in positions[1:]:
                    link(int(positions[0]), int(p),
                         ("EXACT", 100.0, f"same {key_label} '{shown}' - the key should be unique"))
    row_hashes = pd.util.hash_pandas_object(df, index=False)
    for positions in pd.Series(range(len(df))).groupby(row_hashes.values).apply(list):
        if len(positions) > 1:
            for p in positions[1:]:
                link(positions[0], p, ("EXACT", 100.0, f"identical rows - all {df.shape[1]} columns are equal"))

    empty_stats = {"groups": 0, "records": 0, "EXACT": 0, "PROBABLE": 0, "SIMILAR": 0,
                   "suppressed_pairs": suppressed, "remembered_records": 0}
    if not links:
        return [], empty_stats

    uf = _UnionFind(len(records))
    for i, j in links:
        uf.union(i, j)
    cluster_members: Dict[int, set] = {}
    cluster_links: Dict[int, list] = {}
    for pair, link in links.items():
        root = uf.find(pair[0])
        cluster_members.setdefault(root, set()).update(pair)
        cluster_links.setdefault(root, []).append((pair, link))

    assembled = []
    for root, members in cluster_members.items():
        member_links = cluster_links[root]
        best_type = max((t for _, (t, _, _) in member_links), key=lambda t: MATCH_RANK[t])
        best_score = max(s for _, (_, s, _) in member_links)
        assembled.append((members, member_links, best_type, best_score))
    assembled.sort(key=lambda c: (MATCH_RANK[c[2]], c[3], len(c[0])), reverse=True)

    max_groups = Config.DUPLICATE_MAX_GROUPS
    if len(assembled) > max_groups:
        logger.warning("Found %d duplicate groups; keeping the strongest %d (duplicates.max_groups)",
                       len(assembled), max_groups)
        assembled = assembled[:max_groups]

    rows: List[Dict[str, Any]] = []
    stats = {**empty_stats, "groups": len(assembled)}
    for g_num, (members, member_links, group_type, group_score) in enumerate(assembled, 1):
        group_id = f"DUP-{g_num:03d}"
        stats[group_type] += 1
        stats["records"] += len(members)
        for m in sorted(members, key=lambda p: records[p]["key"]):
            own = [(pair, link) for pair, link in member_links if m in pair]
            own.sort(key=lambda x: (MATCH_RANK[x[1][0]], x[1][1]), reverse=True)
            m_type, m_score, _ = own[0][1]
            reasons = []
            for pair, (_, _, reason) in own[:3]:
                other = records[pair[1] if pair[0] == m else pair[0]]
                reasons.append(f"vs {key_label} {other['key']}: {reason}")
            if len(own) > 3:
                reasons.append(f"and {len(own) - 3} more match(es) in this group")
            rec = records[m]
            row = {
                "row_index": rec["row_index"],
                "key_field": key_label,
                "key_value": rec["key"],
                "issue_detail": f"Possible duplicate ({m_type} {m_score:g}%) - " + "; ".join(reasons),
                "duplicate_group_id": group_id,
                "similarity_score": m_score,
                "match_type": m_type,
                "match_reasons": "; ".join(reasons),
                "review_verdict": "PENDING",
                "record_data": rec["display"],
            }
            remembered = client_knowledge.carried_verdict(
                decisions, rec["record_id"], [records[o]["record_id"] for o in members if o != m])
            if remembered:
                row.update({
                    "review_verdict": remembered["verdict"],
                    "decision_source": "REMEMBERED",
                    "reviewed_at": remembered["decided_at"],
                    "reviewer_comment": f"Remembered from an earlier review (run {remembered['run_id'][:8]})",
                })
                stats["remembered_records"] += 1
            rows.append(row)
    return rows, stats


def _classify_pair(a: Dict[str, Any], b: Dict[str, Any], ev: Dict[str, Any],
                   location_cols: List[str], fuzzy_threshold: float) -> Optional[Tuple[str, float, str]]:
    same_location = [c for c in location_cols if a["location"][c] and a["location"][c] == b["location"][c]]
    location_text = ", ".join(f"same {c}" for c in same_location)

    if ev["identifiers"]:
        shared = "; ".join(f"same {label} ({a['raw_identifiers'][label]})" for label in ev["identifiers"])
        return "EXACT", 100.0, shared + (f"; {location_text}" if location_text else "")

    if not (a["norm_name"] and b["norm_name"]):
        return None

    if a["norm_name"] == b["norm_name"]:
        if len(same_location) >= 2:
            return "EXACT", 100.0, f"same name '{a['name']}'; {location_text}"
        if same_location:
            return "PROBABLE", 95.0, f"same name '{a['name']}'; {location_text}"
        return "SIMILAR", 75.0, f"same name '{a['name']}' but different location"

    if a["numbers"] != b["numbers"] or not same_location:
        return None
    similarity = fuzzy_token_similarity(a["norm_name"], b["norm_name"])
    if similarity < fuzzy_threshold:
        return None
    match_type = "PROBABLE" if similarity >= _PROBABLE_NAME_SIMILARITY else "SIMILAR"
    return match_type, similarity, f"similar names ({similarity:g}%: '{a['name']}' vs '{b['name']}'); {location_text}"


# ---------------------------------------------------------------------------
# Finding assembly (same dict shape graph.py produces for LLM findings)
# ---------------------------------------------------------------------------

_UNDISPLAYED_RULE_KEYS = ("why", "checks", "source", "source_detail", "notes")


def describe_rules(table_name: str, rules: Dict[str, Any]) -> str:
    """The 'View Matching Rules' text: what is checked, and why each column was used."""
    lines = [
        "# Deterministic duplicate matching - the rules below are applied in plain Python.",
        f"# Rules for {table_name}: {rules['source_detail']}.",
        "",
        "Checks:",
        *(f"  - {c}" for c in rules["checks"]),
    ]
    if rules.get("notes"):
        lines += ["", f"Note: {rules['notes']}"]
    if rules.get("why"):
        lines += ["", "Columns used:", *(f"  - {col}: {reason}" for col, reason in rules["why"].items())]
    lines += ["", "Rules:",
              json.dumps({k: v for k, v in rules.items() if k not in _UNDISPLAYED_RULE_KEYS}, indent=2)]
    return "\n".join(lines)


def detect_table_duplicates(table_name: str, df: pd.DataFrame, client_id: Optional[str] = None,
                            dictionary: Optional[Dict[Tuple[str, str], str]] = None,
                            client_name: Optional[str] = None,
                            rule_planner=None) -> Optional[Dict[str, Any]]:
    """Build one DUPLICATE finding (with all group rows) for any table, or None.

    Rules are resolved by ``duplicate_rules.resolve_rules``: a config.yaml
    override, this client's saved rules, or - for a table/schema never seen for
    this client - one LLM call through ``rule_planner`` using ``dictionary``
    (data_loader.load_data_dictionary), after which they are saved. With a
    ``client_id``, that client's remembered decisions are applied as well (see
    client_knowledge): known-unique pairs are skipped and earlier verdicts
    pre-filled.
    """
    if not Config.DUPLICATES_ENABLED or df.empty:
        return None
    rules = duplicate_rules.resolve_rules(table_name, df, dictionary, client_id=client_id,
                                          client_name=client_name, rule_planner=rule_planner)
    logger.info("[%s] duplicate rules (%s): %s", table_name, rules["source"], "; ".join(rules["checks"]))

    decisions = client_knowledge.load_duplicate_decisions(client_id, table_name)
    rows, stats = find_duplicate_groups(df, rules, decisions)
    logger.info("[%s] duplicate detection: %d group(s), %d record(s) (EXACT=%d PROBABLE=%d SIMILAR=%d) | "
                "client=%s remembered decisions=%d, unique pairs skipped=%d, verdicts pre-filled=%d",
                table_name, stats.get("groups", 0), stats.get("records", 0),
                stats.get("EXACT", 0), stats.get("PROBABLE", 0), stats.get("SIMILAR", 0),
                client_id, len(decisions), stats.get("suppressed_pairs", 0), stats.get("remembered_records", 0))
    if not rows:
        return None

    breakdown = ", ".join(f"{stats[t]} {t.lower()}" for t in ("EXACT", "PROBABLE", "SIMILAR") if stats[t])
    memory_notes = []
    if stats["suppressed_pairs"]:
        memory_notes.append(f"{stats['suppressed_pairs']} pair(s) previously reviewed as unique skipped")
    if stats["remembered_records"]:
        memory_notes.append(f"{stats['remembered_records']} earlier decision(s) pre-filled")
    memory_text = f" {'; '.join(memory_notes).capitalize()}." if memory_notes else ""
    severity = "HIGH" if stats["EXACT"] else ("MEDIUM" if stats["PROBABLE"] else "LOW")

    return {
        "table": table_name,
        "column": " + ".join(rules["key"]) or (rules["name"] or "ROW"),
        "hypothesis": f"{table_name} rows that duplicate each other, checked by: {'; '.join(rules['checks'])}.",
        "check_code": describe_rules(table_name, rules),
        "summary": (f"{stats['groups']} duplicate group(s) covering {stats['records']} {rules['label']} "
                    f"({breakdown}).{memory_text}"),
        "severity": severity,
        "confidence": "HIGH",
        "reusable": False,  # built-in rule, nothing to promote into the skill library
        "category": "DUPLICATE",
        "rule_scope": "UNIVERSAL",
        "industry": None,
        "fix_type": "MANUAL_FIX",
        "auto_fix_value": None,
        "is_anomaly": False,
        "sub_type": None,
        "raw_tool_result": stats,
        "detail_rows": rows,
    }
