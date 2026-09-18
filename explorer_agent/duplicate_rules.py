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


def resolve_rules(table: str, df: pd.DataFrame,
                  dictionary: Optional[Dict[Tuple[str, str], str]] = None,
                  client_id: Optional[str] = None, client_name: Optional[str] = None,
                  rule_planner: Optional["duplicate_rule_planner.RulePlanner"] = None) -> Dict[str, Any]:
    """The matching rules for one table - from config, from memory, or freshly drafted."""
    raw = Config.DUPLICATE_TABLE_RULES.get(table)
    if raw:
        return _from_config(table, raw)

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
