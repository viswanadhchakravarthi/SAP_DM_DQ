"""The local model as an on-demand auditor of small, ambiguous slices.

Rules, lookups and the embedding model settle almost everything. What is left are a
few cases a person would have to read: two records whose names are 70-85% alike and
whose address agrees, or a street field that might have a city or PO Box typed into
it. This module asks the LOCAL model (the in-process llama.cpp GGUF from
``llm.local.model_path``) about exactly one such case at a time.

Guardrails, all enforced here rather than left to callers:

* **Never a table.** There is no entry point that takes a DataFrame. A call carries one
  pair or one string, and a process-wide budget (``local_audit.max_records_per_run``,
  default 50 records; a pair costs 2) stops further calls. Items past the budget are
  simply not audited. A 3B model on CPU takes seconds per item, so an unbounded loop
  over 50k-1M rows would run for days and exhaust memory.
* **Local only.** The record values in the prompt reach no hosted model: the module
  imports the local loader directly and never builds a hosted client (the same
  exception ``explain.py`` documents). Off by default (``local_audit.enabled``).
* **The model proposes, rules and people decide.** A verdict can only add a SIMILAR
  look-alike for a reviewer, or a proposed street split a reviewer confirms. It never
  merges, deletes or edits a record, and an extracted city must literally appear in the
  original text (no invented values).
* **One failure ends it.** If the model cannot be loaded or errors, auditing is switched
  off for the rest of the process, so a missing GGUF costs one warning, not one per item.
"""

import threading
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from .config import Config
from .logging_config import get_logger
from .metrics import metrics
from .schemas import AddressParts, PairVerdict

logger = get_logger("local_auditor")

_lock = threading.Lock()
_model = None
_broken = False

_PAIR_SYSTEM = """You compare two master data records and decide whether they describe the SAME real-world \
entity (one company or person entered twice). Use only the facts shown.
SAME = every difference is only spelling: abbreviation, legal form, word order, punctuation.
DIFFERENT = a difference spelling cannot explain: another number, branch, department, person or place.
UNSURE = the facts do not decide it. Prefer UNSURE over guessing: a wrong SAME costs a reviewer's time.
In 'reason' quote the two concrete values that decided it (for example: "Pharma" vs "Pharmaceutical"). \
Do not describe the task."""

_ADDRESS_SYSTEM = """You split ONE free-text street field of a master data record into parts. Copy text \
exactly as written; never invent, translate or correct anything. Leave a part empty when it is not in \
the text. 'street' is the street name with its house number only. A PO Box (Postfach, Apartado, \
Caixa Postal) goes into po_box. A town or city written inside the field goes into city, a postal code \
into postal_code."""


def enabled() -> bool:
    return bool(Config.LOCAL_AUDIT_ENABLED and Config.LOCAL_LLM_MODEL_PATH and not _broken)


def remaining() -> int:
    return max(0, Config.LOCAL_AUDIT_MAX_RECORDS - metrics.local_audit_records)


def _structured(schema):
    """The local model bound to one schema, loaded on first use."""
    global _model
    if _model is None:
        # Deliberately the local loader and nothing else: no path from here to a hosted model.
        from .llm_providers import _get_local_llm
        from .local_llms import QwenCoderGGUFChatModel
        _model = QwenCoderGGUFChatModel(llm=_get_local_llm(), max_tokens_tool_call=Config.LOCAL_AUDIT_MAX_TOKENS)
    return _model.with_structured_output(schema)


def _ask(schema, system: str, facts: str, cost: int):
    """One bounded question; None when auditing is off, over budget or the model failed."""
    global _broken
    if not enabled() or cost > remaining():
        return None
    with _lock:
        if cost > remaining():
            return None
        metrics.local_audit_records += cost
        try:
            return _structured(schema).invoke([SystemMessage(content=system), HumanMessage(content=facts)])
        except Exception as exc:  # missing llama_cpp / model file, out of memory, unparseable output
            logger.warning("Local audit failed (%s) - auditing is switched off for this run",
                           str(exc).splitlines()[0][:200])
            _broken = True
            return None


def _describe(record: Dict[str, Any]) -> str:
    fields = {"name": record.get("name"), **(record.get("display") or {})}
    return "; ".join(f"{k}: {v}" for k, v in fields.items() if v)


def arbitrate_pair(a: Dict[str, Any], b: Dict[str, Any]) -> Optional[PairVerdict]:
    """Same entity or not, for one borderline pair of duplicate-detector records (2 records of budget)."""
    facts = f"Record A: {_describe(a)}\nRecord B: {_describe(b)}"
    verdict = _ask(PairVerdict, _PAIR_SYSTEM, facts, cost=2)
    if verdict is not None:
        logger.info("Local audit: %s vs %s -> %s (%s)", a.get("key"), b.get("key"), verdict.verdict,
                    verdict.reason[:120])
    return verdict


def split_street(value: str, city: str = "") -> Optional[AddressParts]:
    """The parts inside one street string (1 record of budget). Anything not literally present in the
    original text is discarded, so a hallucinated city can never reach a finding."""
    facts = f"Street field: {value}" + (f"\nThe record's own city column says: {city}" if city else "")
    parts = _ask(AddressParts, _ADDRESS_SYSTEM, facts, cost=1)
    if parts is None:
        return None
    lowered = value.lower()
    cleaned = {k: v.strip() for k, v in parts.model_dump().items()}
    for key in ("po_box", "city", "postal_code"):
        if cleaned[key] and cleaned[key].lower() not in lowered:
            cleaned[key] = ""
    return AddressParts(**cleaned)
