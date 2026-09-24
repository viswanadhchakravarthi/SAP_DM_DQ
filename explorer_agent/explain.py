"""Why a record was flagged: an exact explanation first, an optional plain-language one second.

1. `build_explanation` is deterministic and free. It is assembled from what the run already stored
   (the finding's rule id, rule statement and column meanings, the record's own reason text) plus the
   record's values, read from the uploaded CSV at display time by the review app. Nothing in it is
   generated, so it can't be wrong in a way the rule engine isn't.
2. `generate_plain_language` asks the LOCAL model (in-process llama.cpp, no network) to restate those
   same facts in two or three sentences for a business reviewer. It is optional, off by default
   (`explain.local_llm.enabled`), cached per record, and labelled AI-generated in the UI.

Privacy: this is the one place record VALUES go to an LLM, and only because that LLM runs on this
machine. The module never builds a hosted client - it imports `_get_local_llm` directly, so there is no
configuration under which Gemini sees these values - and it refuses to run unless the switch is on.
Every other LLM call in the project still sees metadata only (CLAUDE.md, invariant 1).
"""

import re
import threading
from typing import Any, Dict, List, Optional

from langchain_core.messages import HumanMessage, SystemMessage

from .config import Config
from .logging_config import get_logger

logger = get_logger("explain")

# What each built-in rule family means for a migration. Written once, generic and hedged: these describe
# the rule, they don't claim what happened to this particular record.
_FAMILIES = [
    ("mandatory", "Mandatory field",
     "SAP treats this field as mandatory. A record loaded without it is rejected or created incomplete, so the "
     "value has to be supplied by the business before migration."),
    ("flag", "Deletion flag or block",
     "The record carries a deletion flag or a block, i.e. it is marked as no longer to be used. Migrating it copies "
     "dead data into the new system; decide whether it is still in migration scope."),
    ("dormant", "Dormant record",
     "The record is old and no organisational-level record (company code, sales or purchasing view) uses it. It is a "
     "candidate to leave out or archive instead of migrating."),
    ("orphan", "Missing parent",
     "The record refers to a parent that does not exist. SAP checks this reference on load, so the record fails or "
     "has to be linked to a valid parent first."),
    ("propagation", "Block not carried down",
     "A block or deletion flag set on the central record must also be set on its dependent records, otherwise the "
     "dependent record stays usable where it should be blocked."),
    ("country", "Country key",
     "The country key is not a valid ISO code (SAP country keys are case-sensitive). The load rejects it, and every "
     "country-based check (postal code, tax number) depends on it."),
    ("postal", "Postal code format",
     "The postal code does not fit the format for the record's country. SAP can reject it on load, and address or "
     "tax processing fails later."),
    ("tax.format", "Tax number format",
     "The tax number does not fit the formats valid for the record's country (EU VAT or national), or it is a "
     "placeholder. Tax reporting and invoicing can fail on it."),
    ("tax.missing", "No tax number",
     "None of the tax number fields holds a real value for this record. Where a tax number is expected, invoicing and "
     "tax reporting will be incomplete."),
    ("domain", "Value not allowed",
     "The value is not in the list of allowed values of the target field (its check table), so the load rejects it."),
    ("text", "Text hygiene",
     "Illegal characters, encoding damage, doubled or leading spaces, placeholder text or unusual casing. These make "
     "loads fail and create look-alike records that hide duplicates."),
    ("anomaly.negative", "Negative amount or quantity",
     "A negative amount or quantity where master data does not normally expect one."),
    ("anomaly.outlier", "Extreme value",
     "The value is far outside the range of the rest of the column: often a typing error or a wrong unit."),
    ("anomaly.rare_code", "Rare code",
     "The code is used only once or twice while the usual codes each cover many rows: likely invalid or a legacy value."),
    ("anomaly.currency", "Currency key",
     "The currency key is not a valid ISO 4217 code, so amounts in that currency can't be posted."),
]

_MEANING_LINE = re.compile(r"^#\s{3}(\w+) = (\w+) - (.*)$")


def rule_family(rule_id: Optional[str]) -> Dict[str, Optional[str]]:
    """{'label', 'impact'} of a built-in rule id such as 'mandatory.LFB1.ZTERM' (None values when unknown)."""
    rid = (rule_id or "").lower()
    # Longest prefix first, so 'tax.missing' wins over a plain 'tax'.
    for prefix, label, impact in sorted(_FAMILIES, key=lambda f: -len(f[0])):
        if rid == prefix or rid.startswith(prefix + "."):
            return {"label": label, "impact": impact}
    return {"label": None, "impact": None}


def columns_from_check_code(check_code: str) -> List[Dict[str, str]]:
    """Columns a built-in rule used, with what the mapping decided they mean (the 'Column meaning' lines)."""
    out = []
    for line in (check_code or "").splitlines():
        m = _MEANING_LINE.match(line)
        if m:
            out.append({"column": m.group(1), "concept": m.group(2), "reason": m.group(3).strip()})
    return out


def evidence_columns(finding: Dict[str, Any], item: Dict[str, Any], table_columns: List[str]) -> List[str]:
    """Columns worth showing for this record: its key, then the columns the rule used (or the check's column)."""
    have = set(table_columns)
    wanted: List[str] = []
    for c in str(item.get("key_field") or "").replace("+", " ").split():
        wanted.append(c)
    used = [c["column"] for c in columns_from_check_code(finding.get("check_code") or "")]
    if not used and finding.get("column_name"):
        used = [c for c in str(finding["column_name"]).replace("+", " ").split()]
    wanted += used
    return [c for c in dict.fromkeys(wanted) if c in have]


def build_explanation(finding: Dict[str, Any], item: Dict[str, Any], columns_meta: Dict[str, Dict[str, str]],
                      values: Dict[str, str], stale: bool, note: Optional[str]) -> Dict[str, Any]:
    """The deterministic 'why flagged' for one record."""
    check_code = finding.get("check_code") or ""
    built_in = check_code.startswith("# Built-in SAP rule")
    try:
        import json
        rule_id = (json.loads(finding["raw_result"]) or {}).get("rule_id") if built_in and finding.get("raw_result") else None
    except (ValueError, TypeError):
        rule_id = None
    family = rule_family(rule_id) if built_in else {"label": None, "impact": None}
    meaning = {c["column"]: c for c in columns_from_check_code(check_code)}

    evidence = []
    for col in values if values else []:
        meta = columns_meta.get(col, {})
        evidence.append({"column": col, "description": meta.get("description", ""), "data_type": meta.get("data_type", ""),
                         "concept": meaning.get(col, {}).get("concept"), "why": meaning.get(col, {}).get("reason"),
                         "value": values[col]})
    return {
        "item_id": item["id"], "finding_id": finding["id"], "table": finding["table_name"],
        "record": {"key_field": item.get("key_field"), "key_value": item.get("key_value"), "row_index": item.get("row_index")},
        "what": item.get("issue_detail") or finding.get("result_summary"),
        "rule": {"kind": "BUILT_IN" if built_in else "LLM_CHECK", "id": rule_id, "family": family["label"],
                 "statement": finding.get("hypothesis"), "impact": family["impact"],
                 "severity": finding.get("severity"), "category": finding.get("category")},
        "evidence": evidence, "stale": stale, "note": note,
    }


# --------------------------------------------------------------------------- #
# Optional: plain language from the local model
# --------------------------------------------------------------------------- #
class ExplainUnavailable(RuntimeError):
    """Plain-language explanations are off, or the local model can't be loaded (message is safe to show)."""


_local_lock = threading.Lock()  # one local generation at a time (one model, CPU)

_SYSTEM = ("You explain data quality findings to a business reviewer on an SAP data migration project. Use ONLY the "
           "facts you are given; never invent values, rules or numbers. Write two or three short plain sentences: "
           "why this record was flagged, what it can cause in the migration, and what the reviewer should check. "
           "No headings, no bullet points, no code.")


def _facts(expl: Dict[str, Any]) -> str:
    rule = expl["rule"]
    lines = [f"Table: {expl['table']}",
             f"Record: {expl['record']['key_field']} = {expl['record']['key_value']}",
             f"What was found: {expl['what']}",
             f"Rule: {rule['statement']}"]
    if rule.get("impact"):
        lines.append(f"Why the rule exists: {rule['impact']}")
    for e in expl["evidence"]:
        label = e["description"] or e["column"]
        lines.append(f"{e['column']} ({label}) = {e['value']!r}" + (f" - {e['why']}" if e.get("why") else ""))
    return "\n".join(lines)


def generate_plain_language(expl: Dict[str, Any]) -> Dict[str, Any]:
    """Two or three sentences from the local model. Raises ExplainUnavailable when off or not loadable."""
    if not Config.EXPLAIN_LOCAL_LLM_ENABLED:
        raise ExplainUnavailable("Plain-language explanations are off. Set explain.local_llm.enabled: true in "
                                 "config.yaml to allow the local model to read record values.")
    if not Config.LOCAL_LLM_MODEL_PATH:
        raise ExplainUnavailable("No local model is configured (llm.local.model_path in config.yaml).")
    try:
        # Deliberately the local loader and nothing else: there is no path from here to a hosted model.
        from .llm_providers import _get_local_llm
        from .local_llms import QwenCoderGGUFChatModel
        with _local_lock:
            model = QwenCoderGGUFChatModel(llm=_get_local_llm(), max_tokens=Config.EXPLAIN_MAX_TOKENS)
            message = model.invoke([SystemMessage(content=_SYSTEM), HumanMessage(content=_facts(expl))])
    except ExplainUnavailable:
        raise
    except Exception as exc:  # missing llama_cpp, missing/corrupt model file, out of memory...
        raise ExplainUnavailable(f"The local model could not be used: {str(exc).splitlines()[0][:200]}") from exc
    text = " ".join(str(message.content).split())
    if not text:
        raise ExplainUnavailable("The local model returned an empty answer.")
    usage = getattr(message, "usage_metadata", None) or {}
    return {"text": text, "model": "local:gguf", "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens")}
