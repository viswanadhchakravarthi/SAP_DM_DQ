"""Score a run's findings against a synthetic client's answer key.

    python -m explorer_agent.evaluate --client danawsiv [--run-id <id>] [--source rules|duplicates|llm|all]
                                      [--answer-key <path>]

A minimal harness (see "POC vs production" -> Evaluation in CLAUDE.md), not a
benchmark: matching is by table + object key + field, so it measures whether
the right record was flagged for the right field, not whether the wording fits.

* Recall, per answer-key issue type: share of its rows for which some finding
  item flagged the same table and object key (first key part, e.g. LIFNR), on a
  finding whose column includes the key's field (any field when the key says
  ALL or leaves it blank).
* Precision, per finding: share of its rows that match some answer-key row on
  the same terms. A low number is either noise or a defect the key does not
  list - read the rows before concluding which.

* Survivorship, when the key names survivors ("Golden Record Survivor" /
  "Obsolete Duplicate"): of the duplicate groups containing a key survivor, the
  share whose recommended survivor is that record, and how many key obsolete
  duplicates were recommended as DUPLICATE.

The answer key is read here only; it never goes near a run or an LLM.
"""

import argparse
import re
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd

from .config import Config
from . import episodic_store as store

_RULE_MARK = "# Built-in SAP rule "


def find_answer_key(client_id: str) -> Path:
    base = Path(Config.PROJECT_ROOT) / "client_data"
    for sep in ("_", "-"):
        for name in ("ANSWER_KEY", "Answer_Key"):
            path = base / f"{client_id}{sep}{name}.csv"
            if path.exists():
                return path
    raise FileNotFoundError(f"No answer key for {client_id!r} next to its folder in {base}")


def load_answer_key(path: Path) -> pd.DataFrame:
    key = pd.read_csv(path, dtype=str, keep_default_na=False)
    if "Key" not in key.columns:  # vendor-only keys name the key column after the field
        key = key.rename(columns={"LIFNR": "Key"})
    key["Table"] = key["Table"].str.upper().str.strip()
    key["Key"] = key["Key"].str.strip()
    key["Field"] = key["Field"].str.upper().str.strip()
    return key


def _fields(column: str) -> Set[str]:
    return {p for p in re.split(r"[^A-Z0-9_]+", (column or "").upper()) if p}


def _source(check_code: str, category: str) -> str:
    if (check_code or "").startswith(_RULE_MARK):
        return "rules"
    return "duplicates" if category == "DUPLICATE" else "llm"


def load_run(run_id: Optional[str], client_id: str) -> Tuple[str, pd.DataFrame]:
    with store.get_connection() as conn:
        if not run_id:
            row = conn.execute("SELECT run_id FROM runs WHERE client_id = ? ORDER BY started_at DESC LIMIT 1",
                               (client_id,)).fetchone()
            if not row:
                raise SystemExit(f"No runs for client {client_id!r}")
            run_id = row["run_id"]
        rows = conn.execute(
            """SELECT f.id AS finding_id, f.table_name, f.column_name, f.result_summary, f.category,
                      f.check_code, i.key_value
               FROM findings f JOIN finding_items i ON i.finding_id = f.id
               WHERE f.run_id = ?""", (run_id,)).fetchall()
    items = pd.DataFrame([dict(r) for r in rows])
    if items.empty:
        raise SystemExit(f"Run {run_id} has no finding rows")
    items["table_name"] = items["table_name"].str.upper()
    items["object_key"] = items["key_value"].astype(str).str.split(" / ").str[0].str.strip()
    items["source"] = [_source(c, cat) for c, cat in zip(items["check_code"], items["category"])]
    return run_id, items


def score(items: pd.DataFrame, key: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    # (table, object key) -> fields flagged there, from the findings
    flagged: Dict[Tuple[str, str], Set[str]] = {}
    for (table, obj), group in items.groupby(["table_name", "object_key"]):
        flagged[(table, obj)] = set().union(*(_fields(c) for c in group["column_name"]))
    expected: Dict[Tuple[str, str], Set[str]] = {}
    for (table, obj), group in key.groupby(["Table", "Key"]):
        expected[(table, obj)] = set(group["Field"])

    def hit(fields_a: Set[str], fields_b: Set[str]) -> bool:
        return bool(fields_a & fields_b) or "ALL" in fields_b or "" in fields_b or "ALL" in fields_a

    key = key.assign(found=[hit(flagged.get((t, k), set()), {f}) and (t, k) in flagged
                            for t, k, f in zip(key["Table"], key["Key"], key["Field"])])
    recall = (key.groupby(["Table", "Issue_Type"])
              .agg(rows=("found", "size"), found=("found", "sum"))
              .assign(recall=lambda d: (d["found"] / d["rows"]).round(2))
              .reset_index())

    items = items.assign(match=[(t, k) in expected and hit(_fields(c), expected[(t, k)])
                                for t, k, c in zip(items["table_name"], items["object_key"], items["column_name"])])
    precision = (items.groupby(["source", "table_name", "column_name", "result_summary"])
                 .agg(rows=("match", "size"), in_key=("match", "sum"))
                 .assign(precision=lambda d: (d["in_key"] / d["rows"]).round(2))
                 .reset_index()
                 .sort_values(["source", "table_name", "precision"]))
    return recall, precision


def survivorship_score(run_id: str, key: pd.DataFrame) -> Optional[str]:
    survivors = key[key["Issue_Type"].str.contains("Survivor", case=False)]
    obsolete = key[key["Issue_Type"].str.contains("Obsolete", case=False)]
    if survivors.empty:
        return None
    with store.get_connection() as conn:
        rows = pd.DataFrame([dict(r) for r in conn.execute(
            """SELECT f.table_name, i.duplicate_group_id, i.key_value, i.recommended_verdict
               FROM findings f JOIN finding_items i ON i.finding_id = f.id
               WHERE f.run_id = ? AND f.category = 'DUPLICATE'""", (run_id,)).fetchall()])
    if rows.empty:
        return "Survivorship: no duplicate rows in this run."
    rows["table_name"] = rows["table_name"].str.upper()
    rows["obj"] = rows["key_value"].astype(str).str.split(" / ").str[0].str.strip()
    wanted = set(zip(survivors["Table"], survivors["Key"]))
    right = total = no_pick = 0
    wrong = []
    for (table, _gid), g in rows.groupby(["table_name", "duplicate_group_id"]):
        keyed = [o for o in g["obj"] if (table, o) in wanted]
        if not keyed:
            continue
        total += 1
        pick = g.loc[g["recommended_verdict"] == "UNIQUE", "obj"].tolist()
        if not pick:
            no_pick += 1
        elif pick[0] in keyed:
            right += 1
        else:
            wrong.append(f"{table} {keyed[0]} (picked {pick[0]})")
    obs = set(zip(obsolete["Table"], obsolete["Key"]))
    as_dup = rows[[(t, o) in obs for t, o in zip(rows["table_name"], rows["obj"])]]
    lines = [f"Survivorship: key survivors are in {total} of {len(wanted)} detected group(s); recommended survivor "
             f"correct in {right}/{total}"
             + (f", {no_pick} without a recommendation (SIMILAR match)" if no_pick else "") + ".",
             f"Key 'obsolete duplicates' found in groups: {len(as_dup)}/{len(obs)}; recommended DUPLICATE: "
             f"{int((as_dup['recommended_verdict'] == 'DUPLICATE').sum())}."]
    if wrong:
        lines.append("Wrong picks: " + "; ".join(wrong[:10]))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Score a run against the client's answer key")
    parser.add_argument("--client", required=True, help="client_id, e.g. danawsiv")
    parser.add_argument("--run-id", default=None, help="default: the client's latest run")
    parser.add_argument("--source", choices=["rules", "duplicates", "llm", "all"], default="all")
    parser.add_argument("--answer-key", default=None, help="path to the key CSV (default: found next to "
                                                           "client_data/<client>)")
    args = parser.parse_args()

    key = load_answer_key(Path(args.answer_key) if args.answer_key else find_answer_key(args.client))
    run_id, items = load_run(args.run_id, args.client)
    if args.source != "all":
        items = items[items["source"] == args.source]
        if items.empty:
            raise SystemExit(f"No {args.source} findings in run {run_id}")
    recall, precision = score(items, key)

    pd.set_option("display.width", 200)
    pd.set_option("display.max_colwidth", 70)
    pd.set_option("display.max_rows", 500)
    print(f"Run {run_id} | client {args.client} | source {args.source} | "
          f"{len(items)} flagged row(s), {len(key)} answer-key row(s)\n")
    print("RECALL by answer-key issue type")
    print(recall.to_string(index=False))
    print(f"\nOverall recall: {recall['found'].sum()}/{recall['rows'].sum()} "
          f"= {recall['found'].sum() / max(recall['rows'].sum(), 1):.2f}\n")
    print("PRECISION by finding (in_key = rows that match an answer-key row on table + key + field)")
    print(precision.to_string(index=False))
    surv = survivorship_score(run_id, key)
    print(f"\nOverall precision: {precision['in_key'].sum()}/{precision['rows'].sum()} "
          f"= {precision['in_key'].sum() / max(precision['rows'].sum(), 1):.2f}")
    if surv:
        print("\n" + surv)


if __name__ == "__main__":
    main()
