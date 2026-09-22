"""
Loads tabular data locally. Deliberately dumb/simple - no LLM involvement here.
Extend with DB connectors (SAP HANA/Oracle/etc.) later without touching Explorer.
"""

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import pandas as pd

# Required headers of a data dictionary CSV (Data_Type / Notes are optional).
DICTIONARY_REQUIRED_COLUMNS = ("Table", "Field", "Description")
_TABLE_NAME_MAX_LEN = 30


def table_name_from_filename(filename: str) -> str:
    """Table name for an uploaded/discovered file: 'lfa1.csv' -> 'LFA1', 'Vendor Master.csv' -> 'VENDOR_MASTER'."""
    stem = Path(filename).stem
    name = re.sub(r"[^A-Z0-9_]+", "_", stem.upper()).strip("_")
    if not name or not name[0].isalpha():
        raise ValueError(f"Can't derive a table name from '{filename}' - the file name must start with a letter")
    if len(name) > _TABLE_NAME_MAX_LEN:
        raise ValueError(f"Table name '{name}' is longer than {_TABLE_NAME_MAX_LEN} characters")
    return name


def discover_table_files(data_dir: str, dictionary_file: Optional[str] = None) -> Dict[str, str]:
    """Every CSV under ``data_dir`` except the data dictionary: {"LFA1": "LFA1.csv", ...}.

    Tables are no longer a fixed list in config.yaml - whatever table files a
    client provides are profiled.

    The search is recursive, so one client can group its tables by business
    object (``vendor-master/LFA1.csv``, ``material-master/MARA.csv``, ...) instead
    of piling every file into one folder. The table name still comes from the
    file name alone, so the layout is presentation only: ``vendor-master/LFA1.csv``
    is still table LFA1. Values are paths relative to ``data_dir``.
    """
    skip = (dictionary_file or "").lower()
    base = Path(data_dir)
    tables: Dict[str, str] = {}
    for path in sorted(base.rglob("*.csv")):
        if path.name.lower() == skip:
            continue
        name = table_name_from_filename(path.name)
        relative = path.relative_to(base).as_posix()
        if name in tables:
            raise ValueError(f"Files '{tables[name]}' and '{relative}' both map to table {name}")
        tables[name] = relative
    return tables


# --------------------------------------------------------------------------
# Column typing
#
# Letting pandas infer dtypes corrupts SAP data. Keys are zero-padded CHAR
# fields: MATNR '000000000000100001' infers to int 100001 and LIFNR '0000070061'
# to 70061, so the padding - which is part of the value - is gone. Worse, a
# numeric column holding a blank infers to float64, which is only exact to 2**53
# (~9.0e15); an 18-character MATNR reaches 1e17, so two different materials can
# land on the same float and be reported as duplicates of each other.
#
# So nothing is inferred: every file is read as text, and a column is converted
# back to a number only when it is safe. The data dictionary's SAP data type is
# the authority (CHAR/NUMC/CUKY/UNIT/DATE are text even when they look numeric;
# DEC/QUAN/CURR/INT/FLTP are measures). Columns the dictionary does not describe
# fall back to a conservative test.
# --------------------------------------------------------------------------

_TEXT_TYPE_RE = re.compile(r"^(CHAR|NUMC|CUKY|UNIT|LANG|CLNT|DATS?|TIMS|STRING|SSTR|RAW|LCHR)", re.I)
_NUMERIC_TYPE_RE = re.compile(r"^(DEC|QUAN|CURR|INT|FLTP|PREC)", re.I)
_NUMBER_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)$")
_LEADING_ZERO_RE = re.compile(r"^[+-]?0\d")
# float64 holds integers exactly up to 2**53; stay well inside it.
_MAX_SAFE_DIGITS = 15


def _looks_numeric(series: pd.Series) -> bool:
    """True when a text column can become a number without losing information."""
    values = series.dropna().astype(str).str.strip()
    values = values[values != ""]
    if values.empty:
        return False
    if not values.map(lambda v: bool(_NUMBER_RE.match(v))).all():
        return False
    # '0000070061' is an identifier, not the number seventy thousand.
    if values.map(lambda v: bool(_LEADING_ZERO_RE.match(v))).any():
        return False
    # An 18-digit MATNR cannot survive a float64 round trip.
    if values.map(lambda v: len(v.split(".")[0].lstrip("+-"))).max() > _MAX_SAFE_DIGITS:
        return False
    return True


def _to_number(series: pd.Series) -> pd.Series:
    """Numeric version of a text column, keeping whole numbers out of float64.

    pandas turns an integer column containing a blank into float64, which is why
    a 13-digit EAN used to reach the reviewer as '4067063370378.0'. Nullable
    Int64 holds the integer and the blank at the same time.
    """
    numeric = pd.to_numeric(series, errors="coerce")
    non_null = numeric.dropna()
    if not non_null.empty and (non_null % 1 == 0).all():
        try:
            return numeric.astype("Int64")
        except (TypeError, ValueError, OverflowError):
            return numeric
    return numeric


def apply_column_types(df: pd.DataFrame, column_types: Optional[Dict[str, str]] = None) -> pd.DataFrame:
    """Convert the measure columns of an all-text DataFrame back to numbers.

    ``column_types`` maps UPPERCASE column name -> SAP data type from the data
    dictionary. Anything it does not cover is decided by ``_looks_numeric``.
    """
    types = {str(k).strip().upper(): str(v or "").strip() for k, v in (column_types or {}).items()}
    for col in df.columns:
        declared = types.get(str(col).strip().upper(), "")
        if declared:
            if _TEXT_TYPE_RE.match(declared):
                continue                      # CHAR/NUMC/DATE/... stays text
            if _NUMERIC_TYPE_RE.match(declared):
                df[col] = _to_number(df[col])
                continue
        if _looks_numeric(df[col]):           # undeclared: only when provably safe
            df[col] = _to_number(df[col])
    return df


def load_table(path: str, sheet_name=None,
               column_types: Optional[Dict[str, str]] = None) -> pd.DataFrame:
    """Read a table as text, then restore only the columns that are really numeric.

    ``column_types``: {COLUMN: SAP data type} from the data dictionary, when the
    caller has one. See the note above for why nothing is inferred.
    """
    p = Path(path)
    if p.suffix.lower() in (".xlsx", ".xls"):
        df = pd.read_excel(p, sheet_name=sheet_name or 0, dtype=str)
    elif p.suffix.lower() == ".csv":
        df = pd.read_csv(p, dtype=str)
    else:
        raise ValueError(f"Unsupported file type: {p.suffix}")
    return apply_column_types(df, column_types)


def load_data_dictionary(path: str) -> Dict[Tuple[str, str], str]:
    """
    Loads a data dictionary CSV into a lookup: (table_name, field_name) -> description.

    IMPORTANT: inspect your actual Data_Dictionary.csv headers first -
    this assumes columns roughly named 'Table', 'Field', 'Description'.
    Adjust the column name lookups below to match your real file.
    """
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]  # defensive: strip whitespace from headers

    # Adjust these three names to match your actual CSV headers
    table_col, field_col, desc_col, datatype_col, notes_col = "Table", "Field", "Description", "Data_Type", "Notes"

    lookup = {}
    for _, row in df.iterrows():
        table = str(row.get(table_col, "")).strip().upper()
        field = str(row.get(field_col, "")).strip().upper()
        desc = str(row.get(desc_col, "")).strip()
        datatype = str(row.get(datatype_col, "")).strip()
        notes = str(row.get(notes_col, "")).strip()

        if table and field:
            lookup[(table, field)] = f"{desc} - {datatype} - {notes}"
    return lookup


def get_field_description(dictionary: Dict[Tuple[str, str], str], table_name: str, column: str) -> str:
    return dictionary.get((table_name.upper(), column.upper()), "No description available")


def load_data_dictionary_structured(path: str) -> Dict[str, Dict[str, str]]:
    """
    Loads the same Data_Dictionary.csv as load_data_dictionary(), but keeps
    description/data_type/notes as separate fields instead of concatenating
    them - used by review_app to render structured hover tooltips.

    Returns: {"TABLE.FIELD": {"description":..., "data_type":..., "notes":...}}
    """
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]

    table_col, field_col, desc_col, datatype_col, notes_col = "Table", "Field", "Description", "Data_Type", "Notes"

    lookup = {}
    for _, row in df.iterrows():
        table = str(row.get(table_col, "")).strip().upper()
        field = str(row.get(field_col, "")).strip().upper()
        if not table or not field:
            continue
        lookup[f"{table}.{field}"] = {
            "description": str(row.get(desc_col, "")).strip(),
            "data_type": str(row.get(datatype_col, "")).strip(),
            "notes": str(row.get(notes_col, "")).strip(),
        }
    return lookup


def dictionary_column_types(path: str) -> Dict[Tuple[str, str], str]:
    """{(TABLE, FIELD): SAP data type} from a data dictionary CSV.

    Feeds load_all_tables so a CHAR key is not silently turned into a number.
    """
    types: Dict[Tuple[str, str], str] = {}
    for full, entry in load_data_dictionary_structured(path).items():
        table, _, field = full.partition(".")
        if table and field:
            types[(table.upper(), field.upper())] = entry.get("data_type", "")
    return types


def load_all_tables(data_dir: str, table_files: Dict[str, str],
                    column_types: Optional[Dict[Tuple[str, str], str]] = None
                    ) -> Dict[str, pd.DataFrame]:
    """
    table_files: {"LFA1": "LFA1.csv", "LFB1": "LFB1.csv", ...}
    column_types: {(TABLE, FIELD): SAP data type}, from dictionary_column_types().
    Returns: {"LFA1": <DataFrame>, "LFB1": <DataFrame>, ...}
    """
    base = Path(data_dir)
    tables = {}
    for table_name, filename in table_files.items():
        per_table = {field: dtype for (tbl, field), dtype in (column_types or {}).items()
                     if tbl == table_name.upper()}
        tables[table_name] = load_table(str(base / filename), column_types=per_table)
    return tables