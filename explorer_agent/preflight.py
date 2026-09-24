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

# Methods that only work on numbers; called on a text column they raise.
_NUMERIC_ONLY = {"mean", "std", "median", "quantile", "var"}
_ORDER_OPS = (ast.Gt, ast.Lt, ast.GtE, ast.LtE)
_NULL_GUARDS = {"isna", "isnull", "notna", "notnull", "isinstance"}
# Calls on a frame that add or rename columns: after them the column list is unknown.
_SHAPE_CHANGERS = {"assign", "insert", "rename", "reindex", "join", "merge", "pivot", "melt", "stack", "unstack"}


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


def preflight(code: str, df: pd.DataFrame, all_tables: Dict[str, pd.DataFrame]) -> List[str]:
    """Problems that would make `code` fail; empty when it looks runnable."""
    return _analyse(code, df, all_tables)
