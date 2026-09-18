"""Saved duplicate-matching rules - the reason the LLM is only paid for once.

``duplicate_rule_planner`` asks the LLM to draft the matching rules for a table.
That answer is knowledge about the client's data, not per-run state, so it is
stored here as human-readable JSON next to the rest of the memory system:

    <memory.clients_dir>/<client_id>/duplicate_rules.json   this client's rules
    <memory.base_dir>/procedural/duplicate_rules.json       rules the LLM marked
                                                            UNIVERSAL / INDUSTRY_SPECIFIC

Lookup is by **schema signature** - the table name plus a hash of its column
set, order and case independent. Clients upload whatever tables they have and
those layouts change between extracts, so the signature is what decides whether
a saved rule still describes the data in front of us: an identical column set
is reused as is with no LLM call, anything else is re-drafted for that one
table and saved. A changed layout is re-drafted even when the columns the rules
use are all still there, because a column that appeared may itself be worth
matching on (a newly extracted tax or registration number, say) and only the
LLM can tell. Other tables in the same run are unaffected.

Cross-client reuse is OFF by default (``duplicates.rules.reuse_across_clients``).
The same (table, column) pair does not mean the same thing at every company:
standard fields get repurposed - a sort field holding a legacy vendor code, a
spare text field holding a store number - and custom fields mean whatever their
owner decided. A rule learned at one client is therefore a hint, not a fact,
about another. When the flag is on, a shared rule is only reused for an exact
schema-signature match.
"""

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ..client_knowledge import read_json, write_json_atomic
from ..config import Config
from ..logging_config import get_logger

logger = get_logger("duplicate_rule_store")

SHARED_SCOPES = ("UNIVERSAL", "INDUSTRY_SPECIFIC")
# Keys of a rules dict that are persisted (the rest - source, source_detail,
# checks - is derived again on load, so old files never go stale).
_PERSISTED = ("key", "key_unique", "name", "identifiers", "location", "display",
              "label", "rule_scope", "industry", "notes", "why")


def schema_signature(columns) -> str:
    """Stable id for a table layout: order and case independent."""
    joined = "|".join(sorted(str(c).strip().upper() for c in columns))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]


def _client_path(client_id: str) -> Path:
    return Path(Config.CLIENT_KNOWLEDGE_DIR) / client_id / "duplicate_rules.json"


def _shared_path() -> Path:
    return Path(Config.MEMORY_BASE_DIR) / "procedural" / "duplicate_rules.json"


def _rules_of(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in entry.get("rules", {}).items() if k in _PERSISTED}


def _describe(entry: Dict[str, Any], origin: str) -> str:
    created = str(entry.get("created_at", ""))[:10]
    return (f"reused from {origin} (drafted by {entry.get('created_by', 'an LLM')} on {created}, "
            f"used {entry.get('reuse_count', 0)} time(s) before)")


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def load_rules(table: str, columns, client_id: Optional[str] = None,
               client_name: Optional[str] = None) -> Optional[Tuple[Dict[str, Any], str, str]]:
    """Saved rules for this table+schema, or None.

    Returns ``(rules, source, source_detail)`` where source is
    ``client-memory``/``shared-memory`` and source_detail is the sentence the
    review app shows above the rules.
    """
    signature = schema_signature(columns)

    if client_id:
        entry = read_json(_client_path(client_id), {}).get("tables", {}).get(table)
        if entry:
            if entry.get("signature") == signature:
                origin = f"this client's memory ({client_name or client_id})"
                return _rules_of(entry), "client-memory", _describe(entry, origin)
            logger.info("[%s] this client's saved rules were drafted for a different column set "
                        "- the table's schema changed, so the rules are re-drafted", table)

    if Config.DUPLICATE_RULES_REUSE_ACROSS_CLIENTS:
        for entry in read_json(_shared_path(), {}).get("rules", []):
            # A rule proven at another client is not evidence about this one
            # unless the layout is identical - see the module docstring.
            if entry.get("table") == table and entry.get("signature") == signature:
                origin = f"shared {entry.get('rule_scope', 'UNIVERSAL')} memory"
                return _rules_of(entry), "shared-memory", _describe(entry, origin)

    return None


# ---------------------------------------------------------------------------
# Save / bookkeeping
# ---------------------------------------------------------------------------

def save_rules(table: str, columns, rules: Dict[str, Any], created_by: str,
               client_id: Optional[str] = None) -> None:
    """Store freshly drafted rules for this client, and share them if the LLM scoped them that way."""
    entry = {
        "table": table,
        "signature": schema_signature(columns),
        "columns": [str(c) for c in columns],
        "rules": {k: v for k, v in rules.items() if k in _PERSISTED},
        "rule_scope": rules.get("rule_scope", "CLIENT_SPECIFIC"),
        "industry": rules.get("industry"),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": created_by,
        "client_id": client_id,
        "reuse_count": 0,
    }

    if client_id:
        path = _client_path(client_id)
        data = read_json(path, {"version": 1, "tables": {}})
        data.setdefault("tables", {})[table] = entry
        write_json_atomic(path, data)
        logger.info("[%s] duplicate rules saved for client %s (%s)", table, client_id, path.name)

    if entry["rule_scope"] in SHARED_SCOPES:
        path = _shared_path()
        data = read_json(path, {"version": 1, "rules": []})
        others = [e for e in data.get("rules", [])
                  if not (e.get("table") == table and e.get("signature") == entry["signature"])]
        data["rules"] = others + [entry]
        write_json_atomic(path, data)
        logger.info("[%s] rules also stored as %s knowledge (reusable at other clients)",
                    table, entry["rule_scope"])


def record_reuse(table: str, client_id: Optional[str]) -> None:
    """Count one run that ran on saved rules instead of calling the LLM."""
    if not client_id:
        return
    path = _client_path(client_id)
    data = read_json(path, None)
    entry = (data or {}).get("tables", {}).get(table)
    if not entry:
        return
    entry["reuse_count"] = entry.get("reuse_count", 0) + 1
    entry["last_used_at"] = datetime.now(timezone.utc).isoformat()
    write_json_atomic(path, data)


def list_rules(client_id: str) -> Dict[str, Any]:
    """Every saved rule set for one client, keyed by table (for inspection/debugging)."""
    return read_json(_client_path(client_id), {}).get("tables", {})
