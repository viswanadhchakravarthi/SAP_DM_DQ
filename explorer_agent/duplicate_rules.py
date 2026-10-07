"""Where a table's duplicate-matching rules come from.

The rules themselves are written by the LLM, once (``duplicate_rule_planner``),
and kept (``memory.duplicate_rule_store``). This module is only the resolver
that decides, per table and per run, which of those sources to use:

1. ``duplicates.tables.<TABLE>`` in config.yaml - an explicit human override,
   the escape hatch when someone wants to pin a table's rules by hand.
2. Rules saved for THIS client whose schema signature still fits the uploaded
   data - the normal case from the second run onwards, zero LLM calls.
3. Rules shared as UNIVERSAL / INDUSTRY_SPECIFIC knowledge, only when
   ``duplicates.rules.reuse_across_clients`` is on and the layout matches
   exactly (see the store's docstring for why this is off by default).
4. Otherwise the LLM drafts them from the client's data dictionary and
   privacy-sanitized column statistics, and the result is saved for next time.

If step 4 can't run (no LLM configured for a ``--duplicates-only`` run, or every
model in the chain failed), the table falls back to identical-row detection
only: comparing whole rows needs no business knowledge, so it can't be wrong,
while inventing role assignments in Python would be a guess about columns whose
meaning differs from client to client.

Earlier versions inferred the roles here with regular expressions over column
names and dictionary text. That was removed: it assumed a (TABLE, COLUMN) pair
means the same thing everywhere, which is exactly what is not true once fields
are repurposed or custom.
"""

from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from . import duplicate_rule_planner
from .config import Config
from .duplicate_rule_planner import build_checks
from .logging_config import get_logger
from .memory import duplicate_rule_store
from .metrics import metrics

logger = get_logger("duplicate_rules")

_FALLBACK_DISPLAY_COLUMNS = 8


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def _from_config(table: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    rules = {
        "key": _as_list(raw.get("key")),
        "key_unique": bool(raw.get("key_unique", False)),
        "name": raw.get("name"),
        "identifiers": [_as_list(i) for i in raw.get("identifiers", [])],
        "location": _as_list(raw.get("location")),
        "display": _as_list(raw.get("display")),
        "label": raw.get("label", "records"),
        "rule_scope": raw.get("rule_scope", "CLIENT_SPECIFIC"),
        "industry": raw.get("industry"),
        "notes": raw.get("notes"),
        "why": {},
        "source": "config.yaml",
        "source_detail": f"pinned by hand in config.yaml (duplicates.tables.{table})",
    }
    rules["checks"] = build_checks(rules)
    return rules


def _identical_rows_only(df: pd.DataFrame, reason: str) -> Dict[str, Any]:
    """No rules available: still catch rows where every column is equal."""
    rules = {
        "key": [], "key_unique": False, "name": None, "identifiers": [], "location": [],
        "display": [str(c) for c in df.columns[:_FALLBACK_DISPLAY_COLUMNS]],
        "label": "records", "rule_scope": None, "industry": None, "notes": None, "why": {},
        "source": "identical-rows-only",
        "source_detail": (f"no matching rules are available ({reason}), so only rows where EVERY "
                          f"column is equal are reported"),
    }
    rules["checks"] = ["identical rows"]
    return rules


# Concepts that identify a real-world entity on their own (a shared one links two records).
_IDENTIFIER_CONCEPTS = ("TAX_ID", "IBAN", "EMAIL", "PHONE")
_LOCATION_CONCEPTS = ("POSTAL_CODE", "CITY", "STREET")
# A column mapping that is only a best-effort fallback says nothing about meaning.
_UNUSABLE_MAPPING_SOURCES = ("unmapped", "sap-standard-partial")
_MAX_DISPLAY = 10


def rules_from_mapping(table: str, df: pd.DataFrame, mapping: Optional[Dict[str, Any]],
                       labels: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    """Matching rules built from what the columns MEAN (the column mapping), with no LLM call.

    The mapping already says which column is a tax number, e-mail, phone, IBAN,
    bank account, name or address part - for SAP-standard layouts from the rule
    pack, for anything else from the one-time mapping call. Building the rules
    from it means no identifier can be forgotten the way a free-form duplicate
    planner sometimes did (STCD2-STCD5, STCEG, bank details). Returns None when
    there is no usable mapping, and the caller falls back to the LLM planner.
    """
    if not mapping or mapping.get("source") in _UNUSABLE_MAPPING_SOURCES:
        return None
    bindings = {c: b for c, b in mapping["columns"].items() if c in df.columns}
    if not any(b["concept"] != "OTHER" for b in bindings.values()):
        return None

    def of(*concepts: str) -> List[str]:
        return [c for c, b in bindings.items() if b["concept"] in concepts]

    key = [c for c in of("KEY") if bindings[c]["part_of_key"]]
    key_parts = [c for c, b in bindings.items() if b["part_of_key"]]
    # A key must be unique only on a master table: every key part is a KEY and none points elsewhere.
    key_unique = bool(key) and all(bindings[c]["concept"] == "KEY" and not bindings[c]["references"]
                                   for c in key_parts)

    why: Dict[str, str] = {}
    groups: Dict[str, List[str]] = {}
    for column in of(*_IDENTIFIER_CONCEPTS):
        groups[f"__{column}"] = [column]
        why[column] = f"identifier - {bindings[column]['concept']}: {bindings[column]['reason']}"
    accounts = of("BANK_ACCOUNT")
    if accounts:
        # An account number is only unique together with its bank: country + bank key + account.
        parts = [c for c in of("COUNTRY") if bindings[c]["part_of_key"]] + of("BANK_KEY") + accounts
        groups["__bank"] = parts
        for column in parts:
            why[column] = (f"identifier (together with {', '.join(p for p in parts if p != column)}) - "
                           f"{bindings[column]['concept']}: {bindings[column]['reason']}")

    names = of("LEGAL_NAME")
    name = next((c for c in names if bindings[c]["required"]), names[0] if names else None)
    location = of(*_LOCATION_CONCEPTS)
    if name:
        why[name] = (f"name - {bindings[name]['reason']}" if location else
                     f"name - NOT used for matching: this table has no location columns to confirm a name match.")
    for column in location:
        why[column] = f"location - {bindings[column]['concept']}: {bindings[column]['reason']}"
    for column in key:
        why[column] = f"key - {bindings[column]['reason']}"

    # No distinct-share guard here (the LLM planner needs one because it can mislabel a column): the
    # mapping says what the column IS, and the detector already ignores a value shared by more than
    # duplicates.max_identifier_share records, so a default e-mail or "TBD" cannot link anything.
    identifiers = [cols for cols in groups.values() if cols]
    shown = [c for c in [*([name] if name else []), *names, *location, *of("COUNTRY"),
                         *[c for cols in identifiers for c in cols]] if c not in key]
    display = list(dict.fromkeys(shown))[:_MAX_DISPLAY] or [c for c in df.columns if c not in key][:8]
    rules = {
        "key": key, "key_unique": key_unique,
        "name": name if location else None,
        "identifiers": identifiers, "location": location, "display": display,
        "label": (labels or {}).get(table, "records"),
        "rule_scope": "UNIVERSAL" if str(mapping.get("source", "")).startswith("sap-standard") else "CLIENT_SPECIFIC",
        "industry": None, "notes": None, "why": why,
        "source": "concept-mapping",
        "source_detail": (f"built from the column mapping ({mapping.get('source')}): every column mapped to "
                          f"TAX_ID, IBAN, bank account, e-mail or phone is an identifier, the name is matched "
                          f"fuzzily and confirmed by postal code / city / street - no LLM call"),
    }
    rules["checks"] = build_checks(rules)
    return rules


def resolve_rules(table: str, df: pd.DataFrame,
                  dictionary: Optional[Dict[Tuple[str, str], str]] = None,
                  client_id: Optional[str] = None, client_name: Optional[str] = None,
                  rule_planner: Optional["duplicate_rule_planner.RulePlanner"] = None,
                  mapping: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The matching rules for one table - from config, from the column mapping, from memory, or drafted."""
    raw = Config.DUPLICATE_TABLE_RULES.get(table)
    if raw:
        return _from_config(table, raw)

    if Config.DUPLICATE_RULES_FROM_MAPPING:
        from .rule_context import load_pack  # local import: keeps this module free of the rule engines
        concept_rules = rules_from_mapping(table, df, mapping, load_pack().get("duplicate_labels"))
        if concept_rules:
            metrics.duplicate_rule_concept_hits += 1
            return concept_rules

    saved = duplicate_rule_store.load_rules(table, df.columns, client_id, client_name)
    if saved:
        rules, source, detail = saved
        rules["checks"] = build_checks(rules)
        rules["source"], rules["source_detail"] = source, detail
        if source == "client-memory":
            duplicate_rule_store.record_reuse(table, client_id)
        metrics.duplicate_rule_hits += 1
        logger.info("[%s] %s - no LLM call needed", table, detail)
        return rules

    metrics.duplicate_rule_misses += 1
    if rule_planner is None:
        return _identical_rows_only(df, "no LLM is configured for this run to draft them")

    try:
        rules = duplicate_rule_planner.plan_rules(table, df, rule_planner, dictionary, client_name)
    except Exception as exc:
        logger.error("[%s] the LLM could not draft duplicate rules (%s) - falling back to identical "
                     "rows only for this table", table, str(exc).splitlines()[0][:300])
        return _identical_rows_only(df, "the LLM could not draft them for this table")

    duplicate_rule_store.save_rules(table, df.columns, rules,
                                    created_by=rule_planner.label, client_id=client_id)
    owner = f"{client_name}'s" if client_name else "the client's"
    rules["source"] = "llm"
    rules["source_detail"] = (
        f"drafted now by {rule_planner.label} from {owner} data dictionary and column statistics, "
        f"and saved - later runs on this schema reuse it without an LLM call")
    return rules
