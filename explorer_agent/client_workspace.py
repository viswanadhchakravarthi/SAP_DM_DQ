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
from typing import Any, Dict, Optional

import pandas as pd

from . import client_knowledge
from .config import Config
from .data_loader import DICTIONARY_REQUIRED_COLUMNS, table_name_from_filename

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
            meta.setdefault("tables", {})[table] = {
                "file": target,
                "original_name": Path(original_filename).name,
                "rows": int(df.shape[0]),
                "columns": int(df.shape[1]),
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            }
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


def dictionary_path(client_id: str) -> Optional[Path]:
    dictionary = get_workspace(client_id)["dictionary"]
    return workspace_dir(client_id) / dictionary["file"] if dictionary else None
