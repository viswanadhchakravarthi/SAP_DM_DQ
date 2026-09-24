"""Structural profile - the handoff to the Mapping / Value Mapping Agent (Priority 4).

What every source column LOOKS like, with no need to know what it means: the
input a Mapping Agent needs to map source fields to SAP fields (types, lengths,
value shapes and regexes) and a Value Mapping Agent needs to map source codes to
SAP codes (the complete distinct values of code columns, with counts). Written
per run as ``contracts.StructuralProfile`` JSON (``sap-dm.structural-profile``),
validated against the contract before it is saved:

    <handoff.dir>/<client_id>/<run_id>/structural_profile.json
    <handoff.dir>/<client_id>/structural_profile.latest.json

and served by the review app (``/api/clients/{id}/handoff/structural-profile``).

Privacy: this file leaves the agent, and a downstream agent may send it to an
LLM, so distinct values are listed ONLY for CATEGORICAL / FLAG columns that are
not personal. Names, addresses, contact data, tax and bank numbers, and any
column whose values (nearly) all differ, get shapes and statistics only.
Sensitivity comes from the column mapping when there is one, plus content
checks (e-mail / IBAN / phone shapes) that work without it. Pure pandas, no LLM.
"""

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .config import Config
from .contracts import (CONTRACT_VERSION, Privacy, Producer, StructuralProfile)
from .logging_config import get_logger

logger = get_logger("structural_profile")

# Concepts (column_mapping) whose values are personal or identifying: never listed.
PERSONAL_CONCEPTS = {"LEGAL_NAME", "STREET", "CITY", "POSTAL_CODE", "EMAIL", "PHONE", "TAX_ID", "SEARCH_TERM", "TEXT"}
_NUMERIC_DECL = re.compile(r"^(CURR|QUAN|DEC|FLTP|INT\d*|INTEGER|NUMBER|DECIMAL|FLOAT)", re.I)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_IBAN = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{10,30}$")
# A phone number has a '+' or separators between digit groups; a bare run of digits
# (vendor numbers, G/L accounts, bank accounts) or a date does not count.
_PHONE = re.compile(r"^(?:\+|00)?\(?\d{1,4}\)?(?:[\s\-/.]\(?\d{1,5}\)?){2,}$")
_INT = re.compile(r"^-?\d+$")
_DEC = re.compile(r"^-?\d+[.,]\d+$")
_DATEISH = re.compile(r"^\d{4}-\d{2}-\d{2}|^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$|^\d{8}$")


# ---------------------------------------------------------------------------
# Shapes and regexes
# ---------------------------------------------------------------------------

def _char_class(ch: str) -> str:
    if "A" <= ch <= "Z":
        return "A"
    if "a" <= ch <= "z":
        return "a"
    if ch.isdigit():
        return "9"
    if ch.isalpha():
        return "L"          # any other letter (Ä, ł, ...)
    if ch.isspace():
        return " "
    return ch


_CLASS_REGEX = {"A": "[A-Z]", "a": "[a-z]", "9": "[0-9]", "L": r"[^\W\d_]", " ": r"\s"}


def shape_runs(value: str) -> Tuple[Tuple[str, int], ...]:
    runs: List[List[Any]] = []
    for ch in value:
        c = _char_class(ch)
        if runs and runs[-1][0] == c:
            runs[-1][1] += 1
        else:
            runs.append([c, 1])
    return tuple((c, n) for c, n in runs)


def runs_to_shape(runs) -> str:
    return "".join(c if n == 1 else f"{c}{{{n}}}" for c, n in runs)


def runs_to_regex(runs) -> str:
    parts = []
    for c, n in runs:
        piece = _CLASS_REGEX.get(c, re.escape(c))
        parts.append(piece if n == 1 else f"{piece}{{{n}}}")
    return "^" + "".join(parts) + "$"


# ---------------------------------------------------------------------------
# One column
# ---------------------------------------------------------------------------

def _detected_type(series: pd.Series, filled: pd.Series) -> Tuple[str, bool]:
    """(detected type, leading zeros) - from the loaded dtype, else from the values."""
    if filled.empty:
        return "empty", False
    if pd.api.types.is_numeric_dtype(series):
        numbers = pd.to_numeric(series, errors="coerce").dropna()
        return ("integer" if (numbers == numbers.round()).all() else "decimal"), False
    sample = filled
    if sample.str.match(_INT).all():
        # Zero-padded numbers are codes (LIFNR '0000070061'): keep them strings.
        if sample.str.match(r"^-?0\d").any():
            return "string", True
        return "integer", False
    if sample.str.match(_DEC).all():
        return "decimal", False
    if sample.str.match(_DATEISH).mean() >= 0.95 and \
            pd.to_datetime(sample, errors="coerce", format="mixed").notna().mean() >= 0.95:
        return "date", False
    return "string", False


def _value_class(detected: str, numeric_dtype: bool, declared: str, stats: Dict[str, Any]) -> str:
    if stats["filled"] == 0:
        return "EMPTY"
    if detected == "date":
        return "DATE"
    if numeric_dtype or (declared and _NUMERIC_DECL.match(declared)):
        return "NUMERIC"
    if stats["distinct"] <= 3 and (stats["max_length"] or 0) <= 2:
        return "FLAG"
    if stats["distinct_pct"] >= 90 and stats["filled"] >= 20:
        return "IDENTIFIER"
    if stats["distinct"] <= Config.HANDOFF_MAX_DOMAIN_VALUES and stats["distinct_pct"] <= 50 \
            and (stats["avg_length"] or 0) <= 20:
        return "CATEGORICAL"
    return "FREE_TEXT"


def _content_personal(filled: pd.Series) -> Optional[str]:
    """Personal-data shapes recognisable without a mapping."""
    if filled.empty:
        return None
    sample = filled.head(500)
    for regex, what in ((_EMAIL, "e-mail addresses"), (_IBAN, "IBANs")):
        if sample.str.match(regex).mean() >= 0.5:
            return what
    phones = sample.str.match(_PHONE) & ~sample.str.match(_DATEISH) & (sample.str.count(r"\d") >= 7)
    if phones.mean() >= 0.5:
        return "phone numbers"
    return None


def profile_column(series: pd.Series, position: int, dictionary_entry: Optional[Dict[str, str]],
                   binding: Optional[Dict[str, Any]], hint: Optional[Dict[str, Any]],
                   value_map: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    raw = series.dropna().astype(str)
    filled = raw[raw.str.strip() != ""]
    rows = len(series)
    counts = filled.value_counts()
    lengths = filled.str.len()
    detected, leading_zeros = _detected_type(series, filled)
    numeric_dtype = pd.api.types.is_numeric_dtype(series)
    declared = (dictionary_entry or {}).get("data_type") or ""

    stats: Dict[str, Any] = {
        "rows": rows,
        "filled": int(len(filled)),
        "null_pct": round(100 * (rows - len(filled)) / rows, 2) if rows else 0.0,
        "distinct": int(len(counts)),
        "distinct_pct": round(100 * len(counts) / len(filled), 2) if len(filled) else 0.0,
        "unique": bool(len(filled) == rows and len(counts) == rows and rows > 0),
        "min_length": int(lengths.min()) if len(filled) else None,
        "max_length": int(lengths.max()) if len(filled) else None,
        "avg_length": round(float(lengths.mean()), 2) if len(filled) else None,
        "leading_zeros": leading_zeros,
    }
    if detected in ("integer", "decimal") and len(filled):
        numbers = pd.to_numeric(filled.str.replace(",", ".", regex=False), errors="coerce").dropna()
        if len(numbers):
            stats["min"], stats["max"] = str(numbers.min()), str(numbers.max())
        if detected == "decimal":
            stats["decimals"] = int(filled.str.extract(r"[.,](\d+)$")[0].str.len().max() or 0)
    elif detected == "date":
        dates = pd.to_datetime(filled, errors="coerce", format="mixed").dropna()
        if len(dates):
            stats["min"], stats["max"] = str(dates.min().date()), str(dates.max().date())

    # Shapes, computed once per distinct value and weighted by its count.
    shape_counts: Counter = Counter()
    for value, n in counts.items():
        shape_counts[shape_runs(value)] += int(n)
    total = max(len(filled), 1)
    top = shape_counts.most_common(5)
    patterns = [{"shape": runs_to_shape(r), "regex": runs_to_regex(r), "count": n,
                 "pct": round(100 * n / total, 2)} for r, n in top]
    regex, coverage, covered, chosen = None, None, 0, []
    for r, n in top:
        chosen.append(r)
        covered += n
        if covered / total >= 0.95:
            regex = runs_to_regex(chosen[0]) if len(chosen) == 1 else \
                "^(?:" + "|".join(runs_to_regex(c)[1:-1] for c in chosen) + ")$"
            coverage = round(100 * covered / total, 2)
            break

    value_class = _value_class(detected, numeric_dtype, declared, stats)
    concept = (binding or {}).get("concept")
    personal_reason = None
    if concept in PERSONAL_CONCEPTS:
        personal_reason = f"mapped as {concept}"
    else:
        what = _content_personal(filled)
        if what:
            personal_reason = f"values look like {what}"
    sensitivity = "PERSONAL" if personal_reason else "NONE"

    if value_class in ("CATEGORICAL", "FLAG") and sensitivity == "NONE":
        domain = {"listed": True, "values": [
            {"value": v, "count": int(n), "pct": round(100 * n / total, 2)} for v, n in counts.items()]}
        allowed = set((binding or {}).get("allowed_values") or [])
        if allowed:
            # Against the SAP target domain: what the Value Mapping Agent still has to convert.
            vmap = value_map or {}
            for item in domain["values"]:
                v = item["value"].strip()
                if v in allowed:
                    item["target_status"] = "VALID"
                elif vmap.get(v) in allowed:
                    item["target_status"], item["target_value"] = "MAPPED", vmap[v]
                else:
                    item["target_status"] = "UNMAPPED"
            domain.update(target=binding.get("domain_target"), check_table=binding.get("check_table"),
                          unmapped_count=sum(1 for i in domain["values"] if i["target_status"] == "UNMAPPED"))
    else:
        reason = (f"personal data ({personal_reason})" if sensitivity == "PERSONAL"
                  else {"IDENTIFIER": "identifier - nearly every value differs",
                        "FREE_TEXT": "free text", "NUMERIC": "numeric measure - see min/max",
                        "DATE": "date - see min/max", "EMPTY": "no values",
                        "CATEGORICAL": f"more than {Config.HANDOFF_MAX_DOMAIN_VALUES} distinct values"}
                  .get(value_class, value_class.lower()))
        domain = {"listed": False, "values": [], "withheld_reason": reason}

    column: Dict[str, Any] = {
        "column": str(series.name),
        "position": position,
        "dictionary": dictionary_entry,
        "detected_type": detected,
        "value_class": value_class,
        "sensitivity": sensitivity,
        "stats": stats,
        "patterns": patterns,
        "regex": regex,
        "regex_coverage_pct": coverage,
        "domain": domain,
    }
    if hint:
        column["semantic_hint"] = hint
    return column


# ---------------------------------------------------------------------------
# Whole upload
# ---------------------------------------------------------------------------

def _hint(table: str, col: str, mapping: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not mapping:
        return None
    binding = mapping["columns"].get(col)
    if not binding or binding["concept"] == "OTHER":
        return None
    source = mapping.get("source", "")
    sap_field = binding.get("target") or (f"{table}.{col}" if source.startswith("sap-standard") else None)
    hint = {"concept": binding["concept"], "sap_field": sap_field, "source": source}
    if binding.get("targets") and len(binding["targets"]) > 1:
        hint["sap_fields"] = binding["targets"]
    return hint


def build_profile(tables: Dict[str, pd.DataFrame], table_files: Dict[str, str],
                  structured_dictionary: Dict[str, Dict[str, str]], mappings: Dict[str, Dict[str, Any]],
                  client: Dict[str, str], run_id: Optional[str]) -> Dict[str, Any]:
    out_tables = []
    for table, df in tables.items():
        mapping = mappings.get(table)
        columns = []
        for pos, col in enumerate(df.columns, 1):
            entry = structured_dictionary.get(f"{table}.{str(col).upper()}")
            columns.append(profile_column(df[col], pos, entry or None,
                                          (mapping or {}).get("columns", {}).get(col), _hint(table, col, mapping),
                                          ((mapping or {}).get("value_maps") or {}).get(col)))
        declared = [c for c, b in ((mapping or {}).get("columns", {})).items() if b["part_of_key"] and c in df.columns]
        out_tables.append({
            "table": table,
            "source_file": table_files.get(table, f"{table}.csv"),
            "row_count": len(df),
            "column_count": len(df.columns),
            "key_candidates": [[c["column"]] for c in columns if c["stats"]["unique"]],
            "declared_key": declared or None,
            "declared_key_unique": (not df.duplicated(subset=declared).any()) if declared else None,
            "columns": columns,
        })
    doc = {
        "contract": "sap-dm.structural-profile",
        "version": CONTRACT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "producer": Producer(agent="data-profiling-agent", run_id=run_id, client_id=client["client_id"],
                             client_name=client.get("name")).model_dump(),
        "privacy": Privacy(
            domain_policy=("Distinct values are listed only for CATEGORICAL and FLAG columns that are not "
                           "personal. Personal columns (by mapping or by content) and identifiers, free text, "
                           "numbers and dates carry shapes and statistics only."),
            max_domain_values=Config.HANDOFF_MAX_DOMAIN_VALUES,
            withheld_classes=sorted(PERSONAL_CONCEPTS) + ["IDENTIFIER", "FREE_TEXT", "NUMERIC", "DATE"],
        ).model_dump(),
        "tables": out_tables,
    }
    StructuralProfile.model_validate(doc)   # never hand off a document that breaks the contract
    return doc


def write_profile(doc: Dict[str, Any]) -> Path:
    client_id, run_id = doc["producer"]["client_id"], doc["producer"].get("run_id") or "adhoc"
    base = Path(Config.HANDOFF_DIR) / client_id
    run_path = base / run_id / "structural_profile.json"
    run_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(doc, indent=2, ensure_ascii=False, default=str)
    run_path.write_text(text, encoding="utf-8")
    (base / "structural_profile.latest.json").write_text(text, encoding="utf-8")
    listed = sum(1 for t in doc["tables"] for c in t["columns"] if c["domain"]["listed"])
    total = sum(len(t["columns"]) for t in doc["tables"])
    logger.info("Structural profile written: %s (%d tables, %d columns, distinct values listed for %d)",
                run_path, len(doc["tables"]), total, listed)
    return run_path


def profile_path(client_id: str, run_id: Optional[str] = None) -> Path:
    base = Path(Config.HANDOFF_DIR) / client_id
    return base / run_id / "structural_profile.json" if run_id else base / "structural_profile.latest.json"
