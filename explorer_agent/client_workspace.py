"""Per-client data workspace - the files a client's runs profile.

Uploaded on the review app's first page and kept separate from learned
knowledge (``client_knowledge``, under memory_store/) because this is raw,
potentially sensitive client data:

    <data.client_data_dir>/<client_id>/
        workspace.json          metadata: dictionary + tables (rows, columns, upload time)
        Data_Dictionary.csv     the data dictionary, under its uploaded file name
        LFA1.csv, LFB1.csv ...  one CSV per table, named <TABLE>.csv

A run for the client uses this folder as ``--data-dir``; ``main.py`` discovers
the tables from the CSV files in it, so the set of tables is whatever was
uploaded rather than a fixed list.
"""

import json
import os
import re
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from . import client_knowledge
from .config import Config
from .data_loader import DICTIONARY_REQUIRED_COLUMNS, load_data_dictionary_structured, table_name_from_filename

_lock = threading.Lock()


class WorkspaceError(ValueError):
    """An upload or workspace change was rejected (message is safe to show users)."""


def workspace_dir(client_id: str) -> Path:
    # client_id_for() both validates and canonicalizes, so a crafted id can't
    # escape the client data folder.
    if client_knowledge.client_id_for(client_id) != client_id:
        raise WorkspaceError(f"Invalid client id: {client_id!r}")
    return Path(Config.CLIENT_DATA_DIR) / client_id


def _metadata_path(client_id: str) -> Path:
    return workspace_dir(client_id) / "workspace.json"


def _read_metadata(client_id: str) -> Dict[str, Any]:
    path = _metadata_path(client_id)
    if not path.exists():
        return {"dictionary": None, "tables": {}}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_metadata(client_id: str, data: Dict[str, Any]) -> None:
    path = _metadata_path(client_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def get_workspace(client_id: str) -> Dict[str, Any]:
    """Workspace summary for the UI and for starting runs."""
    meta = _read_metadata(client_id)
    base = workspace_dir(client_id)
    dictionary = meta.get("dictionary")
    if dictionary and not (base / dictionary["file"]).exists():
        dictionary = None
    tables = [
        {"table": name, **info}
        for name, info in sorted(meta.get("tables", {}).items())
        if (base / info["file"]).exists()
    ]
    return {
        "client_id": client_id,
        "data_dir": str(base),
        "dictionary": dictionary,
        "tables": tables,
        "ready": bool(dictionary and tables),
    }


def _safe_filename(filename: str) -> str:
    name = Path(filename or "").name
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(name).stem).strip("._")
    if not stem:
        raise WorkspaceError(f"Invalid file name: {filename!r}")
    if Path(name).suffix.lower() != ".csv":
        raise WorkspaceError(f"'{name}' is not a .csv file")
    return f"{stem}.csv"


def new_upload_path(client_id: str) -> Path:
    """Temporary file inside the workspace to stream an upload into."""
    base = workspace_dir(client_id)
    base.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=base, prefix=".upload-", suffix=".tmp")
    os.close(fd)
    return Path(tmp)


def _read_csv(tmp_path: Path, display_name: str) -> pd.DataFrame:
    try:
        df = pd.read_csv(tmp_path)
    except Exception as exc:  # pandas raises several parser/encoding error types
        raise WorkspaceError(f"'{display_name}' could not be read as CSV: {exc}") from exc
    if df.shape[1] == 0 or df.shape[0] == 0:
        raise WorkspaceError(f"'{display_name}' has no data rows")
    return df


def _safe_subdir(subdir: Optional[str]) -> str:
    """Sanitized relative folder inside the workspace, or "" - never escapes it."""
    if not subdir:
        return ""
    parts = []
    for raw in Path(str(subdir)).parts:
        part = re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("._")
        if part and part not in (".", ".."):
            parts.append(part)
    return "/".join(parts)


def save_table(client_id: str, original_filename: str, tmp_path: Path,
               subdir: Optional[str] = None) -> Dict[str, Any]:
    """Validate an uploaded table CSV and store it as <TABLE>.csv (replacing that table).

    ``subdir`` optionally files it under a folder inside the workspace, so a
    client can group its tables by business object. The table name comes from the
    file name either way, so the folder is presentation only (see
    data_loader.discover_table_files, which searches recursively).
    """
    try:
        _safe_filename(original_filename)
        try:
            table = table_name_from_filename(original_filename)
        except ValueError as exc:
            raise WorkspaceError(str(exc)) from exc
        df = _read_csv(tmp_path, original_filename)

        with _lock:
            meta = _read_metadata(client_id)
            dictionary = meta.get("dictionary")
            if dictionary and re.sub(r"[^A-Z0-9_]+", "_", Path(dictionary["file"]).stem.upper()).strip("_") == table:
                raise WorkspaceError(f"'{original_filename}' has the same name as the data dictionary")
            folder = _safe_subdir(subdir)
            target = f"{folder}/{table}.csv" if folder else f"{table}.csv"
            previous = (meta.get("tables", {}).get(table) or {}).get("file")
            destination = workspace_dir(client_id) / target
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(tmp_path, destination)
            # A table that moved to a different folder must not be left behind twice,
            # or discovery would see the same table under two paths and refuse the run.
            if previous and previous != target:
                (workspace_dir(client_id) / previous).unlink(missing_ok=True)
            entry = {
                "file": target,
                "original_name": Path(original_filename).name,
                "rows": int(df.shape[0]),
                "columns": int(df.shape[1]),
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            }
            # A re-upload keeps the reviewer's helper columns that still exist in the new file.
            kept = [c for c in (meta.get("tables", {}).get(table) or {}).get("helper_columns", [])
                    if c in set(map(str, df.columns))]
            if kept:
                entry["helper_columns"] = kept
            meta.setdefault("tables", {})[table] = entry
            _write_metadata(client_id, meta)
        return get_workspace(client_id)
    finally:
        tmp_path.unlink(missing_ok=True)


def save_dictionary(client_id: str, original_filename: str, tmp_path: Path) -> Dict[str, Any]:
    """Validate an uploaded data dictionary CSV and make it the client's dictionary."""
    try:
        target = _safe_filename(original_filename)
        df = _read_csv(tmp_path, original_filename)
        headers = {str(c).strip() for c in df.columns}
        missing = [c for c in DICTIONARY_REQUIRED_COLUMNS if c not in headers]
        if missing:
            raise WorkspaceError(
                f"'{original_filename}' doesn't look like a data dictionary - missing column(s): {', '.join(missing)} "
                f"(expected {', '.join(DICTIONARY_REQUIRED_COLUMNS)}, optionally Data_Type and Notes)")

        table_col = next(c for c in df.columns if str(c).strip() == "Table")

        with _lock:
            meta = _read_metadata(client_id)
            try:
                clashes = table_name_from_filename(target) in meta.get("tables", {})
            except ValueError:
                clashes = False
            if clashes:
                raise WorkspaceError(f"'{original_filename}' has the same name as an uploaded table")
            base = workspace_dir(client_id)
            previous = meta.get("dictionary")
            os.replace(tmp_path, base / target)
            if previous and previous["file"].lower() != target.lower():
                (base / previous["file"]).unlink(missing_ok=True)
            meta["dictionary"] = {
                "file": target,
                "rows": int(df.shape[0]),
                "tables_described": int(df[table_col].nunique()),
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            }
            _write_metadata(client_id, meta)
        return get_workspace(client_id)
    finally:
        tmp_path.unlink(missing_ok=True)


def remove_table(client_id: str, table: str) -> Dict[str, Any]:
    with _lock:
        meta = _read_metadata(client_id)
        info = meta.get("tables", {}).pop(table, None)
        if info is None:
            raise KeyError(table)
        (workspace_dir(client_id) / info["file"]).unlink(missing_ok=True)
        _write_metadata(client_id, meta)
    return get_workspace(client_id)


def clear_files(client_id: str) -> Dict[str, Any]:
    """Remove every uploaded data file of a client - the dictionary, all tables and any
    other CSV in its folder (a run profiles every CSV there, listed or not) - so a new
    set can be uploaded. Sub-folders left empty are removed too.

    Kept: the Mapping Agent / Metadata Repository inputs (field_mapping.json,
    target_domains.json - not uploaded on page 1), and everything outside the folder:
    findings and review state (episodic DB), and the client's memory
    (memory_store/clients/<client>: duplicate decisions and rules, column mappings)."""
    base = workspace_dir(client_id)
    removed = 0
    with _lock:
        if base.exists():
            for path in sorted(base.rglob("*.csv")):
                path.unlink(missing_ok=True)
                removed += 1
            for folder in sorted((p for p in base.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
                if not any(folder.iterdir()):
                    folder.rmdir()
        _write_metadata(client_id, {"dictionary": None, "tables": {}})
    return {**get_workspace(client_id), "removed_files": removed}


def seed_from_folder(client_id: str, source_dir: str, dictionary_file: str) -> Dict[str, Any]:
    """Copy an existing data folder (e.g. the repo's data/) into a client's workspace.

    Recursive, and the source's folder structure is preserved, so a source laid
    out by business object keeps that layout in the workspace.
    """
    source = Path(source_dir)
    tmp = new_upload_path(client_id)
    shutil.copyfile(source / dictionary_file, tmp)
    save_dictionary(client_id, dictionary_file, tmp)
    for path in sorted(source.rglob("*.csv")):
        if path.name.lower() != dictionary_file.lower():
            folder = path.relative_to(source).parent.as_posix()
            tmp = new_upload_path(client_id)
            shutil.copyfile(path, tmp)
            save_table(client_id, path.name, tmp, subdir=None if folder == "." else folder)
    return get_workspace(client_id)


# --------------------------------------------------------------------------- #
# Helper columns: context columns a reviewer wants to see next to flagged records
# --------------------------------------------------------------------------- #
# Chosen per table on page 1 and saved here. They change nothing about what is analysed
# (every column still is); the review page only shows their values beside each flagged
# record, read from the uploaded CSV at display time and never sent to an LLM.

_helper_cache: Dict[Tuple[str, int, Tuple[str, ...]], pd.DataFrame] = {}


def _table_info(client_id: str, table: str) -> Tuple[Dict[str, Any], Path]:
    info = _read_metadata(client_id).get("tables", {}).get(table.upper())
    if not info:
        raise KeyError(table)
    path = workspace_dir(client_id) / info["file"]
    if not path.exists():
        raise KeyError(table)
    return info, path


def get_table_columns(client_id: str, table: str) -> Dict[str, Any]:
    """Every column of an uploaded table with what the data dictionary says about it, for the picker."""
    info, path = _table_info(client_id, table)
    header = [str(c) for c in pd.read_csv(path, nrows=0).columns]
    described: Dict[str, Dict[str, str]] = {}
    dictionary = dictionary_path(client_id)
    if dictionary:
        try:
            for full, entry in load_data_dictionary_structured(str(dictionary)).items():
                tbl, _, field = full.partition(".")
                if tbl == table.upper():
                    described[field] = entry
        except Exception:  # a dictionary that can't be read must not block the picker
            described = {}
    return {
        "table": table.upper(),
        "columns": [{"name": c, "description": described.get(c.upper(), {}).get("description", ""),
                     "data_type": described.get(c.upper(), {}).get("data_type", "")} for c in header],
        "helper_columns": [c for c in info.get("helper_columns", []) if c in header],
    }


def set_helper_columns(client_id: str, table: str, columns: List[str]) -> Dict[str, Any]:
    """Save the helper columns of a table (must be columns of the uploaded file; order kept)."""
    with _lock:
        meta = _read_metadata(client_id)
        info = meta.get("tables", {}).get(table.upper())
        if not info:
            raise KeyError(table)
        header = [str(c) for c in pd.read_csv(workspace_dir(client_id) / info["file"], nrows=0).columns]
        unknown = [c for c in columns if c not in header]
        if unknown:
            raise WorkspaceError(f"Not columns of {table.upper()}: {', '.join(unknown[:5])}")
        chosen = [c for c in header if c in set(columns)]
        if chosen:
            info["helper_columns"] = chosen
        else:
            info.pop("helper_columns", None)
        _write_metadata(client_id, meta)
    return get_workspace(client_id)


def helper_values(client_id: str, table: str, row_indexes: List[int], columns: List[str]
                  ) -> Dict[int, Dict[str, str]]:
    """{row_index: {column: value}} straight from the uploaded CSV (text, so zero-padded keys survive).

    ``row_index`` is the dataframe position the run used: the loader reads the same file in the same
    order, so position N here is row N there."""
    if not columns or not row_indexes:
        return {}
    _, path = _table_info(client_id, table)
    key = (str(path), path.stat().st_mtime_ns, tuple(columns))
    df = _helper_cache.get(key)
    if df is None:
        df = pd.read_csv(path, dtype=str, usecols=columns, keep_default_na=False)
        if len(_helper_cache) >= 8:
            _helper_cache.clear()
        _helper_cache[key] = df
    out: Dict[int, Dict[str, str]] = {}
    for i in row_indexes:
        if 0 <= i < len(df):
            out[i] = {c: str(df.iat[i, df.columns.get_loc(c)]) for c in columns}
    return out


def dictionary_path(client_id: str) -> Optional[Path]:
    dictionary = get_workspace(client_id)["dictionary"]
    return workspace_dir(client_id) / dictionary["file"] if dictionary else None
