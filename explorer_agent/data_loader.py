"""
Loads tabular data locally. Deliberately dumb/simple - no LLM involvement here.
Extend with DB connectors (SAP HANA/Oracle/etc.) later without touching Explorer.
"""

"""
Loads tabular data locally. Deliberately dumb/simple - no LLM involvement here.
"""

from pathlib import Path
from typing import Dict, List, Tuple
import pandas as pd


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