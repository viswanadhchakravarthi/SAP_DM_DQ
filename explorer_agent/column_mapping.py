"""What each column MEANS - the layer that lets fixed rules run on any schema.

The deterministic engines (``sap_rules``, ``anomaly_rules``) are written against
business concepts - KEY, DELETION_FLAG, COUNTRY, POSTAL_CODE, TAX_ID, AMOUNT ...
(``schemas.ColumnConcept``) - never against column names. This module decides,
once per client and table layout, which column plays which concept:

1. A mapping saved for THIS client whose schema signature still fits (the
   normal case from the second run on, and where a human correction lives -
   the file is plain JSON under ``<memory.clients_dir>/<client>/column_mappings.json``).
2. The SAP-standard mapping from the rule pack (``standard_columns``) when every
   uploaded column of the table is a known standard field - free, no LLM. The
   client's data dictionary can still override an attribute there (e.g. SPERM
   documented as a posting block instead of the standard purchasing block).
3. Otherwise ONE LLM call per table maps it from metadata only - column names,
   the client's dictionary text, aggregate statistics, masked examples and, for
   tiny flag-like columns, their 1-2 character codes (e.g. 'X', 'Y') - and the
   result is saved, so later runs are free.
4. If no LLM is available or every model failed: the standard fields that ARE
   known are still mapped, the rest stay OTHER, and the rules that need them are
   skipped rather than guessed.

Same pattern as the duplicate-rule planner (``duplicate_rule_planner``), which
maps columns to matching roles; this one feeds the rule engines instead.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from langchain_core.messages import HumanMessage, SystemMessage

from .client_knowledge import read_json, write_json_atomic
from .config import Config
from .duplicate_rule_planner import column_metadata
from .logging_config import get_logger
from .memory.duplicate_rule_store import schema_signature
from .metrics import metrics
from .schemas import COLUMN_CONCEPTS, ColumnMappingPlan

logger = get_logger("column_mapping")

_ATTRIBUTES = ("concept", "part_of_key", "references", "related_column", "required", "required_group",
               "flag_set_values", "block_type", "cascades", "allow_negative", "reason")

SYSTEM_PROMPT = """You are a data migration analyst preparing a client's table for automated data-quality \
rules. The rules are written against business CONCEPTS, not column names, so your job is to say what \
every column MEANS. You never see raw data: you get the client's own data dictionary, aggregate \
statistics and masked example values (only the last two characters are visible), plus the names and \
columns of the other tables in the same upload.

Map EVERY column to exactly one concept (definitions are in the schema) and fill the attributes that \
apply to it.

How to decide:
1. The client's DICTIONARY DESCRIPTION outranks the column name. Standard fields are repurposed and custom \
   fields mean whatever their owner decided; if the description contradicts the name, follow the \
   description and say so in the reason. The dictionary may name tables of the SOURCE system that are \
   listed here under other names - use value_overlap to tell which listed table is meant.
2. Use the statistics as evidence: a flag has 1-2 distinct short values and is mostly empty; a key is \
   highly distinct; an amount is numeric; a code repeats a small set of values. A column with many distinct \
   values is never a flag.
3. Concept details that are easy to get wrong:
   - the date a record was CREATED is CREATED_DATE (not DATE);
   - the name of a vendor, customer, partner or account holder is LEGAL_NAME (TEXT is only for descriptions);
   - company code, purchasing / sales organisation, plant, storage location are ORG_UNIT (not CODE);
   - bank keys, account numbers and IBANs are OTHER unless they identify a row (then part_of_key).
4. part_of_key: mark the columns that together identify one row of THIS table.
5. references: set it for EVERY column holding the key of another listed table ('TABLE.COLUMN' exactly as \
   listed). value_overlap gives the share of this column's distinct values found in a unique column of \
   another table - near 100% is strong evidence. Orphan checks run on it, so leave it empty when unsure.
6. required: true for the fields a record cannot be migrated or used without - typically the partner's name \
   and country, the organisational unit of an org-level row, the reconciliation account, the order currency, \
   the account group. Optional contact or descriptive fields are false.
7. related_column: the COUNTRY column that decides a POSTAL_CODE's or TAX_ID's format, or the CURRENCY \
   column an AMOUNT is expressed in - only columns of this table.
8. Flags: always fill flag_set_values (from short_code_values or the dictionary), block_type for a \
   BLOCK_FLAG, and cascades=true for a flag on a MASTER table (one that other tables reference) whose \
   child tables carry their own flag of the same kind - set it on the master's flag, never on the child's.
9. One sentence of reason per column that a human auditor can check - cite the dictionary text or statistic.

Return only the mapping. Do not invent columns and do not repeat any example value back."""


# ---------------------------------------------------------------------------
# Bindings
# ---------------------------------------------------------------------------

def _binding(raw: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """One column's binding with every attribute present (defaults filled)."""
    concept = str(raw.get("concept") or "OTHER").upper()
    return {
        "concept": concept if concept in COLUMN_CONCEPTS else "OTHER",
        "part_of_key": bool(raw.get("part_of_key", False)),
        "references": raw.get("references") or None,
        "related_column": raw.get("related_column") or None,
        "required": bool(raw.get("required", False)),
        "required_group": raw.get("required_group") or None,
        "flag_set_values": [str(v) for v in raw.get("flag_set_values") or []] or None,
        "block_type": raw.get("block_type") or None,
        "cascades": bool(raw.get("cascades", False)),
        "allow_negative": bool(raw.get("allow_negative", False)),
        "reason": raw.get("reason") or reason,
    }


def _mapping(table: str, columns: Dict[str, Dict[str, Any]], source: str, detail: str,
             notes: Optional[str] = None) -> Dict[str, Any]:
    return {"table": table, "columns": columns, "source": source, "source_detail": detail, "notes": notes}


def standard_mapping(table: str, df: pd.DataFrame, pack: Dict[str, Any],
                     dictionary: Optional[Dict[Tuple[str, str], str]] = None,
                     partial: bool = False) -> Optional[Dict[str, Any]]:
    """The rule pack's SAP-standard mapping, or None when a column is not standard.

    With ``partial=True`` the known columns are mapped and the rest become OTHER
    (the no-LLM fallback)."""
    known = (pack.get("standard_columns", {}) or {}).get(table) or {}
    unknown = [c for c in df.columns if c not in known]
    if not known or (unknown and not partial):
        return None
    columns = {}
    for col in df.columns:
        if col not in known:
            columns[col] = _binding({"concept": "OTHER"}, "not a standard field of this table - left unmapped")
            continue
        spec = dict(known[col])
        reason = f"SAP-standard {table}-{col} (rule pack)"
        desc = (dictionary or {}).get((table.upper(), col.upper()))
        for override in spec.pop("dictionary_override", None) or []:
            if desc and re.search(override["regex"], desc, re.IGNORECASE):
                spec.update(override["set"])
                reason += (f"; the client's dictionary describes it as {desc.split(' - ')[0]!r}, so "
                           + ", ".join(f"{k}={v}" for k, v in override["set"].items()))
        columns[col] = _binding(spec, reason)
    source = "sap-standard" if not unknown else "sap-standard-partial"
    detail = ("mapped from the SAP-standard fields in the rule pack (no LLM)" if not unknown else
              f"only the SAP-standard fields are mapped; {unknown} are left unmapped (no LLM was available)")
    return _mapping(table, columns, source, detail)


# ---------------------------------------------------------------------------
# LLM mapping (metadata only)
# ---------------------------------------------------------------------------

def _short_codes(series: pd.Series) -> Optional[List[str]]:
    """The values of a flag-like column: at most 5 distinct values of 1-2 characters.
    Such codes ('X', 'Y', '01') carry no personal data, and without them the model
    cannot say which value means 'set'."""
    values = series.dropna().astype(str).str.strip()
    values = values[values != ""].unique()
    if 0 < len(values) <= 5 and all(len(v) <= 2 for v in values):
        return sorted(values.tolist())
    return None


def _distinct_filled(series: pd.Series) -> set:
    values = series.dropna().astype(str).str.strip()
    return set(values[values != ""])


def value_overlap(table: str, df: pd.DataFrame, tables: Dict[str, pd.DataFrame],
                  minimum: float = 0.5) -> Dict[str, Dict[str, float]]:
    """{column: {'OTHER_TABLE.COLUMN': share}} - the share of a column's distinct values
    found in a UNIQUE column of another table (a candidate parent key).

    Aggregate arithmetic only: no value leaves this function. It is the evidence
    for `references`, which legacy dictionaries often describe with the source
    system's table names ("foreign key to LFA1") rather than the uploaded ones."""
    parents = {}
    for other, odf in tables.items():
        if other == table:
            continue
        for col in odf.columns:
            filled = odf[col].dropna().astype(str).str.strip()
            filled = filled[filled != ""]
            if len(filled) >= 0.95 * len(odf) and filled.nunique() >= 0.95 * len(filled) and filled.nunique() >= 10:
                parents[f"{other}.{col}"] = set(filled)
    result: Dict[str, Dict[str, float]] = {}
    for col in df.columns:
        own = _distinct_filled(df[col])
        if len(own) < 10:
            continue
        # A parent covers its children: only a column with MORE distinct values can be
        # the parent. Without this, two 1:1 child tables (one bank row and one
        # purchasing row per vendor) look like each other's parents, and the master
        # looks like a child of both.
        shares = {ref: round(len(own & keys) / len(own), 3) for ref, keys in parents.items() if len(keys) > len(own)}
        shares = {ref: share for ref, share in shares.items() if share >= minimum}
        if shares:
            result[col] = dict(sorted(shares.items(), key=lambda kv: -kv[1])[:3])
    return result


def build_prompt(table: str, df: pd.DataFrame, other_tables: Dict[str, List[str]],
                 dictionary: Optional[Dict[Tuple[str, str], str]] = None,
                 client_name: Optional[str] = None,
                 overlap: Optional[Dict[str, Dict[str, float]]] = None) -> List[Any]:
    metadata = column_metadata(table, df, dictionary)
    for entry in metadata:
        codes = _short_codes(df[entry["column"]])
        if codes:
            entry["short_code_values"] = codes
        if overlap and entry["column"] in overlap:
            entry["value_overlap"] = overlap[entry["column"]]
    lines = [f"Client: {client_name or '(not given)'}", f"Table: {table}", f"Rows: {len(df)}"]
    note = Config.SAP_TABLE_DESCRIPTIONS.get(table)
    if note:
        lines.append(f"Table description: {note}")
    lines += [
        "",
        "Other tables in this upload (name -> columns), for `references`:",
        json.dumps(other_tables, default=str),
        "",
        "Columns (dictionary text from the client, plus statistics; example values are masked - only the "
        "last two characters are real):",
        json.dumps(metadata, indent=2, default=str),
        "",
        f"Return one entry for each of the {len(df.columns)} columns.",
    ]
    return [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content="\n".join(lines))]


def compile_plan(table: str, plan: ColumnMappingPlan, df: pd.DataFrame,
                 other_tables: Dict[str, List[str]],
                 overlap: Optional[Dict[str, Dict[str, float]]] = None) -> Dict[str, Dict[str, Any]]:
    """Validate the model's answer against the real schema: unknown columns are
    dropped, and references / related columns that do not exist are cleared.
    Then the arithmetic safety net (like the duplicate planner's distinct-value
    check): what the data itself rules out is corrected, and a reference the
    model left empty is filled only when >= 90% of the column's values exist in
    exactly one best candidate parent. Every correction is written into the reason."""
    known = {str(c).upper(): c for c in df.columns}
    columns: Dict[str, Dict[str, Any]] = {}
    for item in plan.columns:
        col = known.get(str(item.column).strip().upper())
        if col is None or col in columns:
            if col is None:
                logger.warning("[%s] mapping names unknown column %r - ignored", table, item.column)
            continue
        binding = _binding(item.model_dump(), "")
        ref = binding["references"]
        if ref:
            ref_table, _, ref_col = str(ref).partition(".")
            target = {c.upper(): c for c in other_tables.get(ref_table.upper(), [])}
            if ref_col.upper() in target:
                binding["references"] = f"{ref_table.upper()}.{target[ref_col.upper()]}"
            else:
                logger.warning("[%s] %s references %r, which is not in the upload - cleared", table, col, ref)
                binding["references"] = None
        rel = binding["related_column"]
        if rel:
            binding["related_column"] = known.get(str(rel).upper())
        columns[col] = binding
    for col in df.columns:  # a column the model skipped is left unmapped, never guessed
        columns.setdefault(col, _binding({"concept": "OTHER"}, "not mapped by the model"))

    for col, b in columns.items():
        distinct = _distinct_filled(df[col])
        if b["concept"] in ("DELETION_FLAG", "BLOCK_FLAG"):
            if len(distinct) > 5:
                b["reason"] = (f"OTHER - the model called it a {b['concept']}, but it has {len(distinct)} "
                               f"distinct values, so it cannot be a flag. (The model said: {b['reason']})")
                b.update(concept="OTHER", flag_set_values=None, block_type=None, cascades=False)
            elif not b["flag_set_values"] and len(distinct) == 1:
                b["flag_set_values"] = sorted(distinct)
                b["reason"] += f" [set value taken from the data: the column's only value]"
        candidates = (overlap or {}).get(col, {})
        if not b["references"] and candidates:
            best, share = next(iter(candidates.items()))
            if share >= 0.9 and not best.startswith(f"{table}."):
                b["references"] = best
                b["reason"] += f" [references {best}: {share:.0%} of its values exist there]"
    return columns


# ---------------------------------------------------------------------------
# Store - <memory.clients_dir>/<client_id>/column_mappings.json
# ---------------------------------------------------------------------------

def _path(client_id: str) -> Path:
    return Path(Config.CLIENT_KNOWLEDGE_DIR) / client_id / "column_mappings.json"


def load_saved(table: str, columns, client_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not client_id:
        return None
    entry = read_json(_path(client_id), {}).get("tables", {}).get(table)
    if not entry:
        return None
    if entry.get("signature") != schema_signature(columns):
        logger.info("[%s] saved column mapping is for a different column set - the schema changed", table)
        return None
    created = str(entry.get("created_at", ""))[:10]
    return _mapping(table, {c: _binding(b, "") for c, b in entry["columns"].items()}, "client-memory",
                    f"reused from this client's memory (mapped by {entry.get('created_by', 'an LLM')} on "
                    f"{created}; edit column_mappings.json to correct it)", entry.get("notes"))


def save(table: str, mapping: Dict[str, Any], columns, created_by: str, client_id: Optional[str]) -> None:
    if not client_id:
        return
    path = _path(client_id)
    data = read_json(path, {"version": 1, "tables": {}})
    data.setdefault("tables", {})[table] = {
        "table": table,
        "signature": schema_signature(columns),
        "columns": mapping["columns"],
        "notes": mapping.get("notes"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": created_by,
    }
    write_json_atomic(path, data)
    logger.info("[%s] column mapping saved for client %s (%s)", table, client_id, path.name)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

def resolve_mappings(tables: Dict[str, pd.DataFrame], pack: Dict[str, Any],
                     dictionary: Optional[Dict[Tuple[str, str], str]] = None,
                     client_id: Optional[str] = None, client_name: Optional[str] = None,
                     planner=None) -> Dict[str, Dict[str, Any]]:
    """{table: mapping} for every loaded table. Runs before any rule, because
    rules on one table read the mapping of others (orphans, dormancy)."""
    other_columns = {t: [str(c) for c in df.columns] for t, df in tables.items()}
    mappings: Dict[str, Dict[str, Any]] = {}
    llm_mapped = []
    for table, df in tables.items():
        saved = load_saved(table, df.columns, client_id)
        if saved:
            metrics.column_mapping_hits += 1
            mappings[table] = saved
            continue
        standard = standard_mapping(table, df, pack, dictionary)
        if standard:
            metrics.column_mapping_standard += 1
            mappings[table] = standard
            continue
        mappings[table] = _plan(table, df, pack, dictionary, client_id, client_name, planner,
                                {t: c for t, c in other_columns.items() if t != table}, tables)
        if mappings[table]["source"] == "llm":
            llm_mapped.append(table)
        logger.info("[%s] column mapping (%s): %s", table, mappings[table]["source"], describe(mappings[table]))
    if llm_mapped:
        _apply_structure(mappings, llm_mapped, client_id)
    return mappings


_NEVER_KEY = ("AMOUNT", "QUANTITY", "CURRENCY", "DELETION_FLAG", "BLOCK_FLAG", "TEXT", "LEGAL_NAME",
              "EMAIL", "PHONE", "CREATED_DATE")


def _apply_structure(mappings: Dict[str, Dict[str, Any]], llm_mapped: List[str], client_id: Optional[str]) -> None:
    """Corrections that follow from the structure of the whole upload, which a
    per-table LLM call cannot see. Only freshly LLM-drafted mappings are touched
    (the SAP pack states its attributes explicitly, and a saved mapping may carry
    a human's edits), and each change is written into the column's reason so a
    reviewer can see and undo it:

    * a value, flag or free-text column is never part of a row key;
    * `cascades` only means something on a MASTER (a table others reference); on
      a child it is cleared, and a master's DELETION_FLAG cascades by default -
      rows of a record marked for deletion are not migrated either.
    """
    referenced = {b["references"].partition(".")[0]
                  for m in mappings.values() for b in m["columns"].values() if b["references"]}
    for table in llm_mapped:
        changed = False
        for b in mappings[table]["columns"].values():
            if b["part_of_key"] and b["concept"] in _NEVER_KEY:
                b["part_of_key"] = False
                b["reason"] += f" [not part of the row key: a {b['concept']} does not identify a row]"
                changed = True
            if b["concept"] not in ("DELETION_FLAG", "BLOCK_FLAG"):
                continue
            if table in referenced and b["concept"] == "DELETION_FLAG" and not b["cascades"]:
                b["cascades"] = True
                b["reason"] += (" [cascades: default for a deletion flag on a table other tables reference - "
                                "edit to false if child rows may stay active]")
                changed = True
            elif table not in referenced and b["cascades"]:
                b["cascades"] = False
                b["reason"] += " [cascades cleared: no other table references this one]"
                changed = True
        if changed and client_id:
            data = read_json(_path(client_id), {})
            entry = data.get("tables", {}).get(table)
            if entry:
                entry["columns"] = mappings[table]["columns"]
                write_json_atomic(_path(client_id), data)


def _plan(table, df, pack, dictionary, client_id, client_name, planner, other_tables,
          all_tables) -> Dict[str, Any]:
    fallback = (standard_mapping(table, df, pack, dictionary, partial=True)
                or _mapping(table, {c: _binding({"concept": "OTHER"}, "unmapped") for c in df.columns},
                            "unmapped", "no mapping available (no LLM) - rules that need a concept are skipped"))
    if planner is None:
        logger.warning("[%s] no LLM configured to map this non-standard table - %s", table,
                       fallback["source_detail"])
        return fallback
    logger.info("[%s] not a known SAP-standard layout - asking the LLM to map its columns", table)
    overlap = value_overlap(table, df, all_tables)
    try:
        plan: ColumnMappingPlan = planner.invoke(build_prompt(table, df, other_tables, dictionary, client_name,
                                                              overlap))
    except Exception as exc:
        metrics.column_mapping_failures += 1
        logger.error("[%s] the LLM could not map the columns (%s) - %s", table,
                     str(exc).splitlines()[0][:300], fallback["source_detail"])
        return fallback
    metrics.column_mapping_llm_calls += 1
    mapping = _mapping(table, compile_plan(table, plan, df, other_tables, overlap), "llm",
                       f"mapped now by {planner.label} from the data dictionary and column statistics, and "
                       f"saved - later runs on this schema reuse it", plan.notes)
    save(table, mapping, df.columns, planner.label, client_id)
    return mapping


def describe(mapping: Dict[str, Any]) -> str:
    """'COL=CONCEPT' for every mapped (non-OTHER) column - for logs."""
    return ", ".join(f"{c}={b['concept']}" + (f"->{b['references']}" if b["references"] else "")
                     for c, b in mapping["columns"].items() if b["concept"] != "OTHER")
