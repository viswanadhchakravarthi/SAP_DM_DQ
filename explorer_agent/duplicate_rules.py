"""Infer duplicate-matching rules for any table - no config, no LLM.

Tables are uploaded per client and are not known in advance, so the rules the
duplicate detector needs are worked out from what is available for every
table: the client's data dictionary (description / data type / notes per
field), the column name, and the values themselves. Every decision is kept
with a human-readable reason in ``rules["why"]`` so reviewers can see (via
"View Matching Rules") why a column was used.

Roles
-----
key          Dictionary "primary key" columns (repeats are duplicates, key_unique=True);
             otherwise "foreign key" columns - rows sharing them belong to the same
             parent object (e.g. several bank accounts of one vendor) and are never
             matched against each other; otherwise none.
name         A name-like text column ("name", "holder"). Name matching also needs
             location columns to corroborate it, so a table without any location
             columns is matched on identifiers only.
identifiers  Values that identify a real-world entity: tax/VAT/registration numbers,
             e-mail, phone, IBAN - from the dictionary, the column name or the value
             format - plus bank key + account number as one composite identifier.
location     Street / city / postal code style columns.
display      The columns shown side by side in the review UI.

``duplicates.tables.<TABLE>`` in config.yaml still overrides inference for a
table when a client needs something specific.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .config import Config

_ROLE_PATTERNS = {
    "primary_key": r"primary key",
    "foreign_key": r"foreign key",
    "identifier": (r"\btax\b|\bvat\b|\bgst|\bpan\b|\btin\b|registration (?:no|number)|\biban\b|e-?mail|"
                   r"\bphone\b|telephone|\bmobile\b|social security|\bssn\b|passport|\bduns\b|national id"),
    "bank_account": r"bank account|account (?:no|number)",
    "bank_part": r"bank key|bank country|bank number|routing|sort code|\bswift\b|\bbic\b",
    "name": r"\bname\b|account holder|\bholder\b",
    "location": r"street|address|\bcity\b|postal|post ?code|\bzip\b|district|\btown\b",
    "country": r"\bcountry\b",
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_IBAN_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{8,30}$")

_MIN_IDENTIFIER_DISTINCT = 0.7  # identifiers are mostly unique per record
_MAX_DISPLAY_COLUMNS = 10


def _column_text(table: str, column: str, dictionary: Optional[Dict[Tuple[str, str], str]]) -> Tuple[str, str]:
    """(searchable text, dictionary text) - column name words plus its dictionary entry."""
    described = (dictionary or {}).get((table.upper(), column.upper()), "")
    if described.startswith("No description"):
        described = ""
    name_words = re.sub(r"([a-z])([A-Z])", r"\1 \2", column)
    name_words = re.sub(r"[_\-.]+", " ", name_words)
    return f"{name_words} {described}".lower(), described


def _matches(role: str, text: str) -> bool:
    return re.search(_ROLE_PATTERNS[role], text) is not None


def _share(series: pd.Series, pattern: re.Pattern) -> float:
    values = series.dropna().astype(str).str.strip()
    values = values[values != ""]
    if values.empty:
        return 0.0
    sample = values.head(500)
    return sample.map(lambda v: bool(pattern.match(v))).mean()


def _stats(series: pd.Series) -> Dict[str, float]:
    non_null = series.dropna()
    non_null = non_null[non_null.astype(str).str.strip() != ""]
    count = len(series) or 1
    return {
        "non_null": len(non_null) / count,
        "distinct": (non_null.nunique() / len(non_null)) if len(non_null) else 0.0,
        "avg_len": non_null.astype(str).str.len().mean() if len(non_null) else 0.0,
    }


def infer_rules(table: str, df: pd.DataFrame,
                dictionary: Optional[Dict[Tuple[str, str], str]] = None) -> Dict[str, Any]:
    why: Dict[str, str] = {}
    info = {}
    for col in df.columns:
        text, described = _column_text(table, col, dictionary)
        info[col] = {"text": text, "described": described, **_stats(df[col])}

    def source(col: str) -> str:
        return f"dictionary: '{info[col]['described']}'" if info[col]["described"] else "column name"

    # --- key -----------------------------------------------------------------
    primary = [c for c in df.columns if _matches("primary_key", info[c]["text"])]
    foreign = [c for c in df.columns if _matches("foreign_key", info[c]["text"])]
    if primary:
        key, key_unique = primary, True
        for c in primary:
            why[c] = f"key - {source(c)} (a repeated key is a duplicate)"
    elif foreign:
        key, key_unique = foreign[:1], False
        why[foreign[0]] = (f"owner key - {source(foreign[0])} (rows of the same "
                           f"{foreign[0]} are one object and are not compared)")
    else:
        # No dictionary hint: an ID-like column whose values are all present and unique
        # identifies the record (used to label rows and remember decisions - not as a check).
        id_like = [c for c in df.columns
                   if re.search(r"\b(id|no|nr|num|number|code|key)\b", info[c]["text"])
                   and not _matches("identifier", info[c]["text"])
                   and info[c]["non_null"] == 1.0 and info[c]["distinct"] == 1.0]
        key, key_unique = id_like[:1], False
        if id_like:
            why[id_like[0]] = f"record id - {source(id_like[0])}, every value present and unique"
    used = set(key)

    # --- identifiers ---------------------------------------------------------
    identifiers: List[List[str]] = []
    for col in df.columns:
        if col in used:
            continue
        c = info[col]
        # Bank account numbers are only unique together with the bank - handled below -
        # except an IBAN, which is globally unique on its own.
        by_text = _matches("identifier", c["text"]) and (
            not _matches("bank_account", c["text"]) or re.search(r"\biban\b", c["text"]) is not None)
        # Value formats: only distinctive ones. Phone numbers are recognised by name/dictionary
        # only - digit-and-dash patterns also match dates and account numbers.
        email = iban = 0.0
        if not by_text and df[col].dtype == object:
            email, iban = _share(df[col], _EMAIL_RE), _share(df[col], _IBAN_RE)
        if not (by_text or email >= 0.8 or iban >= 0.8):
            continue
        if c["non_null"] < 0.1 or c["distinct"] < _MIN_IDENTIFIER_DISTINCT:
            continue
        identifiers.append([col])
        used.add(col)
        why[col] = (f"identifier - {source(col)}" if by_text
                    else f"identifier - values look like {'e-mail addresses' if email >= 0.8 else 'IBANs'}")

    accounts = [c for c in df.columns if c not in used and _matches("bank_account", info[c]["text"])]
    bank_parts = [c for c in df.columns if c not in used and _matches("bank_part", info[c]["text"])]
    for account in accounts:
        if info[account]["non_null"] < 0.1:
            continue
        composite = bank_parts + [account]
        identifiers.append(composite)
        used.update(composite)
        why[account] = (f"identifier together with {', '.join(bank_parts)} - {source(account)}"
                        if bank_parts else f"identifier - {source(account)}")
        for part in bank_parts:
            why[part] = f"part of the bank account identifier - {source(part)}"

    # --- location ------------------------------------------------------------
    location = [c for c in df.columns if c not in used and _matches("location", info[c]["text"])
                and not _matches("country", info[c]["text"])]
    for c in location:
        why[c] = f"location - {source(c)}"
    used.update(location)

    # --- name ----------------------------------------------------------------
    name = None
    candidates = [c for c in df.columns if c not in used and _matches("name", info[c]["text"])
                  and info[c]["non_null"] >= 0.3 and info[c]["distinct"] >= 0.3 and info[c]["avg_len"] >= 3]
    if candidates:
        name = max(candidates, key=lambda c: info[c]["non_null"] * info[c]["distinct"])
        used.add(name)
        why[name] = (f"name - {source(name)}" if location
                     else f"name - {source(name)}, but NOT used for matching: no location columns to confirm a name match")

    countries = [c for c in df.columns if c not in used and _matches("country", info[c]["text"])]
    display = [c for c in [name, *location, *countries, *(c for ids in identifiers for c in ids)] if c]
    display = list(dict.fromkeys(display))[:_MAX_DISPLAY_COLUMNS] or [c for c in df.columns if c not in key][:8]

    checks = ["identical rows"]
    if key_unique:
        checks.append(f"repeated {' + '.join(key)}")
    if identifiers:
        checks.append("shared " + ", ".join(" + ".join(i) for i in identifiers))
    if name and location:
        checks.append(f"same/similar {name} confirmed by {', '.join(location)}")

    return {
        "source": "inferred",
        "key": key,
        "key_unique": key_unique,
        "name": name if location else None,
        "identifiers": identifiers,
        "location": location,
        "display": display,
        "label": "records",
        "checks": checks,
        "why": why,
    }


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def resolve_rules(table: str, df: pd.DataFrame,
                  dictionary: Optional[Dict[Tuple[str, str], str]] = None) -> Dict[str, Any]:
    """config.yaml override for this table if present, otherwise inferred rules."""
    raw = Config.DUPLICATE_TABLE_RULES.get(table)
    if not raw:
        return infer_rules(table, df, dictionary)
    identifiers = [_as_list(i) for i in raw.get("identifiers", [])]
    location = _as_list(raw.get("location"))
    key = _as_list(raw.get("key"))
    checks = ["identical rows"]
    if raw.get("key_unique"):
        checks.append(f"repeated {' + '.join(key)}")
    if identifiers:
        checks.append("shared " + ", ".join(" + ".join(i) for i in identifiers))
    if raw.get("name") and location:
        checks.append(f"same/similar {raw['name']} confirmed by {', '.join(location)}")
    return {
        "source": "config.yaml",
        "key": key,
        "key_unique": bool(raw.get("key_unique", False)),
        "name": raw.get("name"),
        "identifiers": identifiers,
        "location": location,
        "display": _as_list(raw.get("display")),
        "label": raw.get("label", "records"),
        "checks": checks,
        "why": {},
    }
