"""Offline check-digit validation for bank and tax identifiers.

* IBAN (ISO 13616 structure + ISO 7064 mod-97 check digits) and BIC/SWIFT: ``schwifty``.
* VAT, GSTIN, PAN, EIN and national tax numbers: ``python-stdnum``.

Both are pure-Python libraries with their rules bundled - no network, no data leaves
the process. Neither is imported until used, and a missing library only switches the
rules that need it off (``available()``), so the agent runs without them.

A regex says a tax number *looks* right; a check digit says it *is* a possible number.
The rule pack (``tax.checksum``) maps the format names it already uses ("GSTIN", "PAN",
"EU VAT", ...) to the stdnum module that validates them. A value is reported only when
every format it fits has a validator and all of them reject it - a number that fits a
format nobody can check (a US 9-digit number is an EIN *or* an SSN) is never guessed at.
"""

import importlib
import re
from functools import lru_cache
from typing import Any, Dict, List, Optional

from .logging_config import get_logger

logger = get_logger("checksums")


@lru_cache(maxsize=None)
def _stdnum(module: str):
    try:
        return importlib.import_module(f"stdnum.{module}")
    except ImportError:
        logger.warning("python-stdnum module %r is not available - its numbers are not checksum-validated", module)
        return None


def stdnum_available() -> bool:
    return _stdnum("util") is not None


@lru_cache(maxsize=1)
def _schwifty():
    try:
        import schwifty
        return schwifty
    except ImportError:
        logger.warning("schwifty is not installed - IBAN / BIC check digits are not validated")
        return None


def iban_available() -> bool:
    return _schwifty() is not None


# ---------------------------------------------------------------------------
# Tax numbers
# ---------------------------------------------------------------------------

def tax_valid(pack: Dict[str, Any], country: str, format_name: str, value: str) -> Optional[bool]:
    """True / False when a validator exists for this country's format, None when none does."""
    entry = _checksum_entry(pack, country, format_name)
    if not entry:
        return None
    verdicts = []
    for name in entry:
        module = _stdnum(name)
        if module is None:
            return None
        try:
            verdicts.append(bool(module.is_valid(value)))
        except Exception:  # a validator that chokes on odd input is a rejection, not a crash
            verdicts.append(False)
    return any(verdicts)


def _checksum_entry(pack: Dict[str, Any], country: str, format_name: str) -> List[str]:
    spec = (pack.get("tax", {}) or {}).get("checksum", {}) or {}
    if format_name == "EU VAT":
        found = spec.get("EU VAT")
    else:
        found = (spec.get(country) or {}).get(format_name)
    return [found] if isinstance(found, str) else list(found or [])


# ---------------------------------------------------------------------------
# Bank identifiers
# ---------------------------------------------------------------------------

_IBAN_PROBLEMS = {
    "InvalidChecksumDigits": "the check digits are wrong (ISO 7064 mod-97)",
    "InvalidStructure": "the length or characters do not fit the country's IBAN structure",
    "InvalidLength": "the length is wrong for the country",
    "InvalidCountryCode": "the country code is not an IBAN country",
    "InvalidBBANChecksum": "the national check digits inside the account number are wrong",
    "InvalidBBANStructure": "the account number part does not fit the country's structure",
    "InvalidBankCode": "the bank code is not a known bank",
}


def clean_iban(value: Any) -> str:
    return re.sub(r"\s+", "", str(value)).upper()


def iban_problem(value: str) -> Optional[str]:
    """None when the IBAN is valid, else a sentence saying what is wrong (value must be cleaned)."""
    schwifty = _schwifty()
    if schwifty is None or not value:
        return None
    try:
        schwifty.IBAN(value, validate_bban=False)
        return None
    except Exception as exc:
        return _IBAN_PROBLEMS.get(type(exc).__name__, f"it is not a valid IBAN ({type(exc).__name__})")


def iban_country(value: str) -> str:
    return value[:2]


def bic_problem(value: str) -> Optional[str]:
    schwifty = _schwifty()
    if schwifty is None or not value:
        return None
    try:
        schwifty.BIC(value, allow_invalid=False)
        return None
    except Exception as exc:
        return f"it is not a valid BIC/SWIFT code ({str(exc).splitlines()[0][:80] if str(exc) else type(exc).__name__})"
