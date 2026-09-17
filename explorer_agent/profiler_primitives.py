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
    Sandbox/legacy helper kept for previously generated check code and cached
    skills. Delegates to explorer_agent.duplicate_detector, the single
    implementation of duplicate matching (the planner no longer writes
    duplicate checks). ``min_similarity`` is ignored in favour of
    ``duplicates.fuzzy_name_threshold`` in config.yaml.

    Returns detail dicts formatted for finding_items storage and human review.
    """
    from .duplicate_detector import find_duplicate_groups  # local import: detector imports this module

    if tax_cols is None:
        tax_cols = ["STCD1", "STCD2", "STCD3", "STCD4", "STCEG"]
    if extra_cols is None:
        extra_cols = ["ORT01", "STRAS", "LAND1", "TELF1", "SMTP_ADDR"]
    identifiers = [c for c in tax_cols + ["TELF1", "SMTP_ADDR"] if c in df.columns and (c in tax_cols or c in extra_cols)]
    location = [c for c in [postal_col, "STRAS", "ORT01"] if c and c in df.columns and (c == postal_col or c in extra_cols)]
    rules = {
        "key": [key_col] if key_col in df.columns else [],
        "name": name_col,
        "identifiers": [[c] for c in dict.fromkeys(identifiers)],
        "location": location,
        "display": [c for c in [name_col, *location, *identifiers] if c in df.columns],
        "label": "records",
    }
    rows, _ = find_duplicate_groups(df, rules)
    allowed_groups = {f"DUP-{n:03d}" for n in range(1, max_clusters + 1)}
    return [r for r in rows if r["duplicate_group_id"] in allowed_groups]


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
