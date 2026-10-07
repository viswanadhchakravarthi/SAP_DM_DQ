"""Build the offline postal directory ``explorer_agent/reference_data/geo_postal.db``.

A developer-time tool, run once and again only to refresh the data; the agent never
downloads anything while it runs. Source: GeoNames postal code extracts
(https://download.geonames.org/export/zip/, Creative Commons Attribution 4.0 - keep the
attribution in reference_data/README.md).

    python -m explorer_agent.tools.build_geo_postal                       # default countries, downloads
    python -m explorer_agent.tools.build_geo_postal --countries IN US DE
    python -m explorer_agent.tools.build_geo_postal --from-dir C:\\geonames   # pre-downloaded <CC>.zip files, no network

GeoNames lines are tab separated: country, postal code, place, admin1 name, admin1 code,
admin2 name, admin2 code, admin3 name, admin3 code, latitude, longitude, accuracy. Only
country, postal code, place, region code and region name are kept (a few MB for the defaults).
"""

import argparse
import io
import sqlite3
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterable, List

from ..config import Config

URL = "https://download.geonames.org/export/zip/{cc}.zip"
DEFAULT_COUNTRIES = ["IN", "US", "DE", "GB", "FR", "NL", "BE", "AT", "CH", "IT", "ES", "PT", "PL", "SE", "DK",
                     "NO", "FI", "IE", "CA", "AU", "SG", "JP", "MX", "BR", "ZA", "BD", "TR", "AE"]

SCHEMA = """
CREATE TABLE postal (country TEXT NOT NULL, postal TEXT NOT NULL, place TEXT NOT NULL,
                     region_code TEXT, region TEXT);
CREATE INDEX postal_lookup ON postal (country, postal);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _read_zip(country: str, source_dir: Path | None) -> bytes:
    if source_dir:
        return (source_dir / f"{country}.zip").read_bytes()
    with urllib.request.urlopen(URL.format(cc=country), timeout=60) as response:
        return response.read()


def _rows(country: str, payload: bytes) -> Iterable[tuple]:
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        with archive.open(f"{country}.txt") as handle:
            for line in io.TextIOWrapper(handle, encoding="utf-8"):
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 5 or not parts[1].strip():
                    continue
                yield country, parts[1].strip().upper(), parts[2].strip(), parts[4].strip(), parts[3].strip()


def build(countries: List[str], target: Path, source_dir: Path | None = None) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    conn = sqlite3.connect(tmp)
    conn.executescript(SCHEMA)
    total = 0
    for country in countries:
        try:
            rows = list(_rows(country, _read_zip(country, source_dir)))
        except Exception as exc:
            print(f"  {country}: skipped ({type(exc).__name__}: {exc})")
            continue
        conn.executemany("INSERT INTO postal VALUES (?, ?, ?, ?, ?)", rows)
        total += len(rows)
        print(f"  {country}: {len(rows):,} postal codes")
    conn.execute("INSERT INTO meta VALUES ('source', 'GeoNames postal codes, CC BY 4.0, https://www.geonames.org')")
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    tmp.replace(target)
    print(f"Wrote {target} ({total:,} rows, {target.stat().st_size / 1e6:.1f} MB)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--countries", nargs="+", default=DEFAULT_COUNTRIES, help="ISO 3166-1 alpha-2 codes")
    parser.add_argument("--from-dir", type=Path, help="folder with pre-downloaded <CC>.zip files (no network)")
    parser.add_argument("--output", type=Path, default=Config.GEO_POSTAL_DB)
    args = parser.parse_args()
    build([c.upper() for c in args.countries], args.output, args.from_dir)


if __name__ == "__main__":
    main()
