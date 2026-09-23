"""Deterministic SAP domain rules - known SAP standards, checked without an LLM.

The planner LLM used to re-invent even textbook SAP checks (deletion flags,
mandatory fields, orphaned LFB1 rows, which tax field holds an Indian GSTIN) on
every run, with different code and different coverage each time. Those checks
are now data in a rule pack (``Config.SAP_RULES_PACK_FILE``, by default
``rule_packs/sap_master_data.yaml``) executed here, before the planner, at zero
LLM cost. The planner is then told what already ran, so it spends its one call
on what a fixed rule cannot know.

Rule families (one finding per rule and table, with every offending row):

* ACTIVENESS   - status flags set; dormancy = old creation date AND never
                 extended to an organisational-level table.
* COMPLETENESS - mandatory fields blank (MANUAL_FIX unless the client agreed a
                 default in config.yaml).
* CORRECTNESS  - invalid ISO country keys; postal codes checked against their
                 country's format; tax numbers checked across ALL tax fields of a
                 row together (STCD1..STCD5, STCEG), by country.
* RELATIONSHIP_INTEGRITY - orphaned child rows, and master-level deletion flags
                 or blocks not carried to the organisational-level rows.

Every rule is skipped when a table or column it needs is not in the upload, and
can be switched off per client (``sap_rules.client_overrides``): a standard
field can mean something else at one company. Propagation rules can also carry
a ``guard`` on the client's data dictionary for fields whose meaning varies.

Row-level output goes only to the local SQLite store and the review UI - never
to an LLM (same rule as ``detail_code``). Only the rule descriptions reach the
planner prompt.
"""

import re
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd
import yaml

from .config import Config
from .logging_config import get_logger
from .metrics import metrics

logger = get_logger("sap_rules")

_SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]


# ---------------------------------------------------------------------------
# Rule pack
# ---------------------------------------------------------------------------

@lru_cache(maxsize=4)
def load_pack(path: Optional[str] = None) -> Dict[str, Any]:
    """Parse the rule pack and precompile its regexes. Raises on a broken pack:
    a silently empty rule set would look like clean data."""
    path = path or Config.SAP_RULES_PACK_FILE
    with open(path, encoding="utf-8") as f:
        pack = yaml.safe_load(f) or {}

    def full(pattern: str) -> "re.Pattern":
        return re.compile(rf"(?:{pattern})\Z")

    pack["_iso"] = set(str(pack.get("iso_countries", "")).split())
    pack["_flag_values"] = {str(v).upper() for v in pack.get("flag_values", ["X"])}
    postal = pack.get("postal", {}) or {}
    pack["_postal"] = {str(c): (full(spec["regex"]), spec.get("example", ""))
                       for c, spec in (postal.get("formats") or {}).items()}
    tax = pack.get("tax", {}) or {}
    tax_formats: Dict[str, List[Tuple[str, "re.Pattern"]]] = {}
    for country, pattern in (tax.get("eu_vat") or {}).items():
        tax_formats.setdefault(str(country), []).append(("EU VAT", full(pattern)))
    for country, named in (tax.get("national") or {}).items():
        for name, pattern in named.items():
            tax_formats.setdefault(str(country), []).append((str(name), full(pattern)))
    pack["_tax_formats"] = tax_formats
    pack["_tax_placeholder"] = full(tax.get("placeholder_regex", r"(?!)"))
    pack["_eu"] = set(tax.get("eu_members", []))
    for rule in pack.get("propagation", []) or []:
        if "join" not in rule or "id" not in rule:
            raise ValueError(f"Propagation rule {rule!r} in {path} needs 'id' and 'join'")
    return pack


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _text(series: pd.Series) -> pd.Series:
    return series.fillna("").astype(str).str.strip()


def _upper(series: pd.Series) -> pd.Series:
    return _text(series).str.upper()


def _blank(series: pd.Series) -> pd.Series:
    return _text(series) == ""


def _dict_label(dictionary: Dict[Tuple[str, str], str], table: str, column: str) -> Optional[str]:
    """Short dictionary description ('Posting Block'), or None when undocumented."""
    desc = (dictionary or {}).get((table.upper(), column.upper()))
    return desc.split(" - ")[0].strip() if desc else None


def _key_cols(pack: Dict[str, Any], table: str, df: pd.DataFrame) -> List[str]:
    cols = [c for c in (pack.get("keys", {}) or {}).get(table, []) if c in df.columns]
    return cols or [df.columns[0]]


def _join_keys(df: pd.DataFrame, cols: List[str]) -> pd.Series:
    """One hashable key per row (tuple of trimmed strings) for cross-table joins."""
    parts = [_text(df[c]) for c in cols]
    return pd.Series(list(zip(*parts)), index=df.index) if parts else pd.Series([], dtype=object)


@dataclass
class RuleCoverage:
    """What the rule pack checked on one table, whether or not it found anything.

    ``lines`` go into the planner prompt; ``pairs`` let graph.py drop planner checks
    that would repeat a rule. A pair is (column, category, sub_type), where sub_type
    "*" matches any sub_type of that category.
    """
    lines: List[str] = field(default_factory=list)
    pairs: Set[Tuple[str, str, str]] = field(default_factory=set)

    def add(self, line: str, columns, category: str, sub_type: str = "*") -> None:
        self.lines.append(line)
        self.pairs.update((c, category, sub_type) for c in columns)

    def covers(self, column: str, category: str, sub_type: Optional[str]) -> bool:
        return ((column, category, "*") in self.pairs
                or (column, category, sub_type or "VALUE_ERROR") in self.pairs)


class _Ctx:
    """Everything one table's rules need, plus the client's switches."""

    def __init__(self, pack, table, df, all_tables, dictionary, client_id):
        self.pack, self.table, self.df = pack, table, df
        self.all_tables = all_tables or {}
        self.dictionary = dictionary or {}
        self.keys = _key_cols(pack, table, df)
        overrides = (Config.SAP_RULES_CLIENT_OVERRIDES or {}).get(client_id or "", {}) or {}
        self.disabled = list(Config.SAP_RULES_DISABLED) + list(overrides.get("disabled_rules", []) or [])
        self.auto_fix = overrides.get("auto_fix", {}) or {}
        # Client-required fields on top of the SAP standard ones, same shape as the pack.
        self.extra_mandatory = ((overrides.get("mandatory", {}) or {}).get(table) or {})
        self.covered = RuleCoverage()

    def enabled(self, rule_id: str) -> bool:
        off = any(rule_id == d or rule_id.startswith(d + ".") for d in self.disabled)
        if off:
            logger.info("[%s] SAP rule %s disabled by configuration", self.table, rule_id)
        return not off

    def row(self, idx, detail: str) -> Dict[str, Any]:
        rec = self.df.loc[idx]
        return {
            "row_index": int(idx),
            "key_field": " + ".join(self.keys),
            "key_value": " / ".join(str(rec[c]) if pd.notna(rec[c]) else "" for c in self.keys),
            "issue_detail": detail,
        }

    def other(self, name: str) -> Optional[pd.DataFrame]:
        other = self.all_tables.get(name)
        return other if other is not None and not other.empty else None


def _finding(ctx: _Ctx, rule_id: str, column: str, title: str, hypothesis: str, rows: List[Dict[str, Any]],
             category: str, severity: str, *, sub_type: Optional[str] = None,
             fix_type: Optional[str] = None, auto_fix_value: Optional[str] = None) -> Dict[str, Any]:
    total = len(rows)
    kept = rows[:Config.SAP_RULES_MAX_ROWS]
    cap_note = f" (first {len(kept)} stored)" if total > len(kept) else ""
    n_rows = len(ctx.df)
    return {
        "table": ctx.table,
        "column": column,
        "hypothesis": hypothesis,
        "check_code": (f"# Built-in SAP rule {rule_id} (rule pack v{ctx.pack.get('version', '?')}, "
                       f"explorer_agent/sap_rules.py) - deterministic, no LLM.\n# {hypothesis}"),
        "summary": f"{title}: {total} of {n_rows} row(s) in {ctx.table}{cap_note}.",
        "severity": severity,
        "confidence": "HIGH",
        "reusable": False,  # already built in - nothing to promote into the skill library
        "category": category,
        "rule_scope": "UNIVERSAL",
        "industry": None,
        "fix_type": fix_type,
        "auto_fix_value": auto_fix_value,
        "is_anomaly": False,
        "sub_type": sub_type,
        "raw_tool_result": {"rule_id": rule_id, "rows_flagged": total, "rows_checked": n_rows},
        "detail_rows": kept,
    }


# ---------------------------------------------------------------------------
# ACTIVENESS
# ---------------------------------------------------------------------------

def _flag_rules(ctx: _Ctx) -> List[Dict[str, Any]]:
    out = []
    for rule in ctx.pack.get("flags", []) or []:
        col = rule["column"]
        rule_id = f"flag.{ctx.table}.{col}"
        if rule["table"] != ctx.table or col not in ctx.df.columns or not ctx.enabled(rule_id):
            continue
        ctx.covered.add(f"{col} flag set ({rule['label']}) - ACTIVENESS", [col], "ACTIVENESS")
        values = _upper(ctx.df[col])
        hits = ctx.df.index[values.isin(ctx.pack["_flag_values"])]
        if not len(hits):
            continue
        meaning = _dict_label(ctx.dictionary, ctx.table, col) or rule["label"]
        rows = [ctx.row(i, f"{col}='{values[i]}' ({meaning}) - confirm whether this record is in "
                           f"migration scope.") for i in hits]
        out.append(_finding(ctx, rule_id, col, rule["label"],
                            f"{ctx.table}.{col} ({meaning}) is set to one of {sorted(ctx.pack['_flag_values'])}.",
                            rows, "ACTIVENESS", rule.get("severity", "LOW")))
    return out


def _dormancy_rules(ctx: _Ctx) -> List[Dict[str, Any]]:
    out = []
    for rule in ctx.pack.get("dormancy", []) or []:
        rule_id = f"dormant.{ctx.table}"
        date_col = rule["date_column"]
        if rule["table"] != ctx.table or date_col not in ctx.df.columns or not ctx.enabled(rule_id):
            continue
        key = ctx.keys[0]
        children = [(name, ctx.other(name)) for name in rule.get("children", [])]
        children = [(name, c) for name, c in children if c is not None and key in c.columns]
        if not children:
            logger.info("[%s] dormancy rule skipped - none of %s loaded", ctx.table, rule.get("children"))
            continue
        years = int(rule.get("older_than_years", 3))
        today = date.today()
        cutoff = pd.Timestamp(today.replace(year=today.year - years))
        child_names = " or ".join(name for name, _ in children)
        ctx.covered.add(f"dormant {rule['noun']}s: {date_col} older than {years} years and no row in "
                        f"{child_names} - ACTIVENESS", [date_col], "ACTIVENESS")

        created = pd.to_datetime(_text(ctx.df[date_col]), errors="coerce", format="mixed")
        extended: Set[str] = set()
        for _, child in children:
            extended |= set(_text(child[key]))
        keys = _text(ctx.df[key])
        # 1900-01-01 and earlier are SAP initial/placeholder dates, not real creation dates.
        mask = created.notna() & (created > pd.Timestamp("1900-01-01")) & (created < cutoff) & ~keys.isin(extended)
        rows = []
        for i in ctx.df.index[mask]:
            age = today.year - created[i].year
            rows.append(ctx.row(i, f"Created {created[i].date()} ({age} years ago) and never extended to "
                                   f"{child_names} - dormant master data; confirm before migrating."))
        if rows:
            out.append(_finding(ctx, rule_id, date_col, f"Dormant {rule['noun']}s",
                                f"{rule['noun'].capitalize()}s created more than {years} years ago ({date_col}) "
                                f"with no row in {child_names}. {date_col} is a creation date, so this is "
                                f"the evidence available without transaction data.",
                                rows, "ACTIVENESS", rule.get("severity", "MEDIUM")))
    return out


# ---------------------------------------------------------------------------
# COMPLETENESS
# ---------------------------------------------------------------------------

def _mandatory_rules(ctx: _Ctx) -> List[Dict[str, Any]]:
    out = []
    fields = {**((ctx.pack.get("mandatory", {}) or {}).get(ctx.table) or {}), **ctx.extra_mandatory}
    covered_fields: List[str] = []
    for field, spec in fields.items():
        spec = spec or {}
        rule_id = f"mandatory.{ctx.table}.{field}"
        alternatives = [c for c in spec.get("any_of", [field]) if c in ctx.df.columns]
        if not alternatives or not ctx.enabled(rule_id):
            continue
        label = spec.get("label", field)
        covered_fields.append("/".join(alternatives))
        mask = pd.Series(True, index=ctx.df.index)
        for c in alternatives:
            mask &= _blank(ctx.df[c])
        hits = ctx.df.index[mask]
        if not len(hits):
            continue
        which = " and ".join(alternatives)
        auto_value = ctx.auto_fix.get(f"{ctx.table}.{field}")
        fix_hint = (f"agreed default '{auto_value}' can be applied" if auto_value is not None
                    else "value must come from the business")
        rows = [ctx.row(i, f"{label} ({which}) is mandatory in {ctx.table} but blank - {fix_hint}.")
                for i in hits]
        out.append(_finding(ctx, rule_id, field, f"Missing {label.lower()} ({which})",
                            f"{label} ({which}) is a mandatory field in {ctx.table}.",
                            rows, "COMPLETENESS", spec.get("severity", "MEDIUM"),
                            fix_type="AUTO_FIXABLE" if auto_value is not None else "MANUAL_FIX",
                            auto_fix_value=None if auto_value is None else str(auto_value)))
    if covered_fields:
        ctx.covered.add(f"mandatory fields blank: {', '.join(covered_fields)} - COMPLETENESS",
                        [c for alt in covered_fields for c in alt.split("/")], "COMPLETENESS")
    return out


# ---------------------------------------------------------------------------
# CORRECTNESS - country, postal code, tax numbers
# ---------------------------------------------------------------------------

def _country_status(ctx: _Ctx, col: str) -> pd.Series:
    """Upper-cased country per row, '' where blank or not a valid ISO code."""
    values = _upper(ctx.df[col])
    return values.where(values.isin(ctx.pack["_iso"]), "")


def _country_rules(ctx: _Ctx) -> List[Dict[str, Any]]:
    col = (ctx.pack.get("country_columns", {}) or {}).get(ctx.table)
    rule_id = f"country.{ctx.table}"
    if not col or col not in ctx.df.columns or not ctx.enabled(rule_id):
        return []
    ctx.covered.add(f"{col} is a valid ISO 3166-1 alpha-2 country code - CORRECTNESS", [col], "CORRECTNESS",
                    "VALUE_ERROR")
    values = _upper(ctx.df[col])
    mask = (values != "") & ~values.isin(ctx.pack["_iso"])
    aliases = ctx.pack.get("country_aliases", {}) or {}
    rows = []
    for i in ctx.df.index[mask]:
        raw = _text(ctx.df[col])[i]
        hint = f" - probably '{aliases[values[i]]}'" if values[i] in aliases else ""
        rows.append(ctx.row(i, f"{col} '{raw}' is not an ISO 3166-1 alpha-2 country code{hint}."))
    if not rows:
        return []
    return [_finding(ctx, rule_id, col, f"Invalid country key ({col})",
                     f"{ctx.table}.{col} must be an ISO 3166-1 alpha-2 country code; postal and tax "
                     f"formats cannot be checked without one.",
                     rows, "CORRECTNESS", "HIGH", sub_type="VALUE_ERROR", fix_type="MANUAL_FIX")]


def _postal_rules(ctx: _Ctx) -> List[Dict[str, Any]]:
    spec = ((ctx.pack.get("postal", {}) or {}).get("tables") or {}).get(ctx.table)
    rule_id = f"postal.{ctx.table}"
    if not spec or spec["column"] not in ctx.df.columns or spec["country"] not in ctx.df.columns \
            or not ctx.enabled(rule_id):
        return []
    col, country_col = spec["column"], spec["country"]
    formats = ctx.pack["_postal"]
    ctx.covered.add(f"{col} postal code format per country in {country_col} "
                    f"({len(formats)} countries) - CORRECTNESS", [col], "CORRECTNESS", "VALUE_ERROR")
    countries = _country_status(ctx, country_col)
    codes = _upper(ctx.df[col])
    rows = []
    for i in ctx.df.index[(codes != "") & countries.isin(list(formats))]:
        regex, example = formats[countries[i]]
        if not regex.match(codes[i]):
            rows.append(ctx.row(i, f"Postal code '{_text(ctx.df[col])[i]}' does not match the "
                                   f"{countries[i]} format (e.g. '{example}')."))
    if not rows:
        return []
    return [_finding(ctx, rule_id, col, f"Postal code does not fit its country ({col} vs {country_col})",
                     f"{ctx.table}.{col} must match the postal code format of the country in "
                     f"{country_col}; countries without a known format are skipped.",
                     rows, "CORRECTNESS", "MEDIUM", sub_type="VALUE_ERROR", fix_type="MANUAL_FIX")]


def _tax_rules(ctx: _Ctx) -> List[Dict[str, Any]]:
    tax = ctx.pack.get("tax", {}) or {}
    spec = (tax.get("tables") or {}).get(ctx.table)
    if not spec:
        return []
    fields = [f for f in tax.get("fields", []) if f in ctx.df.columns]
    if not fields:
        return []
    country_col = spec.get("country")
    has_country = country_col in ctx.df.columns
    countries = _country_status(ctx, country_col) if has_country else pd.Series("", index=ctx.df.index)
    formats = ctx.pack["_tax_formats"]
    placeholder = ctx.pack["_tax_placeholder"]
    expected = tax.get("expected_field", {}) or {}
    field_list = ", ".join(fields)
    noun = spec.get("noun", "record")
    values = {f: _upper(ctx.df[f]).str.replace(" ", "", regex=False) for f in fields}

    run_missing = ctx.enabled(f"tax.{ctx.table}.missing")
    run_format = has_country and ctx.enabled(f"tax.{ctx.table}.format")
    if run_missing:
        ctx.covered.add(f"tax number present in at least one of {field_list} (composite, never STCD1 "
                        f"alone) - COMPLETENESS", fields, "COMPLETENESS")
    if run_format:
        ctx.covered.add(f"tax number format and placeholders per country in {country_col} across "
                        f"{field_list} ({len(formats)} countries) - CORRECTNESS", fields, "CORRECTNESS",
                        "VALUE_ERROR")

    missing_rows, invalid_rows = [], []
    for i in ctx.df.index:
        filled = [(f, values[f][i]) for f in fields if values[f][i]]
        real = [(f, v) for f, v in filled if not placeholder.match(v)]
        fake = [(f, v) for f, v in filled if placeholder.match(v)]
        country = countries[i]
        if not real:
            if run_missing:
                where = expected.get(country) or (expected.get("EU") if country in ctx.pack["_eu"] else None)
                hint = f"; for {country} SAP expects it in {where}" if where else ""
                found = (f" (only placeholder {', '.join(f'{f}={v!r}' for f, v in fake)})" if fake else "")
                missing_rows.append(ctx.row(i, f"No tax number in any of {field_list}{found}{hint}."))
            continue
        if run_format and country in formats:
            accepted = formats[country]
            for f, v in real:
                if not any(rx.match(v) for _, rx in accepted):
                    names = ", ".join(sorted({name for name, _ in accepted}))
                    invalid_rows.append(ctx.row(i, f"{f} '{v}' matches no {country} tax number format "
                                                   f"({names})."))
    out = []
    if missing_rows:
        out.append(_finding(ctx, f"tax.{ctx.table}.missing", " + ".join(fields),
                            f"No tax number ({field_list})",
                            f"Every {noun} needs a tax number in at least one of {field_list}; which field "
                            f"holds it depends on the country (India: STCD3, EU VAT: STCEG, US EIN: STCD2).",
                            missing_rows, "COMPLETENESS", "HIGH", fix_type="MANUAL_FIX"))
    if invalid_rows:
        out.append(_finding(ctx, f"tax.{ctx.table}.format", " + ".join(fields),
                            f"Tax number does not fit its country ({field_list} vs {country_col})",
                            f"Each filled tax field of a {noun} must match a known tax number format of the "
                            f"country in {country_col}; countries without a known format are only checked for "
                            f"presence.",
                            invalid_rows, "CORRECTNESS", "MEDIUM", sub_type="VALUE_ERROR", fix_type="MANUAL_FIX"))
    return out


# ---------------------------------------------------------------------------
# RELATIONSHIP_INTEGRITY
# ---------------------------------------------------------------------------

def _orphan_rules(ctx: _Ctx) -> List[Dict[str, Any]]:
    out = []
    for rule in ctx.pack.get("relationships", []) or []:
        if rule["child"] != ctx.table:
            continue
        parent_name, join = rule["parent"], rule["join"]
        rule_id = f"orphan.{ctx.table}.{parent_name}"
        parent = ctx.other(parent_name)
        if parent is None:
            logger.info("[%s] orphan check against %s skipped - %s not loaded", ctx.table, parent_name, parent_name)
            continue
        if not all(c in ctx.df.columns for c in join) or not all(p in parent.columns for p in join.values()) \
                or not ctx.enabled(rule_id):
            continue
        child_cols, parent_cols = list(join), list(join.values())
        ctx.covered.add(f"orphan rows: {'+'.join(child_cols)} must exist in {parent_name} - "
                        f"CORRECTNESS/RELATIONSHIP_INTEGRITY", child_cols, "CORRECTNESS", "RELATIONSHIP_INTEGRITY")
        parent_keys = set(_join_keys(parent, parent_cols))
        child_keys = _join_keys(ctx.df, child_cols)
        mask = child_keys.map(lambda k: any(k) and k not in parent_keys)
        rows = []
        for i in ctx.df.index[mask.astype(bool)]:
            ref = ", ".join(f"{c} {v}" for c, v in zip(child_cols, child_keys[i]))
            rows.append(ctx.row(i, f"{ctx.table} row references {ref}, which does not exist in {parent_name}."))
        if rows:
            out.append(_finding(ctx, rule_id, "+".join(child_cols), f"Orphan records (no parent in {parent_name})",
                                f"Every {ctx.table} row must belong to an existing {parent_name} record "
                                f"({', '.join(f'{c}={p}' for c, p in join.items())}).",
                                rows, "CORRECTNESS", "HIGH", sub_type="RELATIONSHIP_INTEGRITY"))
    return out


def _guard_allows(ctx: _Ctx, rule: Dict[str, Any]) -> bool:
    guard = rule.get("guard")
    if not guard:
        return True
    desc = (ctx.dictionary or {}).get((rule["parent"].upper(), guard["column"].upper()))
    if not desc:
        return bool(guard.get("if_undocumented", True))
    allowed = re.search(guard["regex"], desc, re.IGNORECASE) is not None
    if not allowed:
        logger.info("[%s] propagation rule %s skipped - the data dictionary describes %s.%s as %r",
                    ctx.table, rule["id"], rule["parent"], guard["column"], desc)
    return allowed


def _propagation_rules(ctx: _Ctx) -> List[Dict[str, Any]]:
    out = []
    flag_values = ctx.pack["_flag_values"]
    for rule in ctx.pack.get("propagation", []) or []:
        if rule["child"] != ctx.table:
            continue
        rule_id = f"propagation.{rule['id']}"
        parent = ctx.other(rule["parent"])
        join, parent_flag, child_flag = rule["join"], rule["parent_flag"], rule.get("child_flag")
        if parent is None or parent_flag not in parent.columns \
                or not all(c in ctx.df.columns for c in join) or not all(p in parent.columns for p in join.values()) \
                or (child_flag and child_flag not in ctx.df.columns) \
                or not ctx.enabled(rule_id) or not _guard_allows(ctx, rule):
            continue
        child_cols, parent_cols = list(join), list(join.values())
        ctx.covered.add(f"{rule['parent']}.{parent_flag} set but not carried to "
                        f"{ctx.table}{'.' + child_flag if child_flag else ''} - "
                        f"CORRECTNESS/RELATIONSHIP_INTEGRITY", [child_flag] if child_flag else child_cols,
                        "CORRECTNESS", "RELATIONSHIP_INTEGRITY")
        flagged = set(_join_keys(parent, parent_cols)[_upper(parent[parent_flag]).isin(flag_values)])
        mask = _join_keys(ctx.df, child_cols).isin(flagged)
        if child_flag:
            mask &= ~_upper(ctx.df[child_flag]).isin(flag_values)
        rows = [ctx.row(i, f"{rule['text'][0].upper()}{rule['text'][1:]}"
                           f"{f' ({child_flag} is blank)' if child_flag else ''}.")
                for i in ctx.df.index[mask]]
        if rows:
            column = child_flag or "+".join(child_cols)
            out.append(_finding(ctx, rule_id, column,
                                f"{rule['parent']}.{parent_flag} not carried to {ctx.table}",
                                f"When {rule['parent']}.{parent_flag} is set, the {ctx.table} rows of that record "
                                f"must reflect it: {rule['text']}.",
                                rows, "CORRECTNESS", rule.get("severity", "MEDIUM"),
                                sub_type="RELATIONSHIP_INTEGRITY"))
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_FAMILIES = (_flag_rules, _dormancy_rules, _mandatory_rules, _country_rules, _postal_rules, _tax_rules,
             _orphan_rules, _propagation_rules)


def run_sap_rules(table_name: str, df: pd.DataFrame, all_tables: Dict[str, pd.DataFrame],
                  dictionary: Optional[Dict[Tuple[str, str], str]] = None,
                  client_id: Optional[str] = None) -> Tuple[List[Dict[str, Any]], RuleCoverage]:
    """Run every applicable pack rule for one table.

    Returns (findings, coverage): findings in the same shape as the planner's and
    the duplicate detector's, and what ran (with or without hits) - described to
    the planner, and used by graph.py to drop planner checks that repeat a rule.
    """
    if not Config.SAP_RULES_ENABLED or df.empty:
        return [], RuleCoverage()
    ctx = _Ctx(load_pack(), table_name.upper(), df, all_tables, dictionary, client_id)
    findings: List[Dict[str, Any]] = []
    for family in _FAMILIES:
        findings.extend(family(ctx))
    findings.sort(key=lambda f: _SEVERITY_ORDER.index(f["severity"]))

    metrics.sap_rules_evaluated += len(ctx.covered.lines)
    metrics.sap_rule_findings += len(findings)
    metrics.sap_rule_rows += sum(f["raw_tool_result"]["rows_flagged"] for f in findings)
    logger.info("[%s] SAP rules: %d rule group(s) ran, %d finding(s), %d row(s) flagged",
                table_name, len(ctx.covered.lines), len(findings),
                sum(f["raw_tool_result"]["rows_flagged"] for f in findings))
    return findings, ctx.covered
