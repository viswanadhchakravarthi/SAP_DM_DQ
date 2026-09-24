"""Deterministic SAP domain rules - known standards, checked without an LLM.

The planner LLM used to re-invent even textbook SAP checks (deletion flags,
mandatory fields, orphaned LFB1 rows, which tax field holds an Indian GSTIN) on
every run, with different code and different coverage each time. Those checks
now live here and run before the planner at zero LLM cost; the planner is told
what already ran, and graph.py drops planner checks that repeat them.

The rules are written against business CONCEPTS from the table's column mapping
(``column_mapping``), never against column names, so they run on SAP-standard
extracts (mapped for free from the rule pack) and on any other schema (mapped
once by the LLM and saved). A rule whose concepts are not mapped is skipped.

Rule families (one finding per rule and table, with every offending row):

* ACTIVENESS   - DELETION_FLAG / BLOCK_FLAG set; dormancy = CREATED_DATE older
                 than N years AND no row in any child table that extends the
                 record to an ORG_UNIT.
* COMPLETENESS - required columns blank (MANUAL_FIX unless the client agreed a
                 default in config.yaml).
* CORRECTNESS  - invalid ISO COUNTRY; POSTAL_CODE against its country's format;
                 TAX_ID columns evaluated together, by country.
* RELATIONSHIP_INTEGRITY - orphan rows (a `references` value missing in the
                 referenced table), and cascading master flags not carried to
                 the child rows.

The statistical and formatting anomalies (Priority 2) are in ``anomaly_rules``;
``run_sap_rules`` runs both. Row-level output goes only to the local SQLite store
and the review UI - never to an LLM. Only rule descriptions reach the planner.
"""

from collections import defaultdict
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from . import anomaly_rules
from .config import Config
from .logging_config import get_logger
from .metrics import metrics
from .rule_context import (SEVERITY_ORDER, Ctx, RuleCoverage, blank, finding, join_keys, load_pack, text,
                           upper)

logger = get_logger("sap_rules")

__all__ = ["RuleCoverage", "load_pack", "run_sap_rules"]

_MANDATORY_SEVERITY = {"SEARCH_TERM": "LOW", "CITY": "MEDIUM", "POSTAL_CODE": "MEDIUM", "STREET": "MEDIUM"}


def _set_values(ctx: Ctx, binding: Dict[str, Any]) -> set:
    return {str(v).upper() for v in (binding.get("flag_set_values") or ctx.pack["_flag_values"])}


def _flag_label(binding: Dict[str, Any]) -> str:
    if binding["concept"] == "DELETION_FLAG":
        return "Deletion flag set"
    kind = (binding.get("block_type") or "GENERAL").lower()
    return "Central block set" if kind == "general" else f"{kind.capitalize()} block set"


def _own_key(columns: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """The column identifying the object a table is about: a KEY in the row key that
    points nowhere else (LFA1.LIFNR - not LFB1.LIFNR, which references LFA1)."""
    for col, b in columns.items():
        if b["concept"] == "KEY" and b["part_of_key"] and not b["references"]:
            return col
    return None


def _parents(ctx: Ctx) -> Dict[str, List[Tuple[str, str]]]:
    """{parent table: [(child column, parent column)]} from this table's references."""
    parents: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    for col, b in ctx.columns.items():
        if b["references"]:
            parent, _, parent_col = b["references"].partition(".")
            parents[parent].append((col, parent_col))
    return parents


# ---------------------------------------------------------------------------
# ACTIVENESS
# ---------------------------------------------------------------------------

def _flag_rules(ctx: Ctx) -> List[Dict[str, Any]]:
    out = []
    for col in ctx.cols("DELETION_FLAG", "BLOCK_FLAG"):
        rule_id = f"flag.{ctx.table}.{col}"
        if not ctx.enabled(rule_id):
            continue
        binding, label = ctx.b(col), _flag_label(ctx.b(col))
        values = upper(ctx.df[col])
        set_values = _set_values(ctx, binding)
        ctx.covered.add(f"{col} ({label.lower()}) - ACTIVENESS", [col], "ACTIVENESS")
        hits = ctx.df.index[values.isin(set_values)]
        if not len(hits):
            continue
        meaning = ctx.label(col)
        rows = [ctx.row(i, f"{col}='{values[i]}' ({meaning}) - confirm whether this record is in "
                           f"migration scope.") for i in hits]
        out.append(finding(ctx, rule_id, col, label,
                           f"{ctx.table}.{col} ({meaning}) is set to one of {sorted(set_values)}.",
                           rows, "ACTIVENESS", "LOW"))
    return out


def _dormancy_rules(ctx: Ctx) -> List[Dict[str, Any]]:
    dates, key = ctx.cols("CREATED_DATE"), _own_key(ctx.columns)
    rule_id = f"dormant.{ctx.table}"
    if not dates or not key or not ctx.enabled(rule_id):
        return []
    date_col = dates[0]
    # Children that extend this record to an organisational level: they point at our
    # key and carry an ORG_UNIT in their own row key (LFB1/BUKRS, LFM1/EKORG).
    children = []
    for name, cols in ctx.mappings.items():
        if name == ctx.table or ctx.other(name) is None:
            continue
        refs = [c for c, b in cols["columns"].items() if b["references"] == f"{ctx.table}.{key}"]
        extends = any(b["concept"] == "ORG_UNIT" and b["part_of_key"] for b in cols["columns"].values())
        if refs and extends and refs[0] in ctx.other(name).columns:
            children.append((name, refs[0]))
    if not children:
        logger.info("[%s] dormancy rule skipped - no organisational-level child table loaded", ctx.table)
        return []
    years = int(ctx.pack.get("dormant_after_years", 3))
    today = date.today()
    cutoff = pd.Timestamp(today.replace(year=today.year - years))
    child_names = " or ".join(name for name, _ in children)
    ctx.covered.add(f"dormant records: {date_col} older than {years} years and no row in {child_names} - "
                    f"ACTIVENESS", [date_col], "ACTIVENESS")

    created = pd.to_datetime(text(ctx.df[date_col]), errors="coerce", format="mixed")
    extended = set()
    for name, ref in children:
        extended |= set(text(ctx.other(name)[ref]))
    keys = text(ctx.df[key])
    # 1900-01-01 and earlier are SAP initial/placeholder dates, not real creation dates.
    mask = created.notna() & (created > pd.Timestamp("1900-01-01")) & (created < cutoff) & ~keys.isin(extended)
    rows = [ctx.row(i, f"Created {created[i].date()} ({today.year - created[i].year} years ago) and never "
                       f"extended to {child_names} - dormant master data; confirm before migrating.")
            for i in ctx.df.index[mask]]
    if not rows:
        return []
    return [finding(ctx, rule_id, date_col, "Dormant records",
                    f"Records created more than {years} years ago ({date_col}) with no row in {child_names}. "
                    f"{date_col} is a creation date, so this is the evidence available without transaction data.",
                    rows, "ACTIVENESS", "MEDIUM", columns_used=[date_col, key])]


# ---------------------------------------------------------------------------
# COMPLETENESS
# ---------------------------------------------------------------------------

def _mandatory_rules(ctx: Ctx) -> List[Dict[str, Any]]:
    groups: Dict[str, List[str]] = {}
    for col, b in ctx.columns.items():
        if b["required"] and b["concept"] != "TAX_ID":  # tax numbers: see _tax_rules
            groups.setdefault(b["required_group"] or f"__{col}", []).append(col)
    out, covered = [], []
    for members in groups.values():
        first = members[0]
        rule_id = f"mandatory.{ctx.table}.{first}"
        if not ctx.enabled(rule_id):
            continue
        covered.append("/".join(members))
        mask = pd.Series(True, index=ctx.df.index)
        for c in members:
            mask &= blank(ctx.df[c])
        hits = ctx.df.index[mask]
        if not len(hits):
            continue
        which = " and ".join(members)
        label = " or ".join(ctx.label(c) for c in members)
        auto_value = ctx.auto_fix.get(f"{ctx.table}.{first}")
        fix_hint = (f"agreed default '{auto_value}' can be applied" if auto_value is not None
                    else "value must come from the business")
        severity = ctx.severity_override.get(first) or _MANDATORY_SEVERITY.get(ctx.b(first)["concept"], "HIGH")
        rows = [ctx.row(i, f"{label} ({which}) is mandatory in {ctx.table} but blank - {fix_hint}.") for i in hits]
        out.append(finding(ctx, rule_id, first, f"Missing {label} ({which})",
                           f"{label} ({which}) is a mandatory field in {ctx.table}.",
                           rows, "COMPLETENESS", severity, columns_used=members,
                           fix_type="AUTO_FIXABLE" if auto_value is not None else "MANUAL_FIX",
                           auto_fix_value=None if auto_value is None else str(auto_value)))
    if covered:
        ctx.covered.add(f"mandatory fields blank: {', '.join(covered)} - COMPLETENESS",
                        [c for g in covered for c in g.split("/")], "COMPLETENESS")
    return out


# ---------------------------------------------------------------------------
# CORRECTNESS - country, postal code, tax numbers
# ---------------------------------------------------------------------------

def _valid_country(ctx: Ctx, col: str) -> pd.Series:
    """Upper-cased country per row, '' where blank or not a valid ISO code."""
    values = upper(ctx.df[col])
    return values.where(values.isin(ctx.pack["_iso"]), "")


def _country_rules(ctx: Ctx) -> List[Dict[str, Any]]:
    out = []
    aliases = ctx.pack.get("country_aliases", {}) or {}
    for col in ctx.cols("COUNTRY"):
        rule_id = f"country.{ctx.table}.{col}"
        if not ctx.enabled(rule_id):
            continue
        ctx.covered.add(f"{col} is a valid ISO 3166-1 alpha-2 country code - CORRECTNESS", [col],
                        "CORRECTNESS", "VALUE_ERROR")
        # Case-sensitive: SAP country keys are upper case, so 'de' fails on load.
        values = text(ctx.df[col])
        rows = []
        for i in ctx.df.index[(values != "") & ~values.isin(ctx.pack["_iso"])]:
            v = values[i].upper()
            hint = (f" - probably '{aliases[v]}'" if v in aliases
                    else f" - probably '{v}'" if v in ctx.pack["_iso"] else "")
            rows.append(ctx.row(i, f"{col} '{text(ctx.df[col])[i]}' is not an ISO 3166-1 alpha-2 country "
                                   f"code{hint}."))
        if rows:
            out.append(finding(ctx, rule_id, col, f"Invalid country key ({col})",
                               f"{ctx.table}.{col} must be an ISO 3166-1 alpha-2 country code; postal and tax "
                               f"formats cannot be checked without one.",
                               rows, "CORRECTNESS", "HIGH", sub_type="VALUE_ERROR", fix_type="MANUAL_FIX"))
    return out


def _postal_rules(ctx: Ctx) -> List[Dict[str, Any]]:
    out = []
    formats = ctx.pack["_postal"]
    for col in ctx.cols("POSTAL_CODE"):
        country_col = ctx.country_for(col)
        rule_id = f"postal.{ctx.table}.{col}"
        if not country_col or not ctx.enabled(rule_id):
            continue
        ctx.covered.add(f"{col} postal code format per country in {country_col} ({len(formats)} countries) - "
                        f"CORRECTNESS", [col], "CORRECTNESS", "VALUE_ERROR")
        countries = _valid_country(ctx, country_col)
        codes = upper(ctx.df[col])
        rows = []
        for i in ctx.df.index[(codes != "") & countries.isin(list(formats))]:
            regex, example = formats[countries[i]]
            if not regex.match(codes[i]):
                rows.append(ctx.row(i, f"Postal code '{text(ctx.df[col])[i]}' does not match the "
                                       f"{countries[i]} format (e.g. '{example}')."))
        if rows:
            out.append(finding(ctx, rule_id, col, f"Postal code does not fit its country ({col} vs {country_col})",
                               f"{ctx.table}.{col} must match the postal code format of the country in "
                               f"{country_col}; countries without a known format are skipped.",
                               rows, "CORRECTNESS", "MEDIUM", columns_used=[col, country_col],
                               sub_type="VALUE_ERROR", fix_type="MANUAL_FIX"))
    return out


def _tax_rules(ctx: Ctx) -> List[Dict[str, Any]]:
    fields = ctx.cols("TAX_ID")
    if not fields:
        return []
    tax = ctx.pack.get("tax", {}) or {}
    country_col = ctx.country_for(fields[0])
    countries = (_valid_country(ctx, country_col) if country_col
                 else pd.Series("", index=ctx.df.index))
    formats, placeholder = ctx.pack["_tax_formats"], ctx.pack["_tax_placeholder"]
    expected = tax.get("expected_field", {}) or {}
    field_list = ", ".join(fields)
    values = {f: upper(ctx.df[f]).str.replace(" ", "", regex=False) for f in fields}

    run_missing = any(ctx.b(f)["required"] for f in fields) and ctx.enabled(f"tax.{ctx.table}.missing")
    run_format = bool(country_col) and ctx.enabled(f"tax.{ctx.table}.format")
    if run_missing:
        ctx.covered.add(f"tax number present in at least one of {field_list} (evaluated together, never one "
                        f"field alone) - COMPLETENESS", fields, "COMPLETENESS")
    if run_format:
        ctx.covered.add(f"tax number format and placeholders per country in {country_col} across {field_list} "
                        f"({len(formats)} countries) - CORRECTNESS", fields, "CORRECTNESS", "VALUE_ERROR")

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
                found = f" (only placeholder {', '.join(f'{f}={v!r}' for f, v in fake)})" if fake else ""
                missing_rows.append(ctx.row(i, f"No tax number in any of {field_list}{found}{hint}."))
            continue
        if run_format and country in formats:
            accepted = formats[country]
            for f, v in real:
                if not any(rx.match(v) for _, rx in accepted):
                    names = ", ".join(sorted({name for name, _ in accepted}))
                    invalid_rows.append(ctx.row(i, f"{f} '{v}' matches no {country} tax number format ({names})."))
    out = []
    used = fields + ([country_col] if country_col else [])
    if missing_rows:
        out.append(finding(ctx, f"tax.{ctx.table}.missing", " + ".join(fields), f"No tax number ({field_list})",
                           f"Every record needs a tax number in at least one of {field_list}; which field holds "
                           f"it depends on the country (India: STCD3, EU VAT: STCEG, US EIN: STCD2).",
                           missing_rows, "COMPLETENESS", "HIGH", columns_used=used, fix_type="MANUAL_FIX"))
    if invalid_rows:
        out.append(finding(ctx, f"tax.{ctx.table}.format", " + ".join(fields),
                           f"Tax number does not fit its country ({field_list} vs {country_col})",
                           f"Each filled tax field must match a known tax number format of the country in "
                           f"{country_col}; countries without a known format are only checked for presence.",
                           invalid_rows, "CORRECTNESS", "MEDIUM", columns_used=used,
                           sub_type="VALUE_ERROR", fix_type="MANUAL_FIX"))
    return out


def _domain_rules(ctx: Ctx) -> List[Dict[str, Any]]:
    """Values outside the SAP target domain (check table from the Metadata Repository,
    sap-dm.target-domains). Checked after value mapping, so a mapped 'UK' is not reported.
    Where a domain exists it replaces the statistical rare-code check (anomaly_rules)."""
    out = []
    for col, b in ctx.columns.items():
        allowed = b.get("allowed_values")
        rule_id = f"domain.{ctx.table}.{col}"
        if not allowed or not ctx.enabled(rule_id):
            continue
        target, check_table = b.get("domain_target"), b.get("check_table")
        where = f"{target}" + (f" (check table {check_table})" if check_table else "")
        ctx.covered.add(f"{col} value exists in the SAP domain of {where} - CORRECTNESS", [col],
                        "CORRECTNESS", "VALUE_ERROR")
        values = text(ctx.df[col])
        allowed_set = set(allowed)
        hits = ctx.df.index[(values != "") & ~values.isin(allowed_set)]
        if not len(hits):
            continue
        rows = [ctx.row(i, f"{col} '{values[i]}' is not a valid {where} value - map it (Value Mapping) or "
                           f"correct it.") for i in hits]
        out.append(finding(ctx, rule_id, col, f"Invalid code ({col} not in {check_table or target})",
                           f"Every {ctx.table}.{col} value must exist in the target domain of {where}.",
                           rows, "CORRECTNESS", "HIGH", sub_type="VALUE_ERROR", fix_type="MANUAL_FIX"))
    return out


# ---------------------------------------------------------------------------
# RELATIONSHIP_INTEGRITY
# ---------------------------------------------------------------------------

def _orphan_rules(ctx: Ctx) -> List[Dict[str, Any]]:
    out = []
    for parent_name, pairs in _parents(ctx).items():
        rule_id = f"orphan.{ctx.table}.{parent_name}"
        parent = ctx.other(parent_name)
        if parent is None:
            logger.info("[%s] orphan check against %s skipped - %s not loaded", ctx.table, parent_name, parent_name)
            continue
        if not all(p in parent.columns for _, p in pairs) or not ctx.enabled(rule_id):
            continue
        child_cols, parent_cols = [c for c, _ in pairs], [p for _, p in pairs]
        ctx.covered.add(f"orphan rows: {'+'.join(child_cols)} must exist in {parent_name} - "
                        f"CORRECTNESS/RELATIONSHIP_INTEGRITY", child_cols, "CORRECTNESS", "RELATIONSHIP_INTEGRITY")
        parent_keys = set(join_keys(parent, parent_cols))
        child_keys = join_keys(ctx.df, child_cols)
        mask = child_keys.map(lambda k: any(k) and k not in parent_keys).astype(bool)
        rows = []
        for i in ctx.df.index[mask]:
            ref = ", ".join(f"{c} {v}" for c, v in zip(child_cols, child_keys[i]))
            rows.append(ctx.row(i, f"{ctx.table} row references {ref}, which does not exist in {parent_name}."))
        if rows:
            out.append(finding(ctx, rule_id, "+".join(child_cols), f"Orphan records (no parent in {parent_name})",
                               f"Every {ctx.table} row must belong to an existing {parent_name} record "
                               f"({', '.join(f'{c}={parent_name}.{p}' for c, p in pairs)}).",
                               rows, "CORRECTNESS", "HIGH", sub_type="RELATIONSHIP_INTEGRITY"))
    return out


def _propagation_rules(ctx: Ctx) -> List[Dict[str, Any]]:
    """A cascading master flag (central deletion flag, central block) must be set on
    this table's rows too, when this table has a flag of the same kind."""
    out = []
    for parent_name, pairs in _parents(ctx).items():
        parent = ctx.other(parent_name)
        parent_cols_map = ctx.other_columns(parent_name)
        if parent is None or not all(p in parent.columns for _, p in pairs):
            continue
        for p_flag, pb in parent_cols_map.items():
            if pb["concept"] not in ("DELETION_FLAG", "BLOCK_FLAG") or not pb["cascades"] or p_flag not in parent.columns:
                continue
            matches = [c for c in ctx.cols(pb["concept"])
                       if pb["concept"] == "DELETION_FLAG" or ctx.b(c).get("block_type") == pb.get("block_type")]
            for c_flag in matches:
                rule_id = f"propagation.{ctx.table}.{c_flag}"
                if not ctx.enabled(rule_id):
                    continue
                child_cols, parent_cols = [c for c, _ in pairs], [p for _, p in pairs]
                what = _flag_label(pb).lower().replace(" set", "")
                ctx.covered.add(f"{parent_name}.{p_flag} ({what}) set but not carried to {ctx.table}.{c_flag} - "
                                f"CORRECTNESS/RELATIONSHIP_INTEGRITY", [c_flag], "CORRECTNESS",
                                "RELATIONSHIP_INTEGRITY")
                p_set = _set_values(ctx, pb)
                flagged = set(join_keys(parent, parent_cols)[upper(parent[p_flag]).isin(p_set)])
                mask = join_keys(ctx.df, child_cols).isin(flagged) & ~upper(ctx.df[c_flag]).isin(_set_values(ctx, ctx.b(c_flag)))
                rows = [ctx.row(i, f"{parent_name}.{p_flag} ({what}) is set on the {parent_name} record, but "
                                   f"{c_flag} on this {ctx.table} row is not.") for i in ctx.df.index[mask]]
                if rows:
                    out.append(finding(ctx, rule_id, c_flag, f"{parent_name}.{p_flag} not carried to {ctx.table}",
                                       f"A {what} set on {parent_name} ({p_flag}) applies to all of that record's "
                                       f"{ctx.table} rows, so {c_flag} must be set there too.",
                                       rows, "CORRECTNESS", "MEDIUM", columns_used=[c_flag] + child_cols,
                                       sub_type="RELATIONSHIP_INTEGRITY"))
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_FAMILIES = (_flag_rules, _dormancy_rules, _mandatory_rules, _country_rules, _postal_rules, _tax_rules,
             _domain_rules, _orphan_rules, _propagation_rules)


def run_sap_rules(table_name: str, df: pd.DataFrame, all_tables: Dict[str, pd.DataFrame],
                  dictionary: Optional[Dict[Tuple[str, str], str]] = None,
                  client_id: Optional[str] = None,
                  mappings: Optional[Dict[str, Dict[str, Any]]] = None) -> Tuple[List[Dict[str, Any]], RuleCoverage]:
    """Run the SAP domain rules and the anomaly rules for one table.

    ``mappings`` is ``column_mapping.resolve_mappings(...)`` for every loaded table.
    Returns (findings, coverage): findings in the same shape as the planner's, and
    what ran (with or without hits) - described to the planner, and used by
    graph.py to drop planner checks that repeat a rule.
    """
    if not Config.SAP_RULES_ENABLED or df.empty:
        return [], RuleCoverage()
    # Callers pass tables already translated by column_mapping.apply_value_maps, so the
    # rules - including cross-table ones - check the values the records will have in SAP.
    ctx = Ctx(load_pack(), table_name.upper(), df, all_tables, dictionary, client_id, mappings)
    families = list(_FAMILIES) + (list(anomaly_rules.FAMILIES) if Config.ANOMALIES_ENABLED else [])
    findings: List[Dict[str, Any]] = []
    for family in families:
        findings.extend(family(ctx))
    findings.sort(key=lambda f: SEVERITY_ORDER.index(f["severity"]))

    flagged = sum(f["raw_tool_result"]["rows_flagged"] for f in findings)
    metrics.sap_rules_evaluated += len(ctx.covered.lines)
    metrics.sap_rule_findings += len(findings)
    metrics.sap_rule_rows += flagged
    metrics.anomaly_findings += sum(1 for f in findings if f["raw_tool_result"]["rule_id"].startswith(("anomaly.", "text.")))
    logger.info("[%s] deterministic rules (mapping: %s): %d rule group(s) ran, %d finding(s), %d row(s) flagged",
                table_name, ctx.mapping_source, len(ctx.covered.lines), len(findings), flagged)
    return findings, ctx.covered
