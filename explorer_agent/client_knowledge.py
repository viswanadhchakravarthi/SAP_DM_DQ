"""Client-specific knowledge - human decisions that outlive a single run.

Episodic memory (``episodic_store``) keeps each run's findings and review
state, but a new run creates new findings, so decisions made there would be
lost. This module keeps the durable part per client (company) as
human-readable JSON, alongside procedural memory:

    <memory.clients_dir>/<client_id>/client.json               display name
    <memory.clients_dir>/<client_id>/duplicate_decisions.json  duplicate review decisions
    <memory.clients_dir>/<client_id>/duplicate_rules.json      LLM-drafted matching rules,
                                                               written by memory.duplicate_rule_store

Duplicate decisions are stored per record, keyed by ``record_id`` =
``<business key>|<fingerprint of the record's display fields>``, with the
partner records it was grouped with at decision time. Embedding the
fingerprint means a decision silently stops applying once either record's
values change - the changed data gets reviewed again instead of inheriting an
outdated verdict.

How ``duplicate_detector`` uses them on the next run for the same client:

* Both records UNIQUE - the pair is not linked at all, so pairs a human said
              are not duplicates stop coming back.
* DUPLICATE / TO_BE_CONFIRMED / UNIQUE - pre-filled on the record when it is
              grouped with at least one of its former partners again. (A group
              with one DUPLICATE and one UNIQUE record is a confirmed duplicate
              whose UNIQUE record is the original to keep.)

The review app writes here the moment a verdict is saved (last action wins);
the explorer only reads.
"""

import hashlib
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .config import Config

# UNIQUE is carried too: a group only comes back with a UNIQUE record in it when
# that record is the original of a duplicate pair (both-UNIQUE pairs are skipped).
CARRY_FORWARD_VERDICTS = {"DUPLICATE", "TO_BE_CONFIRMED", "UNIQUE"}
DUPLICATE_VERDICTS = {"DUPLICATE", "UNIQUE", "TO_BE_CONFIRMED"}

_write_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------

def client_id_for(name: Optional[str]) -> str:
    """Stable folder-safe id: 'Acme Retail Ltd.' -> 'acme-retail-ltd'."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    if not slug:
        raise ValueError("Client name must contain at least one letter or digit")
    return slug


def _client_dir(client_id: str) -> Path:
    return Path(Config.CLIENT_KNOWLEDGE_DIR) / client_id


def read_json(path: Path, default: Any) -> Any:
    """Shared with memory.duplicate_rule_store - same human-readable JSON files."""
    if not path.exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def ensure_client(name: str) -> Dict[str, str]:
    """Register a client (first-used display name wins) and return {client_id, name}."""
    client_id = client_id_for(name)
    path = _client_dir(client_id) / "client.json"
    with _write_lock:
        info = read_json(path, None)
        if info is None:
            info = {
                "client_id": client_id,
                "name": name.strip(),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            write_json_atomic(path, info)
    return {"client_id": info["client_id"], "name": info["name"]}


def get_client(client_id: str) -> Optional[Dict[str, str]]:
    """{client_id, name} for an existing client, or None (also for malformed ids)."""
    try:
        if client_id_for(client_id) != client_id:
            return None
    except ValueError:
        return None
    info = read_json(_client_dir(client_id) / "client.json", None)
    return {"client_id": info["client_id"], "name": info["name"]} if info else None


def list_clients() -> List[Dict[str, Any]]:
    root = Path(Config.CLIENT_KNOWLEDGE_DIR)
    clients = []
    if root.exists():
        for folder in sorted(p for p in root.iterdir() if p.is_dir()):
            info = read_json(folder / "client.json", None)
            if not info:
                continue
            tables = read_json(folder / "duplicate_decisions.json", {}).get("tables", {})
            clients.append({
                "client_id": info["client_id"],
                "name": info["name"],
                "duplicate_decisions": sum(len(t) for t in tables.values()),
            })
    return clients


# ---------------------------------------------------------------------------
# Duplicate decisions
# ---------------------------------------------------------------------------

def record_fingerprint(record_data: Dict[str, Any]) -> str:
    payload = json.dumps(record_data or {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def record_id(key_value: Any, record_data: Dict[str, Any]) -> str:
    return f"{key_value}|{record_fingerprint(record_data)}"


def _decisions_path(client_id: str) -> Path:
    return _client_dir(client_id) / "duplicate_decisions.json"


def load_duplicate_decisions(client_id: Optional[str], table: str) -> Dict[str, Dict[str, Any]]:
    if not client_id:
        return {}
    return read_json(_decisions_path(client_id), {}).get("tables", {}).get(table, {})


def is_known_not_duplicate(decisions: Dict[str, Dict[str, Any]], id_a: str, id_b: str) -> bool:
    """True only when BOTH records were reviewed as UNIQUE against each other.

    Reviewers also use UNIQUE for the original record of a duplicate pair
    ("this one is the duplicate, that one is the original to keep"), so one
    UNIQUE next to a DUPLICATE is a confirmed duplicate, not a reason to stop
    matching the pair.
    """
    entry_a, entry_b = decisions.get(id_a), decisions.get(id_b)
    return bool(
        entry_a and entry_b
        and entry_a["verdict"] == "UNIQUE" and id_b in entry_a["partners"]
        and entry_b["verdict"] == "UNIQUE" and id_a in entry_b["partners"]
    )


def carried_verdict(decisions: Dict[str, Dict[str, Any]], own_id: str,
                    other_ids: Iterable[str]) -> Optional[Dict[str, Any]]:
    """The remembered decision to pre-fill for a record in a new group, if any."""
    entry = decisions.get(own_id)
    if not entry or entry["verdict"] not in CARRY_FORWARD_VERDICTS:
        return None
    return entry if set(other_ids) & set(entry["partners"]) else None


def sync_duplicate_group(client_id: str, table: str, members: List[Dict[str, Any]],
                         run_id: str, finding_id: str, group_id: str) -> int:
    """Store the current verdicts of one reviewed group. Returns the number of records written.

    ``members`` are finding_items rows with ``key_value``, ``record`` (parsed
    record_data) and ``review_verdict``. PENDING removes a record's decision.
    Rows without record data (legacy LLM-generated rows) can't be fingerprinted
    and are skipped.
    """
    usable = [m for m in members if m.get("record")]
    ids = {m["id"]: record_id(m["key_value"], m["record"]) for m in usable}
    now = datetime.now(timezone.utc).isoformat()
    written = 0

    with _write_lock:
        path = _decisions_path(client_id)
        data = read_json(path, {"version": 1, "tables": {}})
        decisions = data.setdefault("tables", {}).setdefault(table, {})

        for m in usable:
            rid = ids[m["id"]]
            verdict = m.get("review_verdict") or "PENDING"
            if verdict not in DUPLICATE_VERDICTS:
                decisions.pop(rid, None)
                continue
            partners = [ids[o["id"]] for o in usable if o["id"] != m["id"]]
            previous = decisions.get(rid)
            if previous and previous["verdict"] == verdict:
                # Same call against more partners over time - keep them all.
                partners = sorted(set(previous["partners"]) | set(partners))
            decisions[rid] = {
                "key": str(m["key_value"]),
                "verdict": verdict,
                "partners": partners,
                "record": m["record"],
                "decided_at": now,
                "run_id": run_id,
                "finding_id": finding_id,
                "group_id": group_id,
            }
            written += 1

        write_json_atomic(path, data)
    return written

