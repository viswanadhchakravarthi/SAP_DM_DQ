"""Bounded repair of planner checks that could not run.

After `execute_all`, checks that failed in the sandbox or were rejected by
`preflight.py` can go back to the planner ONCE per round, together, in a single
call (`graph.py`: execute_all -> repair_batch -> execute_all). The loop is bounded
by `Config.MAX_REPAIR_ROUNDS` (0 = off), a counter in code - never a decision of
the LLM - so the cost is at most one extra call per round and table, and only
when something failed.

What reaches the LLM (metadata only, like every other prompt):
- the failed check's own code and hypothesis (LLM-written, no client data),
- the table's column names and dtypes, and other tables' column names,
- the error as ONE line, with quoted values masked. Pre-flight messages are
  generated here and contain only column names and dtypes, so they pass as they are.
  Sandbox errors can echo a value (`could not convert string to float: 'ABC-123'`),
  so anything quoted becomes '<value>' unless it is a column/table name, a Python or
  pandas attribute/type name ('startswith', 'float') or bare punctuation ('>').
  Best effort, like privacy_guard, but an allow-list: a value only passes if it
  happens to equal one of those names.

A repair only replaces `code` and `detail_code`; column, category, scope and
every other field stay as the planner set them, so a repair can't relabel a
finding.
"""

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from .schemas import ProposedCheck

MAX_ERROR_CHARS = 300
_QUOTED = re.compile(r"(['\"])(.*?)\1")
# Names an error message legitimately quotes: methods/attributes of the types checks use, and type names.
_SAFE_TOKENS = (set(dir(str)) | set(dir(int)) | set(dir(float)) | set(dir(list)) | set(dir(dict))
                | set(dir(pd.Series)) | set(dir(pd.DataFrame)) | set(dir(pd.Series([""]).str))
                | {"str", "int", "float", "bool", "list", "dict", "tuple", "set", "bytes", "object", "NoneType",
                   "Series", "DataFrame", "Timestamp", "datetime", "nan", "NaT"})


def sanitize_error(error: Optional[str], known_names: Sequence[str]) -> str:
    """One line of a sandbox error, with quoted values masked (see module docstring)."""
    lines = [ln.strip() for ln in str(error or "").splitlines() if ln.strip()]
    line = lines[0] if lines else "unknown error"
    known = {str(n).upper() for n in known_names}

    def mask(match: "re.Match[str]") -> str:
        inner = match.group(2)
        if inner.upper() in known or inner in _SAFE_TOKENS or (len(inner) <= 3 and not any(c.isalnum() for c in inner)):
            return match.group(0)
        return f"{match.group(1)}<value>{match.group(1)}"

    return _QUOTED.sub(mask, line)[:MAX_ERROR_CHARS]


def failed_indices(results: List[Dict[str, Any]]) -> List[int]:
    """check_index of every check that did not produce a result."""
    return [r["check_index"] for r in results if not r["success"]]


def build_repair_prompt(table_name: str, df: pd.DataFrame, all_tables: Dict[str, pd.DataFrame],
                        checks: List[ProposedCheck], results: List[Dict[str, Any]],
                        indices: List[int]) -> str:
    columns = {str(c): str(df[c].dtype) for c in df.columns}
    known = list(columns) + list(all_tables)
    for other in all_tables.values():
        known += [str(c) for c in other.columns]
    others = {name: [str(c) for c in t.columns] for name, t in all_tables.items() if name != table_name}

    blocks = []
    for n, i in enumerate(indices, 1):
        check, result = checks[i], results[i]
        error = result.get("error") or ""
        error_line = (error.replace("Pre-flight: ", "", 1)[:MAX_ERROR_CHARS * 2] if result.get("preflight_rejected")
                      else sanitize_error(error, known))
        blocks.append(
            f"### Failed check {n}\n"
            f"column: {check.column}\ncategory: {check.category}\nhypothesis: {check.hypothesis}\n"
            f"code:\n{check.code}\n"
            + (f"detail_code:\n{check.detail_code}\n" if check.detail_code else "")
            + f"error: {error_line}\n")

    return (
        f"Table: {table_name}\n"
        f"Columns and pandas dtypes (all text columns are dtype object; empty cells are NaN): {columns}\n"
        f"Other tables available as tables['<name>'] and their columns: {others or 'none'}\n\n"
        f"{len(indices)} check(s) failed to run. Return exactly {len(indices)} corrected check(s), in the SAME "
        f"order, one per failed check. Keep each check's column, category and hypothesis; change only the "
        f"`code` (and `detail_code` if it has the same problem) so the error goes away.\n\n"
        + "\n".join(blocks))


def apply_repairs(checks: List[ProposedCheck], indices: List[int], repaired: List[ProposedCheck]
                  ) -> Tuple[List[ProposedCheck], List[int]]:
    """New check list with repaired code in place, and the indices that really changed.

    The i-th repaired check answers the i-th failed one. If the model returned a different
    number, only a unique match on `column` is trusted; the rest stay failed. Code that is
    unchanged is not re-run (it would fail the same way)."""
    if len(repaired) == len(indices):
        pairs = list(zip(indices, repaired))
    else:
        by_column: Dict[str, List[ProposedCheck]] = {}
        for r in repaired:
            by_column.setdefault(r.column.upper(), []).append(r)
        pairs = [(i, by_column[checks[i].column.upper()][0]) for i in indices
                 if len(by_column.get(checks[i].column.upper(), [])) == 1]

    updated, changed = list(checks), []
    for i, new in pairs:
        old = checks[i]
        if (new.code or "").strip() == (old.code or "").strip() and (new.detail_code or "") == (old.detail_code or ""):
            continue
        updated[i] = old.model_copy(update={"code": new.code, "detail_code": new.detail_code})
        changed.append(i)
    return updated, changed
