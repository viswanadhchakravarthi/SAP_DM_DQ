"""The LLM drafts the duplicate-matching rules for a table - once per client+schema.

An analyst setting up duplicate detection for a new client reads the data
dictionary, looks at what the columns actually contain, and decides: this is
the business key, these values identify the real-world entity, this is the
name, these fields corroborate a name match. That judgment is what this module
asks the LLM for - ONE structured call per table, from metadata only.

Why the LLM and not Python: a (TABLE, COLUMN) pair does not mean the same thing
at every company. Standard fields get repurposed (a sort field holding a legacy
vendor code from a retired system, a "train station" field holding a warehouse
code), custom Z-fields mean whatever their owner decided, and uploaded extracts
can come from non-SAP sources entirely. Only the client's own dictionary text
says which is which, so pattern-matching column names against a keyword list is
guesswork that silently misfires. The LLM reads the description; Python does not.

What crosses the boundary to an external LLM: column names, the client's data
dictionary text, aggregate statistics, and masked example values
(``profiler_primitives.mask_value`` keeps only the last two characters) - never
raw rows, exactly like the table profiler.

What comes back is a *spec*, not code: each column gets a role (see
``schemas.DuplicateRulePlan``). ``compile_plan`` turns it into the rules dict
``duplicate_detector`` has always executed, so the matching engine, the review
API and the review UI are unchanged. The spec is then saved
(``memory.duplicate_rule_store``) and every later run for that client reuses it
with zero LLM calls.
"""

import json
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from langchain_core.messages import HumanMessage, SystemMessage

from .config import Config
from .logging_config import get_logger
from .metrics import metrics
from .profiler_primitives import mask_value
from .schemas import DuplicateRulePlan

logger = get_logger("duplicate_rule_planner")

MAX_DISPLAY_COLUMNS = 10

SYSTEM_PROMPT = """You are a data migration analyst setting up DUPLICATE DETECTION for one table of a \
client's dataset. You decide which columns the matching engine may use and why. You never see raw data: \
you get the client's own data dictionary plus aggregate statistics and masked example values \
(only the last two characters of a value are visible).

Assign EVERY column exactly one role:

- KEY        The column(s) identifying the business object this table is about. Rows that share a KEY
             belong to the same object and are never compared against each other (e.g. several bank
             accounts of one vendor). Also set key_unique at plan level: true when the key MUST be
             unique in this table, so a repeated key value is itself a duplicate; false when one key
             legitimately owns many rows.
- IDENTIFIER A value that identifies a real-world entity on its own: a tax/VAT/registration number, an
             e-mail address, a phone number, an IBAN, a national ID. Two records sharing one are the
             same entity, so this is the strongest evidence the engine has - only give this role when
             the value really is entity-specific. Columns that are only meaningful TOGETHER (for
             example a bank country + bank key + account number, where the account number alone is not
             unique) MUST all carry the SAME non-empty identifier_group name. Whenever your reason
             says a column only identifies something "together with" or "combined with" another
             column, setting identifier_group on each of them is mandatory - left empty, each column
             is matched on by itself and will link unrelated records.
- NAME       The entity's name or description, used for exact and fuzzy name matching. Choose at most
             one column - the one a human would recognise the entity by.
- LOCATION   A place-like field (street, address, city, postal code, district) that corroborates a name
             match. A name match alone is never trusted without one of these.
- CONTEXT    Not used for matching, but useful for the human reviewer to see side by side.
- IGNORE     Irrelevant for duplicate detection.

How to decide:

1. The client's DICTIONARY DESCRIPTION outranks the column name. The same column name means different
   things at different companies - standard fields are routinely repurposed (a sort field holding a
   legacy code from a retired system, a spare text field holding a store number). If the description
   contradicts what the name suggests, follow the description and say so in your reason.
2. Use the statistics as evidence. A column that is mostly empty, or whose values repeat across many
   rows, is not an identifier no matter what it is called: a standalone IDENTIFIER needs a high
   distinct_pct_of_filled (a low one means many records share each value, so the column classifies
   rather than identifies, and it belongs in an identifier_group or in CONTEXT). A name column is
   text, populated, and varied.
3. Prefer FEWER, stronger rules over many weak ones. A wrong identifier floods the reviewer with false
   duplicates; a missing one only costs recall.
4. Mark show_in_review=true for the columns a reviewer needs to judge two records side by side
   (roughly 6-8: the name, the location fields, the identifiers, and any telling context column).
5. Give each column a one-sentence reason a human auditor can check, citing the dictionary description
   or the statistic you used. These reasons are shown in the review app.
6. Set rule_scope: UNIVERSAL only if these role assignments would hold at ANY company using this same
   standard table layout; INDUSTRY_SPECIFIC if they depend on the industry; CLIENT_SPECIFIC if any
   column is custom or repurposed for this client.

Return only the rule spec. Do not write code, do not invent columns that are not listed, and do not
repeat any example value back."""


# ---------------------------------------------------------------------------
# Metadata sent to the LLM (no raw values)
# ---------------------------------------------------------------------------

def _column_stats(series: pd.Series, sample_size: int) -> Dict[str, Any]:
    total = len(series) or 1
    non_null = series.dropna()
    non_null = non_null[non_null.astype(str).str.strip() != ""]
    text = non_null.astype(str)
    stats: Dict[str, Any] = {
        "dtype": str(series.dtype),
        "filled_pct": round(100 * len(non_null) / total, 1),
        "distinct_pct_of_filled": round(100 * non_null.nunique() / len(non_null), 1) if len(non_null) else 0.0,
        "unique_in_table": bool(len(non_null) == total and non_null.nunique() == total),
        "avg_length": round(float(text.str.len().mean()), 1) if len(non_null) else 0.0,
    }
    if sample_size > 0 and len(non_null):
        # Masked: only the last two characters survive (profiler_primitives.mask_value),
        # enough to show shape/length, not the value.
        stats["masked_examples"] = [mask_value(v) for v in text.head(sample_size).tolist()]
    return stats


def column_metadata(table: str, df: pd.DataFrame,
                    dictionary: Optional[Dict[Tuple[str, str], str]] = None) -> List[Dict[str, Any]]:
    """Per-column metadata for the prompt: dictionary text + sanitized statistics."""
    sample_size = Config.DUPLICATE_RULE_SAMPLE_VALUES
    described = dictionary or {}
    metadata = []
    for column in df.columns:
        entry: Dict[str, Any] = {"column": column}
        description = described.get((table.upper(), str(column).upper()), "").strip()
        entry["dictionary"] = description or "(not in the data dictionary)"
        entry.update(_column_stats(df[column], sample_size))
        metadata.append(entry)
    return metadata


def build_prompt(table: str, df: pd.DataFrame,
                 dictionary: Optional[Dict[Tuple[str, str], str]] = None,
                 client_name: Optional[str] = None) -> List[Any]:
    table_note = Config.SAP_TABLE_DESCRIPTIONS.get(table)
    lines = [f"Client: {client_name}" if client_name else "Client: (not given)",
             f"Table: {table}",
             f"Rows: {len(df)}"]
    if table_note:
        lines.append(f"Table description: {table_note}")
    lines += [
        "",
        "Columns (dictionary text from the client, plus statistics; example values are masked "
        "- only the last two characters are real):",
        json.dumps(column_metadata(table, df, dictionary), indent=2, default=str),
        "",
        f"Return one entry for each of the {len(df.columns)} columns.",
    ]
    return [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content="\n".join(lines))]


# ---------------------------------------------------------------------------
# Plan -> the rules dict the detector executes
# ---------------------------------------------------------------------------

def build_checks(rules: Dict[str, Any]) -> List[str]:
    """Human-readable list of what will actually be checked (shown in the review app)."""
    checks = ["identical rows"]
    if rules.get("key_unique") and rules.get("key"):
        checks.append(f"repeated {' + '.join(rules['key'])}")
    if rules.get("identifiers"):
        checks.append("shared " + ", ".join(" + ".join(i) for i in rules["identifiers"]))
    if rules.get("name") and rules.get("location"):
        checks.append(f"same/similar {rules['name']} confirmed by {', '.join(rules['location'])}")
    return checks


def _distinct_ratio(series: pd.Series) -> float:
    non_null = series.dropna()
    non_null = non_null[non_null.astype(str).str.strip() != ""]
    return non_null.nunique() / len(non_null) if len(non_null) else 0.0


def _drop_impossible_identifiers(groups: Dict[str, List[str]], df: pd.DataFrame,
                                 why: Dict[str, str]) -> List[List[str]]:
    """Reject single-column identifiers the data itself rules out.

    This is arithmetic, not a second opinion on what a column means: if only a
    few percent of a column's filled values are distinct, each value covers many
    records, so it cannot identify one - whatever it is called. It is the mistake
    a model makes when it describes a composite identifier (bank country + key +
    account) correctly but forgets to put its parts in one identifier_group.
    """
    minimum = Config.DUPLICATE_RULE_MIN_IDENTIFIER_DISTINCT
    identifiers = []
    for columns in groups.values():
        if not columns:
            continue
        if len(columns) == 1:
            ratio = _distinct_ratio(df[columns[0]])
            if ratio < minimum:
                why[columns[0]] = (
                    f"context - not used for matching: only {ratio:.1%} of its filled values are "
                    f"distinct, so one value covers many records and cannot identify one. "
                    f"(The rule said: {why.get(columns[0], '')})")
                logger.warning("Dropped identifier %r - only %.1f%% of its values are distinct",
                               columns[0], ratio * 100)
                continue
        identifiers.append(columns)
    return identifiers


def compile_plan(plan: DuplicateRulePlan, df: pd.DataFrame) -> Dict[str, Any]:
    """Turn a validated LLM plan into the rules dict duplicate_detector executes.

    Anything the model got wrong structurally is dropped here rather than raised:
    columns that aren't in the table, duplicate entries, a second NAME column.
    """
    known = {str(c).upper(): c for c in df.columns}
    seen: set = set()
    entries = []
    for item in plan.columns:
        column = known.get(str(item.column).strip().upper())
        if column is None:
            logger.warning("Rule plan references unknown column %r - ignored", item.column)
            continue
        if column in seen:
            continue
        seen.add(column)
        entries.append((column, item))

    why: Dict[str, str] = {}
    key: List[str] = []
    name: Optional[str] = None
    location: List[str] = []
    groups: Dict[str, List[str]] = {}   # identifier_group -> columns (insertion ordered)
    display: List[str] = []

    for column, item in entries:
        if item.role == "KEY":
            key.append(column)
        elif item.role == "IDENTIFIER":
            groups.setdefault(item.identifier_group or f"__{column}", []).append(column)
        elif item.role == "NAME":
            if name is None:
                name = column
            else:
                # Only one name can drive matching; the runner-up is still worth
                # showing to the reviewer, so it degrades to context.
                logger.info("Rule plan proposed a second NAME column %r - keeping %r", column, name)
                why[column] = f"context - {item.reason} (second name column; {name} is matched on)"
                if item.show_in_review:
                    display.append(column)
                continue
        elif item.role == "LOCATION":
            location.append(column)
        elif item.role == "IGNORE":
            continue
        if item.show_in_review:
            display.append(column)
        why[column] = f"{item.role.lower()} - {item.reason}"

    identifiers = _drop_impossible_identifiers(groups, df, why)
    if not display:
        display = [c for c, _ in entries if c not in key][:8]
    display = list(dict.fromkeys(display))[:MAX_DISPLAY_COLUMNS]

    rules: Dict[str, Any] = {
        "key": key,
        "key_unique": bool(plan.key_unique and key),
        # A name match is only trusted when location fields can confirm it - the
        # detector enforces this too, but keeping it out of the spec means the
        # saved rules and the "checks" text say what will really happen.
        "name": name if location else None,
        "identifiers": identifiers,
        "location": location,
        "display": display,
        "label": plan.label,
        "rule_scope": plan.rule_scope,
        "industry": plan.industry,
        "notes": plan.notes,
        "why": why,
    }
    if name and not location:
        why[name] = (f"{why.get(name, 'name - ')} NOT used for matching: this table has no location "
                     f"columns to confirm a name match.")
    rules["checks"] = build_checks(rules)
    return rules


class RulePlanner:
    """The LLM side of rule drafting, built on first use.

    A run whose tables all have saved rules must not pay for an LLM at all -
    not even the construction cost (loading the local GGUF model, for instance).
    So callers hand over a factory, not a chain: nothing is built until a table
    actually needs new rules. ``label`` names the model in logs, in the saved
    rule file and in the review app's "View Matching Rules" text.
    """

    def __init__(self, factory, label: Optional[str] = None):
        self._factory = factory          # () -> (structured runnable, label)
        self._chain = None
        self.label = label or "an LLM"

    def invoke(self, messages: List[Any]) -> DuplicateRulePlan:
        if self._chain is None:
            self._chain, self.label = self._factory()
        return self._chain.invoke(messages)


def plan_rules(table: str, df: pd.DataFrame, rule_llm,
               dictionary: Optional[Dict[Tuple[str, str], str]] = None,
               client_name: Optional[str] = None) -> Dict[str, Any]:
    """One structured LLM call -> the rules dict for this table.

    Raises whatever the LLM chain raises (LLMChainExhaustedError when every
    model failed); the caller decides how to degrade.
    """
    logger.info("[%s] no saved duplicate rules for this schema - asking the LLM to draft them", table)
    plan: DuplicateRulePlan = rule_llm.invoke(build_prompt(table, df, dictionary, client_name))
    metrics.duplicate_rule_llm_calls += 1
    rules = compile_plan(plan, df)
    logger.info("[%s] rules drafted (scope=%s): %s", table, rules["rule_scope"], "; ".join(rules["checks"]))
    return rules
