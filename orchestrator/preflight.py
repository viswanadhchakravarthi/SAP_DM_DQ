"""Pre-flight checks on LLM-generated check code, before it reaches the sandbox.

Free and deterministic (no LLM call): the code is parsed with `ast` and compared
with the real tables. It rejects the mistakes that reliably crash a check:

- the code doesn't parse, or never sets `result`;
- it reads a column that isn't in the table (`df['IBN']`), or a table that isn't
  loaded (`tables['LFA2']`), or a column missing from that other table;
- it uses the `.str` accessor on a numeric column, or compares a text column
  with a number (`df['ZTERM'] > 30`) or takes its mean;
- it calls a string method inside `.apply(lambda ...)` on a column that has
  empty cells (NaN floats), the crash seen in production logs.

A rejected check is not executed; it comes back as a failed result whose error
says exactly what to change. That message is what a repair step can hand to the
planner. The checks are conservative: when the code reassigns `df` or builds
columns on the fly, the column check is skipped rather than guessed.

Only `code` blocks execution. `detail_code` problems are returned as warnings,
because a failing `detail_code` only loses the row detail of an already
confirmed finding.
"""

import ast
from typing import Dict, List, Optional, Set

import pandas as pd
from pandas.api import types as ptypes

from orchestrator.sandbox import SANDBOX_NAMES

# Methods that only work on numbers; called on a text column they raise.
_NUMERIC_ONLY = {"mean", "std", "median", "quantile", "var"}
_ORDER_OPS = (ast.Gt, ast.Lt, ast.GtE, ast.LtE)
_NULL_GUARDS = {"isna", "isnull", "notna", "notnull", "isinstance"}
# Calls on a frame that add or rename columns: after them the column list is unknown.
_SHAPE_CHANGERS = {"assign", "insert", "rename", "reindex", "join", "merge", "pivot", "melt", "stack", "unstack"}


def other_tables_read(code: str, own_table: str) -> List[str]:
    """Names of the OTHER tables a check reads (`tables['LFBK']`, `tables.get('LFBK')`), sorted.

    A check that looks something up in another table tests a relationship, not whether a value is
    filled, whatever category the model gave it. Empty when the code doesn't parse."""
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return []
    found: Set[str] = set()
    for n in ast.walk(tree):
        name = None
        if (isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id == "tables"
                and isinstance(n.slice, ast.Constant)):
            name = n.slice.value
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
              and isinstance(n.func.value, ast.Name) and n.func.value.id == "tables"
              and n.args and isinstance(n.args[0], ast.Constant)):
            name = n.args[0].value
        if isinstance(name, str) and name.upper() != own_table.upper():
            found.add(name)
    return sorted(found)


# Frame methods whose string arguments are column names: method -> (positional index, keyword name).
_COLUMN_ARGS = {"set_index": (0, "keys"), "groupby": (0, "by"), "sort_values": (0, "by"),
                "drop_duplicates": (0, "subset")}


def _defined_names(nodes: List[ast.AST]) -> Set[str]:
    """Names the code itself binds: assignments, loop/comprehension/with targets, function and lambda
    parameters, imports, except-handler names. Anything else it reads must come from the sandbox."""
    names: Set[str] = set()
    for n in nodes:
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            names.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.arg):
            names.add(n.arg)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in n.names)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
    return names


def _const_names(node: ast.AST) -> Optional[List[str]]:
    """String constants of a subscript key: 'A' -> ['A'], ['A', 'B'] -> ['A', 'B']; None if not constant."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.List, ast.Tuple)) and all(
            isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts):
        return [e.value for e in node.elts]
    return None


def _column_of(node: ast.AST, frame: str = "df") -> Optional[str]:
    """'C' when node is exactly df['C']."""
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id == frame:
        names = _const_names(node.slice)
        if names and len(names) == 1 and isinstance(node.slice, ast.Constant):
            return names[0]
    return None


def _lookup(columns: Set[str], name: str) -> Optional[str]:
    """The real column for `name`, matching exactly first, then case-insensitively."""
    if name in columns:
        return name
    upper = {c.upper(): c for c in columns}
    return upper.get(name.upper())


def _analyse(code: str, df: pd.DataFrame, all_tables: Dict[str, pd.DataFrame]) -> List[str]:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"the code does not parse (SyntaxError: {exc.msg}, line {exc.lineno})"]

    problems: List[str] = []
    nodes = list(ast.walk(tree))

    if not any(isinstance(n, ast.Name) and n.id == "result" and isinstance(n.ctx, ast.Store) for n in nodes):
        problems.append("the code never assigns `result`")

    # A name the sandbox does not provide (globals(), locals(), vars(), eval, open, ...) is a NameError
    # at run time - seen in production logs: `NameError: name 'globals' is not defined`.
    defined = _defined_names(nodes)
    unavailable = sorted({n.id for n in nodes if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                          and n.id not in defined and n.id not in SANDBOX_NAMES})
    for name in unavailable:
        problems.append(f"`{name}` does not exist in the sandbox - use only pd, np, re, datetime, df, tables, "
                        f"names you define yourself, and basic builtins (len, str, int, float, set, list, dict, "
                        f"sorted, sum, min, max, zip, enumerate, range, any, all, isinstance)")

    columns = set(map(str, df.columns))
    # Columns the code creates itself, and whether `df` is rebuilt (then its columns are unknown).
    created = {name for n in nodes if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store)
               and isinstance(n.value, ast.Name) and n.value.id == "df" for name in (_const_names(n.slice) or [])}
    df_rebuilt = any(isinstance(n, ast.Name) and n.id == "df" and isinstance(n.ctx, ast.Store) for n in nodes) or any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in _SHAPE_CHANGERS
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "df" for n in nodes)

    for n in nodes:
        if not isinstance(n, ast.Subscript) or not isinstance(n.ctx, ast.Load):
            continue
        # tables['LFA2'] / tables['LFA1']['NAME1']
        if isinstance(n.value, ast.Name) and n.value.id == "tables":
            for table in _const_names(n.slice) or []:
                if table not in all_tables:
                    problems.append(f"table '{table}' is not loaded (available: {', '.join(sorted(all_tables))})")
        elif (isinstance(n.value, ast.Subscript) and isinstance(n.value.value, ast.Name)
              and n.value.value.id == "tables" and isinstance(n.value.slice, ast.Constant)
              and n.value.slice.value in all_tables):
            other = set(map(str, all_tables[n.value.slice.value].columns))
            for col in _const_names(n.slice) or []:
                if col not in other:
                    hint = _lookup(other, col)
                    problems.append(f"column '{col}' is not in table {n.value.slice.value}"
                                    + (f" (did you mean '{hint}'?)" if hint else ""))
        # df['COL']
        elif isinstance(n.value, ast.Name) and n.value.id == "df" and not df_rebuilt:
            for col in _const_names(n.slice) or []:
                if col not in columns and col not in created:
                    hint = _lookup(columns, col)
                    problems.append(f"column '{col}' is not in this table"
                                    + (f" (did you mean '{hint}'?)" if hint else f" (columns: {', '.join(sorted(columns))})"))

    # Columns named in .set_index('X') / .groupby('X') / .sort_values('X') / .drop_duplicates(subset=[..])
    # on `df` or on tables['T']: seen in production logs as `KeyError: "None of ['LIFNR'] are in the columns"`,
    # a key copied from an example into a table that does not have it.
    for n in nodes:
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in _COLUMN_ARGS):
            continue
        frame = n.func.value
        if isinstance(frame, ast.Name) and frame.id == "df" and not df_rebuilt:
            have, where = columns | created, "this table"
        elif (isinstance(frame, ast.Subscript) and isinstance(frame.value, ast.Name) and frame.value.id == "tables"
              and isinstance(frame.slice, ast.Constant) and frame.slice.value in all_tables):
            have, where = set(map(str, all_tables[frame.slice.value].columns)), f"table {frame.slice.value}"
        else:
            continue
        position, keyword = _COLUMN_ARGS[n.func.attr]
        wanted = [kw.value for kw in n.keywords if kw.arg == keyword]
        if position is not None and len(n.args) > position:
            wanted.append(n.args[position])
        for arg in wanted:
            for col in _const_names(arg) or []:
                if col not in have:
                    hint = _lookup(have, col)
                    problems.append(f"column '{col}' (in .{n.func.attr}) is not in {where}"
                                    + (f" (did you mean '{hint}'?)" if hint else f" (columns: {', '.join(sorted(have))})"))

    if df_rebuilt:
        return _dedupe(problems)

    for n in nodes:
        # df['C'].str.<...>  on a non-text column
        if isinstance(n, ast.Attribute) and n.attr == "str":
            col = _column_of(n.value)
            if col in columns and not (ptypes.is_object_dtype(df[col]) or ptypes.is_string_dtype(df[col])):
                problems.append(f"column '{col}' is {df[col].dtype}, not text, so the .str accessor fails - "
                                f"use df['{col}'].astype(str).str... (after handling empty cells)")
        # df['C'].mean() and friends on a text column
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in _NUMERIC_ONLY):
            col = _column_of(n.func.value)
            if col in columns and ptypes.is_object_dtype(df[col]):
                problems.append(f"column '{col}' is text, so .{n.func.attr}() fails - "
                                f"convert with pd.to_numeric(df['{col}'], errors='coerce') first, "
                                f"and only if the column really holds numbers (codes such as ZTERM are keys)")
        # df['C'] > 30  on a text column
        if isinstance(n, ast.Compare) and any(isinstance(op, _ORDER_OPS) for op in n.ops):
            col = _column_of(n.left)
            if (col in columns and ptypes.is_object_dtype(df[col])
                    and any(isinstance(c, ast.Constant) and isinstance(c.value, (int, float))
                            and not isinstance(c.value, bool) for c in n.comparators)):
                problems.append(f"column '{col}' is text, so comparing it with a number fails - "
                                f"convert with pd.to_numeric(df['{col}'], errors='coerce') first")
        # df['C'].apply(lambda v: v.startswith(..)) when C has empty cells
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("apply", "map")
                and n.args and isinstance(n.args[0], ast.Lambda)):
            col = _column_of(n.func.value)
            lam = n.args[0]
            if col in columns and df[col].isna().any() and lam.args.args and _uses_value_unsafely(lam):
                problems.append(f"column '{col}' has {int(df[col].isna().sum()):,} empty cell(s) (NaN floats) and the "
                                f"lambda calls a string method or `in` on every value - use "
                                f"df['{col}'].str.<method>(..., na=False), or handle empty cells first")
    return _dedupe(problems)


def _uses_value_unsafely(lam: ast.Lambda) -> bool:
    """True when the lambda calls a method on / tests membership in / measures its argument without a NaN guard."""
    arg = lam.args.args[0].arg
    body = list(ast.walk(lam.body))
    if any(isinstance(n, ast.Call) and getattr(n.func, "attr", getattr(n.func, "id", None)) in _NULL_GUARDS
           for n in body):
        return False
    for n in body:
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == arg:
            return True
        if isinstance(n, ast.Compare) and any(isinstance(op, (ast.In, ast.NotIn)) for op in n.ops) and any(
                isinstance(c, ast.Name) and c.id == arg for c in n.comparators):
            return True
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "len" and any(
                isinstance(a, ast.Name) and a.id == arg for a in n.args):
            return True
    return False


def _dedupe(items: List[str]) -> List[str]:
    seen, out = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


_MIN_LITERAL_LIST = 3   # a pair like ['M', 'F'] is a format fact; three or more is a claimed domain


def hardcoded_value_lists(code: str) -> List[str]:
    """Problems for a check that tests a column against a literal list of allowed values.

    The planner sees no client configuration, so a list such as ['1000', '2000', '3000'] for vendor
    account groups is a guess presented as a rule (seen on prod data: group 5700 flagged because the
    model assumed the "standard" groups). Valid sets come from the data dictionary, a loaded target
    domain or another table, never from the model. Only for planner checks: a promoted skill was
    approved by a human, so the caller does not apply this to those."""
    try:
        tree = ast.parse(code or "")
    except SyntaxError:
        return []

    def literal_size(node: ast.AST) -> int:
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)) and all(
                isinstance(e, ast.Constant) and isinstance(e.value, (str, int)) for e in node.elts):
            return len({e.value for e in node.elts})
        return 0

    named = {}   # allowed = ['1000', ...]
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            named[n.targets[0].id] = literal_size(n.value)

    def size_of(node: ast.AST) -> int:
        return named.get(node.id, 0) if isinstance(node, ast.Name) else literal_size(node)

    for n in ast.walk(tree):
        candidates = []
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "isin" and n.args:
            candidates = [n.args[0]]
        elif isinstance(n, ast.Compare) and any(isinstance(op, (ast.In, ast.NotIn)) for op in n.ops):
            candidates = list(n.comparators)
        if any(size_of(c) >= _MIN_LITERAL_LIST for c in candidates):
            return ["the code tests a column against a hardcoded list of allowed values, which you cannot know "
                    "(valid codes are configured per client) - do not assert a valid set. Flag by evidence in the "
                    "data instead (a value used by very few rows, a format or length that differs from the "
                    "column's pattern) or against values in another loaded table, or drop this check"]
    return []


def preflight(code: str, df: pd.DataFrame, all_tables: Dict[str, pd.DataFrame]) -> List[str]:
    """Problems that would make `code` fail; empty when it looks runnable."""
    return _analyse(code, df, all_tables)
