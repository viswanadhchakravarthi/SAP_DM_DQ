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
    """Every CSV in ``data_dir`` except the data dictionary: {"LFA1": "LFA1.csv", ...}.

    Tables are no longer a fixed list in config.yaml - whatever table files a
    client provides are profiled.
    """
    skip = (dictionary_file or "").lower()
    tables: Dict[str, str] = {}
    for path in sorted(Path(data_dir).glob("*.csv")):
        if path.name.lower() == skip:
            continue
        name = table_name_from_filename(path.name)
        if name in tables:
            raise ValueError(f"Files '{tables[name]}' and '{path.name}' both map to table {name}")
        tables[name] = path.name
    return tables


def load_table(path: str, sheet_name=None) -> pd.DataFrame:
    p = Path(path)
    if p.suffix.lower() in (".xlsx", ".xls"):
        return pd.read_excel(p, sheet_name=sheet_name or 0)
    elif p.suffix.lower() == ".csv":
        return pd.read_csv(p)
    else:
        raise ValueError(f"Unsupported file type: {p.suffix}")


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


def load_all_tables(data_dir: str, table_files: Dict[str, str]) -> Dict[str, pd.DataFrame]:
    """
    table_files: {"LFA1": "LFA1.csv", "LFB1": "LFB1.csv", ...}
    Returns: {"LFA1": <DataFrame>, "LFB1": <DataFrame>, ...}
    """
    base = Path(data_dir)
    tables = {}
    for table_name, filename in table_files.items():
        tables[table_name] = load_table(str(base / filename))
    return tables