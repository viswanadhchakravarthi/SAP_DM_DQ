"""Offline postal directory (GeoNames extract) - city and region for a country + postal code.

``data/reference/geo_postal.db`` is a static SQLite file built once by
``python -m src.agents.tools.build_geo_postal`` from GeoNames' free per-country
postal code files. Lookups are local and take well under a millisecond; nothing is
fetched at run time. Without the file every function returns None / False, so the
agent behaves exactly as before and rules that use it are skipped.

GeoNames lists several places for some postal codes. A value is returned only when
the directory is unambiguous (one distinct place name), because a wrong auto-fill is
worse than a manual one.
"""

import re
import sqlite3
from functools import lru_cache
from typing import Dict, Optional

from src.agents.config import Config
from src.agents.logging_config import get_logger

logger = get_logger("geo_reference")

_conn: Optional[sqlite3.Connection] = None
_checked = False


def _db() -> Optional[sqlite3.Connection]:
    global _conn, _checked
    if _checked:
        return _conn
    _checked = True
    path = Config.GEO_POSTAL_DB
    if not path.exists():
        logger.info("No offline postal directory at %s - postal-code reference checks are skipped", path)
        return None
    _conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, check_same_thread=False)
    return _conn


def available() -> bool:
    return _db() is not None


def _key(country: str, postal: str) -> str:
    """GeoNames stores GB as the outward code ('EC1A') and CA as the forward sortation area ('K1A')."""
    value = re.sub(r"\s+", " ", str(postal).strip().upper())
    if country == "GB":
        return value.split(" ")[0]
    if country == "CA":
        return value.replace(" ", "")[:3]
    return value


@lru_cache(maxsize=50_000)
def _places(country: str, postal: str):
    conn = _db()
    if conn is None or not postal:
        return ()
    rows = conn.execute("SELECT DISTINCT place, region_code, region FROM postal WHERE country = ? AND postal = ?",
                        (country, _key(country, postal))).fetchall()
    return tuple(rows)


def known_country(country: str) -> bool:
    """True when the directory has any postal code for this country (so a miss means 'not a real code')."""
    conn = _db()
    if conn is None:
        return False
    return conn.execute("SELECT 1 FROM postal WHERE country = ? LIMIT 1", (str(country).upper(),)).fetchone() is not None


def city_for(country: str, postal: str) -> Optional[str]:
    """The place name for this postal code, or None when unknown or ambiguous."""
    places = {p[0] for p in _places(str(country).upper(), str(postal))}
    return next(iter(places)) if len(places) == 1 else None


def region_for(country: str, postal: str) -> Optional[Dict[str, str]]:
    """{'code', 'name'} of the region (state / province) for this postal code, or None."""
    regions = {(p[1], p[2]) for p in _places(str(country).upper(), str(postal)) if p[1] or p[2]}
    if len(regions) != 1:
        return None
    code, name = next(iter(regions))
    return {"code": code, "name": name}


def exists(country: str, postal: str) -> Optional[bool]:
    """True / False when the directory covers the country, None when it cannot tell."""
    country = str(country).upper()
    if not known_country(country):
        return None
    return bool(_places(country, str(postal)))
