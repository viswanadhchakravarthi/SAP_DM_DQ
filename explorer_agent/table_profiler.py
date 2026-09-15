
"""
Table-level statistical profiling via ydata-profiling (fg-data-profiling),
generating ONE upfront summary of an entire table's columns - replaces
per-column on-demand tool calls entirely.

CRITICAL PRIVACY NOTE: ydata-profiling's raw JSON is NOT safe to send to
an LLM as-is. `value_counts_without_nan` (categorical columns) contains
ACTUAL RAW VALUES as dict keys, even with sensitive=True. We use an
ALLOWLIST extraction - pull ONLY explicitly named fields into our own
distilled structure. Anything not listed here is dropped by default,
regardless of what the underlying library adds/changes across versions.

▲ VERIFY BEFORE TRUSTING: field names below (value_counts_without_nan,
etc.) match upstream ydata-profiling as of writing. Since you're using
"fg-data-profiling" (possibly a fork/rename), run this once and print
raw.keys() / raw["variables"][<col>].keys() to confirm exact field names
before relying on this in a real run - package internals can differ.
"""

from typing import Dict, Any
import json
import pandas as pd

from data_profiling import ProfileReport  # per your tested import
from .profiler_primitives import mask_value
from .config import Config
from .logging_config import get_logger

logger = get_logger("table_profiler")

_NUMERIC_FIELDS = ["n", "n_missing", "p_missing", "n_distinct", "p_distinct",
                   "mean", "min", "max", "std", "n_zeros", "n_negative"]
_CATEGORICAL_FIELDS = ["n", "n_missing", "p_missing", "n_distinct", "p_distinct", "is_unique"]
_CATEGORICAL_VALUE_COUNT_KEYS = ["value_counts_without_nan", "value_counts_index_sorted"]


def _extract_variable_summary(var_name: str, var_data: Dict[str, Any]) -> Dict[str, Any]:
    var_type = var_data.get("type", "Unknown")
    summary: Dict[str, Any] = {"column": var_name, "detected_type": var_type}

    if var_type == "Numeric":
        for f in _NUMERIC_FIELDS:
            if f in var_data:
                summary[f] = var_data[f]
    else:
        for f in _CATEGORICAL_FIELDS:
            if f in var_data:
                summary[f] = var_data[f]
        for key in _CATEGORICAL_VALUE_COUNT_KEYS:
            raw_counts = var_data.get(key)
            if raw_counts:
                top_items = list(raw_counts.items())[:Config.PROFILING_TOP_N_FREQUENT]
                summary["top_values_masked"] = {mask_value(str(k)): v for k, v in top_items}
                break

    return summary


def profile_table(df: pd.DataFrame, table_name: str) -> Dict[str, Any]:
    logger.info("Profiling table %s (%d rows, %d columns) via ydata-profiling...",
                table_name, len(df), len(df.columns))

    profile = ProfileReport(
        df, sensitive=True, title=f"{table_name} Data Quality Profile",
        minimal=True, samples=None, duplicates=None, interactions=None,
        missing_diagrams=None, progress_bar=False,
    )
    raw = json.loads(profile.to_json())

    variables_raw = raw.get("variables", {})
    distilled_variables = [_extract_variable_summary(name, data) for name, data in variables_raw.items()]

    table_summary = {
        "n_rows": raw.get("table", {}).get("n", len(df)),
        "n_columns": raw.get("table", {}).get("n_var", len(df.columns)),
    }

    logger.info("Profiling complete for %s - %d variable summaries extracted (allowlisted fields only)",
                table_name, len(distilled_variables))
    return {"table_name": table_name, "table": table_summary, "variables": distilled_variables}
