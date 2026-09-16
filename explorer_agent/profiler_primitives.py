import re
import difflib
from typing import Optional, List, Dict, Any, Tuple
import pandas as pd
import numpy as np


def mask_value(value: Any, keep_last: int = 2) -> str:
    """Mask string value preserving only the last few characters for privacy."""
    s = str(value)
    if len(s) <= keep_last:
        return "*" * len(s)
    return "*" * (len(s) - keep_last) + s[-keep_last:]


def normalize_text(text: Any) -> str:
    """Normalize text for fuzzy matching: lowercase, strip punctuation, standardize common company suffixes."""
    if text is None or pd.isna(text):
        return ""
    s = str(text).strip().lower()
    # Normalize common abbreviations in vendor names
    s = re.sub(r'\bpvt\.?\s*ltd\.?', 'private limited', s)
    s = re.sub(r'\bltd\.?', 'limited', s)
    s = re.sub(r'\binc\.?', 'incorporated', s)
    s = re.sub(r'\bcorp\.?', 'corporation', s)
    s = re.sub(r'\bgmbh\b', 'gmbh', s)
    s = re.sub(r'\bco\.?', 'company', s)
    s = re.sub(r'[^a-z0-9\s]', ' ', s)
    return " ".join(s.split())


def fuzzy_token_similarity(s1: Any, s2: Any) -> float:
    """Compute token-sorted similarity percentage (0-100) using difflib.SequenceMatcher."""
    norm1 = normalize_text(s1)
    norm2 = normalize_text(s2)
    if not norm1 or not norm2:
        return 0.0
    if norm1 == norm2:
        return 100.0

    # Token-sort comparison: sort words to be invariant to word order
    tokens1 = " ".join(sorted(norm1.split()))
    tokens2 = " ".join(sorted(norm2.split()))
    ratio = difflib.SequenceMatcher(None, tokens1, tokens2).ratio()
    return round(ratio * 100.0, 1)


def cluster_duplicates(
    df: pd.DataFrame,
    key_col: str = "LIFNR",
    name_col: str = "NAME1",
    postal_col: Optional[str] = "PSTLZ",
    tax_cols: Optional[List[str]] = None,
    extra_cols: Optional[List[str]] = None,
    min_similarity: float = 70.0,
    max_clusters: int = 30,
) -> List[Dict[str, Any]]:
    """
    Cluster duplicate records using dynamic composite matching across available columns:
    Name, Postal Code, Tax Identifiers, Street, City, etc.

    Returns a list of detail dicts formatted for finding_items storage and human review.
    """
    if key_col not in df.columns or name_col not in df.columns:
        return []

    if tax_cols is None:
        tax_cols = [c for c in ["STCD1", "STCD2", "STCD3", "STCD4", "STCEG"] if c in df.columns]
    else:
        tax_cols = [c for c in tax_cols if c in df.columns]

    if extra_cols is None:
        extra_cols = [c for c in ["ORT01", "STRAS", "LAND1", "TELF1", "SMTP_ADDR"] if c in df.columns]
    else:
        extra_cols = [c for c in extra_cols if c in df.columns]

    postal_col_active = postal_col if postal_col and postal_col in df.columns else None

    # Pre-clean records
    records = []
    for idx, row in df.iterrows():
        key_val = str(row[key_col]) if pd.notna(row[key_col]) else str(idx)
        name_val = str(row[name_col]) if pd.notna(row[name_col]) else ""
        norm_name = normalize_text(name_val)
        postal_val = str(row[postal_col_active]).strip() if postal_col_active and pd.notna(row[postal_col_active]) else ""

        taxes = {}
        for tc in tax_cols:
            tv = str(row[tc]).strip() if pd.notna(row[tc]) else ""
            if tv:
                taxes[tc] = tv

        extras = {}
        for ec in extra_cols:
            ev = str(row[ec]).strip() if pd.notna(row[ec]) else ""
            if ev:
                extras[ec] = ev

        records.append({
            "row_index": int(idx),
            "key": key_val,
            "name": name_val,
            "norm_name": norm_name,
            "postal": postal_val,
            "taxes": taxes,
            "extras": extras,
        })

    # Grouping via Union-Find (Disjoint Set)
    n = len(records)
    parent = list(range(n))
    pair_metadata = {}

    def find(i):
        if parent[i] == i:
            return i
        parent[i] = find(parent[i])
        return parent[i]

    def union(i, j, score, match_type, reasons):
        root_i = find(i)
        root_j = find(j)
        if root_i != root_j:
            parent[root_i] = root_j
        pair_key = (min(i, j), max(i, j))
        pair_metadata[pair_key] = (score, match_type, reasons)

    # 1. Exact Tax Match
    tax_index: Dict[Tuple[str, str], List[int]] = {}
    for i, r in enumerate(records):
        for tc, tv in r["taxes"].items():
            if tv:
                tax_index.setdefault((tc, tv), []).append(i)

    for (tc, tv), indices in tax_index.items():
        if len(indices) > 1:
            for a in range(len(indices)):
                for b in range(a + 1, len(indices)):
                    idx_a, idx_b = indices[a], indices[b]
                    reasons = f"Exact match on Tax ID ({tc}: '{tv}')"
                    score = 100.0
                    if records[idx_a]["name"] and records[idx_b]["name"]:
                        name_sim = fuzzy_token_similarity(records[idx_a]["name"], records[idx_b]["name"])
                        reasons += f"; Name similarity: {name_sim}% ('{records[idx_a]['name']}' vs '{records[idx_b]['name']}')"
                    union(idx_a, idx_b, score, "EXACT", reasons)

    # 2. Exact Name + Postal Code Match
    name_postal_index: Dict[Tuple[str, str], List[int]] = {}
    for i, r in enumerate(records):
        if r["norm_name"] and r["postal"]:
            name_postal_index.setdefault((r["norm_name"], r["postal"]), []).append(i)

    for (n_norm, post), indices in name_postal_index.items():
        if len(indices) > 1:
            for a in range(len(indices)):
                for b in range(a + 1, len(indices)):
                    idx_a, idx_b = indices[a], indices[b]
                    reasons = f"Exact match on Normalized Name ('{records[idx_a]['name']}') and Postal Code ('{post}')"
                    union(idx_a, idx_b, 100.0, "EXACT", reasons)

    # 3. Fuzzy Name Matching (+ Postal / City verification)
    # Check pairwise on candidates (optimizing for reasonable dataset sizes)
    step = 1 if n <= 400 else max(1, n // 400)
    for i in range(0, n, step):
        r_i = records[i]
        if not r_i["norm_name"] or len(r_i["norm_name"]) < 4:
            continue
        for j in range(i + 1, min(i + 120, n)):
            r_j = records[j]
            if not r_j["norm_name"] or len(r_j["norm_name"]) < 4:
                continue

            sim = fuzzy_token_similarity(r_i["norm_name"], r_j["norm_name"])
            if sim >= min_similarity:
                same_postal = bool(r_i["postal"] and r_j["postal"] and r_i["postal"] == r_j["postal"])
                same_city = bool(
                    r_i["extras"].get("ORT01") and r_j["extras"].get("ORT01") and
                    normalize_text(r_i["extras"]["ORT01"]) == normalize_text(r_j["extras"]["ORT01"])
                )

                if sim >= 95.0 or (sim >= 80.0 and (same_postal or same_city)):
                    m_type = "PROBABLE" if sim < 100.0 else "EXACT"
                    reasons = f"Fuzzy Name match ({sim}%: '{r_i['name']}' vs '{r_j['name']}')"
                    if same_postal:
                        reasons += f"; Matching Postal Code ({r_i['postal']})"
                    if same_city:
                        reasons += f"; Matching City ({r_i['extras'].get('ORT01')})"
                    union(i, j, sim, m_type, reasons)
                elif sim >= 70.0 and (same_postal or same_city):
                    reasons = f"Similar Name ({sim}%: '{r_i['name']}' vs '{r_j['name']}') in same area (Postal: {r_i['postal'] or '-'}, City: {r_i['extras'].get('ORT01') or '-'})"
                    union(i, j, sim, "SIMILAR", reasons)

    # Assemble groups
    groups: Dict[int, List[int]] = {}
    for i in range(n):
        root = find(i)
        groups.setdefault(root, []).append(i)

    # Filter only genuine multi-item clusters
    dup_clusters = [members for members in groups.values() if len(members) > 1]
    dup_clusters.sort(key=lambda m: len(m), reverse=True)
    dup_clusters = dup_clusters[:max_clusters]

    detail_items: List[Dict[str, Any]] = []
    for g_idx, members in enumerate(dup_clusters, 1):
        group_id = f"DUP-GRP-{g_idx:03d}"

        # Find maximum similarity and reasons for this cluster
        cluster_scores = []
        cluster_reasons = []
        cluster_types = []
        for a in range(len(members)):
            for b in range(a + 1, len(members)):
                pair_key = (min(members[a], members[b]), max(members[a], members[b]))
                if pair_key in pair_metadata:
                    score, m_type, reasons = pair_metadata[pair_key]
                    cluster_scores.append(score)
                    cluster_types.append(m_type)
                    cluster_reasons.append(reasons)

        group_score = max(cluster_scores) if cluster_scores else 85.0
        if 100.0 in cluster_scores:
            group_type = "EXACT"
        elif any(t == "PROBABLE" for t in cluster_types) or group_score >= 80.0:
            group_type = "PROBABLE"
        else:
            group_type = "SIMILAR"

        primary_reason = cluster_reasons[0] if cluster_reasons else f"Composite duplicate cluster with similarity {group_score}%"

        # Heuristic for default initial candidate golden record: record with most populated non-empty fields
        def record_completeness(m_idx):
            r = records[m_idx]
            count = (1 if r["name"] else 0) + (1 if r["postal"] else 0) + len(r["taxes"]) + len(r["extras"])
            return count

        best_member = max(members, key=record_completeness)

        for m_idx in members:
            rec = records[m_idx]
            is_initial_golden = 1 if m_idx == best_member else 0

            # Build field diff / comparison context
            field_summary = f"Name: {rec['name'] or 'BLANK'} | Postal: {rec['postal'] or 'BLANK'}"
            if rec["taxes"]:
                field_summary += " | Taxes: " + ", ".join(f"{k}={v}" for k, v in rec["taxes"].items())
            if rec["extras"].get("ORT01"):
                field_summary += f" | City: {rec['extras']['ORT01']}"

            detail_items.append({
                "row_index": rec["row_index"],
                "key_field": key_col,
                "key_value": rec["key"],
                "issue_detail": f"[{group_type} {group_score}%] {primary_reason}. Details: {field_summary}",
                "duplicate_group_id": group_id,
                "similarity_score": group_score,
                "match_type": group_type,
                "match_reasons": primary_reason,
                "is_golden_record": is_initial_golden,
                "review_verdict": "PENDING",
                "suggested_action": "RETAIN_AS_GOLDEN" if is_initial_golden else f"MERGE_INTO_GOLDEN (Target: {records[best_member]['key']})",
            })

    return detail_items


def detect_distribution_outliers(
    series: pd.Series,
    iqr_multiplier: float = 1.5,
    min_samples: int = 10,
) -> Dict[str, Any]:
    """
    Perform statistical distribution and outlier analysis on a Pandas Series.
    Works for numeric fields or discrete numerical codes (like payment terms ZTERM e.g. 30, 45, 60 vs 365).
    """
    cleaned = pd.to_numeric(series.dropna(), errors="coerce").dropna()
    if len(cleaned) < min_samples:
        return {"has_outliers": False, "summary": "Insufficient data samples for distribution analysis", "outliers": []}

    q25 = cleaned.quantile(0.25)
    q75 = cleaned.quantile(0.75)
    iqr = q75 - q25
    lower_bound = q25 - (iqr_multiplier * iqr)
    upper_bound = q75 + (iqr_multiplier * iqr)

    outliers_mask = (cleaned < lower_bound) | (cleaned > upper_bound)
    outlier_rows = cleaned[outliers_mask]

    # Calculate majority pattern
    q90 = cleaned.quantile(0.90)
    q95 = cleaned.quantile(0.95)

    has_outliers = len(outlier_rows) > 0
    top_outliers = outlier_rows.value_counts().head(5).to_dict()

    summary = (
        f"Distribution: Q25={q25:.1f}, Median={cleaned.median():.1f}, Q75={q75:.1f}. "
        f"95% of records have values <= {q95:.1f}. "
        f"Found {len(outlier_rows)} outlier record(s) outside [{lower_bound:.1f}, {upper_bound:.1f}]. "
        f"Top outlier values: {top_outliers}."
    )

    return {
        "has_outliers": has_outliers,
        "outlier_count": int(len(outlier_rows)),
        "outlier_pct": round((len(outlier_rows) / len(cleaned)) * 100, 2),
        "lower_bound": float(lower_bound),
        "upper_bound": float(upper_bound),
        "q95": float(q95),
        "top_outlier_values": top_outliers,
        "summary": summary,
    }
