"""Deterministic statistical and formatting anomalies (Priority 2) - no LLM.

Like ``sap_rules``, every check reads the column mapping (``column_mapping``)
instead of column names, so it runs on any mapped schema:

* AMOUNT / QUANTITY - negative values (unless the mapping allows them), and
  distribution outliers. Business amounts are skewed (0, 100, 250 ... 2 500),
  where a plain 1.5 x IQR fence would flag every 2 500 order; the fence is
  therefore computed on log10 of the positive values, at least
  ``anomalies.outlier_min_fence_decades`` wide, and per currency when the amount
  has a CURRENCY column (100 JPY is not 100 EUR).
* CODE - rare codes: a value used once or twice in a column whose usual codes
  each cover many rows ('XXXX' among payment terms used ~170 times each). A code
  cannot be validated without the customizing tables, so rarity is the evidence.
  Codes are keys, not numbers - ZTERM '0030' is not "30 days" (that lives in
  table T052) - so codes never get a numeric fence.
* CURRENCY - ISO 4217.
* LEGAL_NAME / TEXT / STREET / CITY - illegal characters, unbalanced brackets,
  encoding corruption, HTML entities, stray whitespace, placeholders, and values
  in capitals where the column is otherwise mixed case.
* EMAIL / PHONE - format.

Findings are CORRECTNESS / VALUE_ERROR; statistical ones carry is_anomaly=True.
"""

import math
from typing import Any, Dict, List

import numpy as np
import pandas as pd

from .config import Config
from .rule_context import Ctx, finding, text, upper

_TEXT_CONCEPTS = ("LEGAL_NAME", "TEXT", "STREET", "CITY")
_NAME_CONCEPTS = ("LEGAL_NAME", "TEXT")


def _fmt(x: float) -> str:
    return f"{x:,.0f}" if abs(x) >= 100 else f"{x:,.2f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------
# AMOUNT / QUANTITY
# ---------------------------------------------------------------------------

def _numeric_rules(ctx: Ctx) -> List[Dict[str, Any]]:
    out = []
    for col in ctx.cols("AMOUNT", "QUANTITY"):
        b = ctx.b(col)
        values = pd.to_numeric(text(ctx.df[col]).str.replace(",", "", regex=False), errors="coerce")
        label = ctx.label(col)

        rule_id = f"anomaly.negative.{ctx.table}.{col}"
        if not b["allow_negative"] and ctx.enabled(rule_id):
            hits = ctx.df.index[values < 0]
            if len(hits):
                rows = [ctx.row(i, f"{label} ({col}) is negative: {_fmt(values[i])}.") for i in hits]
                out.append(finding(ctx, rule_id, col, f"Negative {label} ({col})",
                                   f"{ctx.table}.{col} ({label}) must not be negative.",
                                   rows, "CORRECTNESS", "MEDIUM", sub_type="VALUE_ERROR", fix_type="MANUAL_FIX"))

        rule_id = f"anomaly.outlier.{ctx.table}.{col}"
        if not ctx.enabled(rule_id):
            continue
        currency = b["related_column"] if b["concept"] == "AMOUNT" and b["related_column"] in ctx.columns else None
        groups = (upper(ctx.df[currency]) if currency else pd.Series("", index=ctx.df.index))
        ctx.covered.add(f"{col} distribution outliers (log-scale IQR fence"
                        f"{', per ' + currency if currency else ''}) and negatives - CORRECTNESS",
                        [col], "CORRECTNESS", "VALUE_ERROR")
        rows = []
        for group, idx in groups.groupby(groups).groups.items():
            positive = values[idx][values[idx] > 0]
            if len(positive) < Config.ANOMALY_OUTLIER_MIN_SAMPLES:
                continue
            logs = np.log10(positive.astype(float))
            q1, q3 = logs.quantile(0.25), logs.quantile(0.75)
            width = max(Config.ANOMALY_OUTLIER_IQR_MULTIPLIER * (q3 - q1), Config.ANOMALY_OUTLIER_MIN_FENCE_DECADES)
            low, high = q1 - width, q3 + width
            unit = f" {group}" if group else ""
            # Only the high side by default: a tiny weight or stock level is normal, a
            # 99,999,999 order minimum is a typing error.
            outside = (logs > high) | ((logs < low) if Config.ANOMALY_OUTLIER_FLAG_LOW else False)
            for i in positive.index[outside]:
                side = "above" if math.log10(values[i]) > high else "below"
                rows.append(ctx.row(i, f"{label} ({col}) {_fmt(values[i])}{unit} is far {side} the usual range "
                                       f"(middle half: {_fmt(10 ** q1)}-{_fmt(10 ** q3)}{unit}; flagged "
                                       f"{side} {_fmt(10 ** (high if side == 'above' else low))}{unit})."))
        if rows:
            out.append(finding(ctx, rule_id, col, f"Outlier {label} ({col})",
                               f"{ctx.table}.{col} values far outside the column's own distribution "
                               f"(IQR fence on log10 values{', per currency' if currency else ''}).",
                               rows, "CORRECTNESS", "LOW", columns_used=[col] + ([currency] if currency else []),
                               sub_type="VALUE_ERROR", fix_type="MANUAL_FIX", is_anomaly=True))
    return out


# ---------------------------------------------------------------------------
# CODE / CURRENCY
# ---------------------------------------------------------------------------

def _code_rules(ctx: Ctx) -> List[Dict[str, Any]]:
    out = []
    for col in ctx.cols("CODE"):
        rule_id = f"anomaly.rare_code.{ctx.table}.{col}"
        if ctx.b(col)["part_of_key"] or not ctx.enabled(rule_id):
            continue
        values = text(ctx.df[col])
        filled = values[values != ""]
        counts = filled.value_counts()
        if (len(filled) < Config.ANOMALY_RARE_CODE_MIN_ROWS or len(counts) > Config.ANOMALY_RARE_CODE_MAX_DISTINCT
                or counts.median() < Config.ANOMALY_RARE_CODE_MIN_TYPICAL_COUNT):
            continue
        ctx.covered.add(f"{col} rare codes (used at most {Config.ANOMALY_RARE_CODE_MAX_COUNT}x among common codes) - "
                        f"CORRECTNESS", [col], "CORRECTNESS", "VALUE_ERROR")
        limit = max(Config.ANOMALY_RARE_CODE_MAX_COUNT, 0)
        rare = counts[(counts <= limit) & (counts / len(filled) <= Config.ANOMALY_RARE_CODE_MAX_SHARE)]
        if rare.empty:
            continue
        typical = int(counts[counts > limit].median()) if (counts > limit).any() else 0
        label = ctx.label(col)
        rows = [ctx.row(i, f"{label} ({col}) '{values[i]}' appears in {counts[values[i]]} of {len(filled)} rows, "
                           f"while this column's usual codes appear ~{typical} times each - confirm the code exists "
                           f"in customizing.")
                for i in filled.index[filled.isin(rare.index)]]
        out.append(finding(ctx, rule_id, col, f"Rare {label} codes ({col})",
                           f"Codes used only once or twice in {ctx.table}.{col}, where the usual codes each cover many "
                           f"rows, are likely invalid or legacy values.",
                           rows, "CORRECTNESS", "LOW", sub_type="VALUE_ERROR", fix_type="MANUAL_FIX",
                           is_anomaly=True))
    return out


def _currency_rules(ctx: Ctx) -> List[Dict[str, Any]]:
    out = []
    for col in ctx.cols("CURRENCY"):
        rule_id = f"anomaly.currency.{ctx.table}.{col}"
        if not ctx.enabled(rule_id):
            continue
        ctx.covered.add(f"{col} is an ISO 4217 currency code - CORRECTNESS", [col], "CORRECTNESS", "VALUE_ERROR")
        # Case-sensitive: SAP currency keys are upper case, so 'usd' fails on load.
        values = text(ctx.df[col])
        hits = ctx.df.index[(values != "") & ~values.isin(ctx.pack["_currencies"])]
        if len(hits):
            rows = [ctx.row(i, f"{col} '{values[i]}' is not an ISO 4217 currency code"
                               + (f" - probably '{values[i].upper()}'" if values[i].upper() in ctx.pack["_currencies"]
                                  else "") + ".") for i in hits]
            out.append(finding(ctx, rule_id, col, f"Invalid currency key ({col})",
                               f"{ctx.table}.{col} must be an ISO 4217 currency code.",
                               rows, "CORRECTNESS", "HIGH", sub_type="VALUE_ERROR", fix_type="MANUAL_FIX"))
    return out


# ---------------------------------------------------------------------------
# Text, e-mail, phone
# ---------------------------------------------------------------------------

def _text_checks(ctx: Ctx, col: str) -> List[tuple]:
    """(check id, title, detail(value) -> str | None, severity, is_anomaly) for one text column."""
    pack, concept = ctx.pack, ctx.b(col)["concept"]
    illegal, mojibake = pack["_illegal_chars"], pack["_mojibake"]

    def illegal_chars(v):
        # An HTML entity ('&#39;') is reported by its own check - its '#' and ';' are not extra defects.
        found = sorted({ch for ch in pack["_html_entity"].sub("", v) if ch in illegal})
        return f"contains characters not allowed in a legal name: {' '.join(found)}" if found else None

    def brackets(v):
        for o, c in ("()", "[]", "{}"):
            if v.count(o) != v.count(c):
                return f"has unbalanced brackets ('{o}' x{v.count(o)}, '{c}' x{v.count(c)})"
        return None

    def encoding(v):
        return ("contains garbled characters from a wrong encoding conversion"
                if any(m in v for m in mojibake) else None)

    def html(v):
        m = pack["_html_entity"].search(v)
        return f"contains the HTML entity '{m.group(0)}'" if m else None

    def whitespace(v):
        # Trailing blanks alone are not flagged: SAP CHAR fields drop them on save.
        return ("has leading or doubled spaces"
                if v != v.lstrip() or "  " in v.rstrip() or "\t" in v else None)

    def placeholder(v):
        return "is a placeholder, not a real value" if pack["_text_placeholder"].match(v.strip().upper()) else None

    checks = [("encoding", "Encoding corruption", encoding, "MEDIUM", False),
              ("html_entity", "HTML entity in text", html, "LOW", False),
              ("whitespace", "Whitespace issue", whitespace, "LOW", False),
              ("placeholder", "Placeholder value", placeholder, "MEDIUM", False)]
    if concept in _NAME_CONCEPTS:
        checks += [("brackets", "Unbalanced brackets", brackets, "LOW", False)]
    if concept == "LEGAL_NAME":
        checks += [("illegal_chars", "Illegal characters in name", illegal_chars, "MEDIUM", False)]
    if concept in _NAME_CONCEPTS:
        raw = ctx.df[col].dropna().astype(str)
        letters = raw[raw.str.contains(r"[A-Za-z]", regex=True)]
        share = letters.str.contains(r"[a-z]", regex=True).mean() if len(letters) else 0
        if share >= (pack.get("text", {}) or {}).get("mixed_case_share", 0.7):
            def casing(v):
                return ("is written entirely in capitals while the rest of the column is mixed case"
                        if sum(ch.isalpha() for ch in v) >= 4 and v == v.upper() and v != v.lower() else None)
            checks += [("casing", "Inconsistent casing", casing, "LOW", True)]
    return checks


def _text_rules(ctx: Ctx) -> List[Dict[str, Any]]:
    out = []
    for col in ctx.cols(*_TEXT_CONCEPTS):
        raw = ctx.df[col]
        present = raw.notna() & (raw.astype(str).str.strip() != "")
        covered = []
        for check, title, test, severity, is_anomaly in _text_checks(ctx, col):
            rule_id = f"text.{check}.{ctx.table}.{col}"
            if not ctx.enabled(rule_id):
                continue
            covered.append(check.replace("_", " "))
            rows = []
            for i in ctx.df.index[present]:
                problem = test(str(raw[i]))
                if problem:
                    rows.append(ctx.row(i, f"{col} '{raw[i]}' {problem}."))
            if rows:
                out.append(finding(ctx, rule_id, col, f"{title} ({col})",
                                   f"{ctx.table}.{col} ({ctx.label(col)}): {title.lower()}.",
                                   rows, "CORRECTNESS", severity, sub_type="VALUE_ERROR", fix_type="MANUAL_FIX",
                                   is_anomaly=is_anomaly))
        if covered:
            ctx.covered.add(f"{col} text hygiene ({', '.join(covered)}) - CORRECTNESS", [col],
                            "CORRECTNESS", "VALUE_ERROR")
    return out


def _contact_rules(ctx: Ctx) -> List[Dict[str, Any]]:
    out = []
    min_digits = int((ctx.pack.get("text", {}) or {}).get("phone_min_digits", 6))
    for concept, regex_key, what in (("EMAIL", "_email", "e-mail address"), ("PHONE", "_phone", "phone number")):
        for col in ctx.cols(concept):
            rule_id = f"text.{concept.lower()}.{ctx.table}.{col}"
            if not ctx.enabled(rule_id):
                continue
            ctx.covered.add(f"{col} {what} format - CORRECTNESS", [col], "CORRECTNESS", "VALUE_ERROR")
            values = text(ctx.df[col])
            rows = []
            for i in ctx.df.index[values != ""]:
                v = values[i]
                bad = not ctx.pack[regex_key].match(v)
                if concept == "PHONE" and not bad:
                    bad = sum(ch.isdigit() for ch in v) < min_digits
                if bad:
                    rows.append(ctx.row(i, f"{col} '{v}' is not a valid {what}."))
            if rows:
                out.append(finding(ctx, rule_id, col, f"Invalid {what} ({col})",
                                   f"{ctx.table}.{col} must hold a well-formed {what}.",
                                   rows, "CORRECTNESS", "MEDIUM", sub_type="VALUE_ERROR", fix_type="MANUAL_FIX"))
    return out


FAMILIES = (_numeric_rules, _code_rules, _currency_rules, _text_rules, _contact_rules)
