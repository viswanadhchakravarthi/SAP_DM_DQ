"""Shared plumbing for the deterministic rule engines (sap_rules, anomaly_rules).

Rules never name a column: they ask the table's column mapping
(``column_mapping``) for the columns playing a concept - ``ctx.cols("COUNTRY")`` -
so the same rule runs on LFA1.LAND1 and on a legacy ``vendors.CountryCode``.
"""

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd
import yaml

from .config import Config
from .logging_config import get_logger

logger = get_logger("rule_context")

SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]


# ---------------------------------------------------------------------------
# Rule pack
# ---------------------------------------------------------------------------

def _full(pattern: str) -> "re.Pattern":
    return re.compile(rf"(?:{pattern})\Z")


@lru_cache(maxsize=4)
def load_pack(path: Optional[str] = None) -> Dict[str, Any]:
    """Parse the rule pack and precompile its regexes. Raises on a broken pack:
    a silently empty rule set would look like clean data."""
    path = path or Config.SAP_RULES_PACK_FILE
    with open(path, encoding="utf-8") as f:
        pack = yaml.safe_load(f) or {}

    pack["_iso"] = set(str(pack.get("iso_countries", "")).split())
    pack["_currencies"] = set(str(pack.get("iso_currencies", "")).split())
    pack["_flag_values"] = {str(v).upper() for v in pack.get("flag_values", ["X"])}
    pack["_postal"] = {str(c): (_full(spec["regex"]), spec.get("example", ""))
                       for c, spec in ((pack.get("postal", {}) or {}).get("formats") or {}).items()}
    tax = pack.get("tax", {}) or {}
    tax_formats: Dict[str, List[Tuple[str, "re.Pattern"]]] = {}
    for country, pattern in (tax.get("eu_vat") or {}).items():
        tax_formats.setdefault(str(country), []).append(("EU VAT", _full(pattern)))
    for country, named in (tax.get("national") or {}).items():
        for name, pattern in named.items():
            tax_formats.setdefault(str(country), []).append((str(name), _full(pattern)))
    pack["_tax_formats"] = tax_formats
    pack["_tax_placeholder"] = _full(tax.get("placeholder_regex", r"(?!)"))
    pack["_eu"] = set(tax.get("eu_members", []))

    text = pack.get("text", {}) or {}
    pack["_illegal_chars"] = set(text.get("illegal_name_chars", ""))
    pack["_mojibake"] = [m.encode("utf-8").decode("unicode_escape") if m.startswith("\\u") else m
                         for m in text.get("mojibake_markers", [])]
    pack["_html_entity"] = re.compile(text.get("html_entity_regex", r"(?!)"))
    pack["_text_placeholder"] = _full(text.get("placeholder_regex", r"(?!)"))
    pack["_email"] = _full(text.get("email_regex", r".*"))
    pack["_phone"] = re.compile(rf"(?:{text.get('phone_regex', '.*')})\Z", re.IGNORECASE)

    if not isinstance(pack.get("standard_columns", {}), dict):
        raise ValueError(f"standard_columns in {path} must map TABLE -> COLUMN -> binding")
    return pack


# ---------------------------------------------------------------------------
# Series helpers
# ---------------------------------------------------------------------------

def text(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.strip()


def upper(series: pd.Series) -> pd.Series:
    return text(series).str.upper()


def blank(series: pd.Series) -> pd.Series:
    return text(series) == ""


def join_keys(df: pd.DataFrame, cols: List[str]) -> pd.Series:
    """One hashable key per row (tuple of trimmed strings) for cross-table joins."""
    parts = [text(df[c]) for c in cols]
    return pd.Series(list(zip(*parts)), index=df.index) if parts else pd.Series([], dtype=object)


# ---------------------------------------------------------------------------
# Coverage (what ran - for the planner prompt and graph.py's duplicate filter)
# ---------------------------------------------------------------------------

@dataclass
class RuleCoverage:
    """What the deterministic rules checked on one table, whether or not they found anything.

    ``lines`` go into the planner prompt; ``pairs`` let graph.py drop planner checks
    that would repeat a rule. A pair is (column, category, sub_type), where sub_type
    "*" matches any sub_type of that category.
    """
    lines: List[str] = field(default_factory=list)
    pairs: Set[Tuple[str, str, str]] = field(default_factory=set)

    def add(self, line: str, columns, category: str, sub_type: str = "*") -> None:
        self.lines.append(line)
        self.pairs.update((str(c).upper(), category, sub_type) for c in columns)

    def covers(self, column: str, category: str, sub_type: Optional[str]) -> bool:
        column = str(column).upper()
        return ((column, category, "*") in self.pairs
                or (column, category, sub_type or "VALUE_ERROR") in self.pairs)


# ---------------------------------------------------------------------------
# Per-table context
# ---------------------------------------------------------------------------

class Ctx:
    """One table, its column mapping, the other loaded tables and the client's switches."""

    def __init__(self, pack, table, df, all_tables, dictionary, client_id, mappings):
        self.pack, self.table, self.df = pack, table, df
        self.all_tables = all_tables or {}
        self.dictionary = dictionary or {}
        self.mappings = mappings or {}
        mapping = self.mappings.get(table) or {"columns": {}, "source": "unmapped", "source_detail": ""}
        overrides = (Config.SAP_RULES_CLIENT_OVERRIDES or {}).get(client_id or "", {}) or {}
        self.disabled = list(Config.SAP_RULES_DISABLED) + list(overrides.get("disabled_rules", []) or [])
        self.auto_fix = overrides.get("auto_fix", {}) or {}
        self.severity_override: Dict[str, str] = {}
        self.label_override: Dict[str, str] = {}
        # Client-required fields on top of the mapped ones (client_overrides.<client>.mandatory).
        columns = {c: dict(b) for c, b in mapping["columns"].items()}
        for col, spec in ((overrides.get("mandatory", {}) or {}).get(table) or {}).items():
            if col in columns:
                columns[col]["required"] = True
                spec = spec or {}
                if spec.get("severity"):
                    self.severity_override[col] = spec["severity"]
                if spec.get("label"):
                    self.label_override[col] = spec["label"]
        self.columns = {c: b for c, b in columns.items() if c in df.columns}
        self.mapping_source = mapping.get("source", "unmapped")
        self.keys = [c for c, b in self.columns.items() if b["part_of_key"]] or [df.columns[0]]
        self.covered = RuleCoverage()
        self._key_values: Optional[pd.Series] = None
        self._key_label = ""

    # -- mapping lookups ----------------------------------------------------
    def cols(self, *concepts: str) -> List[str]:
        return [c for c, b in self.columns.items() if b["concept"] in concepts]

    def b(self, col: str) -> Dict[str, Any]:
        return self.columns[col]

    def country_for(self, col: str) -> Optional[str]:
        """The COUNTRY column deciding this column's format: its related_column,
        else the table's only COUNTRY column."""
        rel = self.columns[col].get("related_column")
        if rel in self.columns:
            return rel
        countries = [c for c in self.cols("COUNTRY") if not self.columns[c]["part_of_key"]] or self.cols("COUNTRY")
        return countries[0] if len(countries) == 1 else None

    def label(self, col: str) -> str:
        """A human name for the column: override, else the client's dictionary text, else the name."""
        if col in self.label_override:
            return self.label_override[col]
        desc = self.dictionary.get((self.table.upper(), str(col).upper()))
        return desc.split(" - ")[0].strip() if desc else str(col)

    def other(self, name: str) -> Optional[pd.DataFrame]:
        other = self.all_tables.get(name)
        return other if other is not None and not other.empty else None

    def other_columns(self, name: str) -> Dict[str, Dict[str, Any]]:
        return (self.mappings.get(name) or {}).get("columns", {})

    # -- output ---------------------------------------------------------------
    def enabled(self, rule_id: str) -> bool:
        off = any(rule_id == d or rule_id.startswith(d + ".") for d in self.disabled)
        if off:
            logger.info("[%s] rule %s disabled by configuration", self.table, rule_id)
        return not off

    def row(self, idx, detail: str) -> Dict[str, Any]:
        if self._key_values is None:   # built once per table, not one df.loc lookup per flagged row
            parts = [self.df[c].astype(object).where(self.df[c].notna(), "").astype(str) for c in self.keys]
            self._key_values = parts[0].str.cat(parts[1:], sep=" / ") if len(parts) > 1 else parts[0]
            self._key_label = " + ".join(self.keys)
        return {
            "row_index": int(idx),
            "key_field": self._key_label,
            "key_value": self._key_values[idx],
            "issue_detail": detail,
        }

    def meaning(self, columns: List[str]) -> str:
        """Why the rule touched these columns - shown under 'check code' in the review app."""
        lines = [f"# Column meaning ({self.mapping_source}):"]
        for col in dict.fromkeys(columns):
            b = self.columns.get(col)
            if b:
                lines.append(f"#   {col} = {b['concept']} - {b['reason']}")
        return "\n".join(lines)


def finding(ctx: Ctx, rule_id: str, column: str, title: str, hypothesis: str, rows: List[Dict[str, Any]],
            category: str, severity: str, *, columns_used: Optional[List[str]] = None,
            sub_type: Optional[str] = None, fix_type: Optional[str] = None,
            auto_fix_value: Optional[str] = None, is_anomaly: bool = False) -> Dict[str, Any]:
    """One finding per rule and table, in the shape main.py saves (same as the planner's)."""
    total = len(rows)
    kept = rows[:Config.SAP_RULES_MAX_ROWS]
    cap_note = f" (first {len(kept)} stored)" if total > len(kept) else ""
    n_rows = len(ctx.df)
    used = columns_used or [c for c in str(column).replace("+", " ").split() if c in ctx.columns]
    return {
        "table": ctx.table,
        "column": column,
        "hypothesis": hypothesis,
        "check_code": (f"# Built-in SAP rule {rule_id} (rule pack v{ctx.pack.get('version', '?')}, "
                       f"deterministic, no LLM).\n# {hypothesis}\n{ctx.meaning(used)}"),
        "summary": f"{title}: {total} of {n_rows} row(s) in {ctx.table}{cap_note}.",
        "severity": severity,
        "confidence": "HIGH",
        "reusable": False,  # already built in - nothing to promote into the skill library
        "category": category,
        "rule_scope": "UNIVERSAL",
        "industry": None,
        "fix_type": fix_type,
        "auto_fix_value": auto_fix_value,
        "is_anomaly": is_anomaly,
        "sub_type": sub_type,
        "raw_tool_result": {"rule_id": rule_id, "rows_flagged": total, "rows_checked": n_rows},
        "detail_rows": kept,
    }
