"""
Table-scoped Explorer graph - BATCH design.

Change: instead of iterative tool-calling (many round-trips), we now:
  1. Profile the WHOLE table upfront (ydata-profiling, sanitized)
  2. ONE Planner call proposes a batch of checks (structured output)
  3. Execute ALL checks locally (sandboxed, zero LLM involvement)
  4. ONE Reflector call judges ALL results at once (structured output)

This is now a LINEAR graph (no cycles needed) - still built with
LangGraph's StateGraph for consistent state handling and future
extensibility (e.g. a retry branch for failed checks), per your
requirement to stay within LangChain/LangGraph. We no longer use
ToolNode/tool-calling here since batch structured output doesn't need it.

LLM calls per table: exactly 2 (1 planner + 1 reflector) - constant,
regardless of column count.
"""

from typing import TypedDict, List, Dict, Any
import pandas as pd
from langchain_core.messages import SystemMessage, HumanMessage
from langgraph.graph import StateGraph, END

from .schemas import CheckPlan, ReflectionBatch
from .check_executor import execute_checks, extract_detail_rows
from .config import Config
from .metrics import metrics
from .logging_config import get_logger

logger = get_logger("graph")

PLANNER_SYSTEM_PROMPT = """You are a data quality exploration agent for SAP data migration \
projects. You are given a statistical profile of an ENTIRE table (aggregated stats and \
masked top-values only - never raw data). Propose a batch of pandas checks in ONE response, \
covering columns that look most likely to have issues. Skip columns that look clean.

For EACH check, provide TWO code fields:

1. `code` (REQUIRED): must set `result` to an AGGREGATE value (count/pct/bool/small dict).
   This is sent to an LLM for review - never include raw row values here.

2. `detail_code` (STRONGLY RECOMMENDED whenever the check identifies specific offending
   rows - e.g. duplicates, invalid formats, nulls in a key field): pandas code that sets
   `result` to a list of dicts, one per offending row, each with keys:
   - row_index (int): the dataframe row index
   - key_field (str): name of a natural key column for this table (e.g. 'LIFNR')
   - key_value (the value of that key field for this row)
   - issue_detail (str): a specific, human-readable description of what's wrong with
     THIS row (e.g. 'Duplicate tax number: 123-45-6789')

Cap that list at around 50 rows if many rows match (use .head(50) or similar).
This detail_code output is for LOCAL HUMAN REVIEW ONLY and is never sent back to you.

Both code fields must be complete, valid Python. Inside code use ONLY single quotes (') \
for string literals - never double quotes, and no f-strings; build strings with + and str().

Example of a check WITH detail_code (duplicate tax numbers):
  code:
    result = int(df['STCD1'].duplicated(keep=False).sum())
  detail_code:
    dupes = df[df.duplicated(subset=['STCD1'], keep=False) & df['STCD1'].notna()]
    result = [{'row_index': int(idx), 'key_field': 'LIFNR', 'key_value': str(row['LIFNR']), 'issue_detail': 'Duplicate tax number: ' + str(row['STCD1'])} for idx, row in dupes.head(50).iterrows()]
"""

REFLECTOR_SYSTEM_PROMPT = """You are reviewing outcomes of MULTIPLE data quality checks that \
just ran, in one batch. For EACH result (identified by check_index), decide if it's a genuine \
issue, its severity/confidence, a one-line summary, and whether it would generalize to other \
similar datasets/clients (reusable)."""


class TableExplorerState(TypedDict):
    table_name: str
    seed_prompt: str
    df: pd.DataFrame
    all_tables: Dict[str, pd.DataFrame]
    proposed_checks: List[Any]
    check_results: List[Dict[str, Any]]
    findings: List[Dict[str, Any]]


def build_explorer_graph(planner_structured, reflector_structured):
    """
    planner_structured: LLM wrapped with .with_structured_output(CheckPlan)
    reflector_structured: LLM wrapped with .with_structured_output(ReflectionBatch)
    """

    def node_plan_batch(state: TableExplorerState) -> Dict[str, Any]:
        plan: CheckPlan = planner_structured.invoke([
            SystemMessage(content=PLANNER_SYSTEM_PROMPT),
            HumanMessage(content=state["seed_prompt"]),
        ])
        metrics.planner_llm_calls += 1

        checks = plan.checks[:Config.MAX_TOTAL_CHECKS_PER_TABLE]

        logger.info("Planner proposed %d check(s) for table %s", len(checks), state["table_name"])
        return {"proposed_checks": checks}

    def node_execute_all(state: TableExplorerState) -> Dict[str, Any]:
        results = execute_checks(state["proposed_checks"], state["df"], state["all_tables"])
        logger.info("Executed %d check(s) for table %s (%d succeeded)",
                    len(results), state["table_name"], sum(1 for r in results if r["success"]))
        return {"check_results": results}

    def node_reflect_batch(state: TableExplorerState) -> Dict[str, Any]:
        successful = [r for r in state["check_results"] if r["success"]]
        if not successful:
            logger.warning("No successful checks to reflect on for table %s", state["table_name"])
            return {"findings": []}

        summary_lines = [
            f"[{r['check_index']}] column={r['column']} hypothesis=\"{r['hypothesis']}\" result={r['result']}"
            for r in successful
        ]
        prompt = "Evaluate EACH check result below. Return a judgment per check_index.\n\n" + "\n".join(summary_lines)

        batch: ReflectionBatch = reflector_structured.invoke([
            SystemMessage(content=REFLECTOR_SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ])
        metrics.reflector_llm_calls += 1

        results_by_index = {r["check_index"]: r for r in successful}
        findings = []
        for j in batch.judgments:
            r = results_by_index.get(j.check_index)
            if r is None or not j.is_issue:
                continue
            findings.append({
                "table": state["table_name"], "column": r["column"],
                "hypothesis": r["hypothesis"], "check_code": r["check_code"],
                "summary": j.summary, "severity": j.severity,
                "confidence": j.confidence, "reusable": j.reusable,
                "raw_tool_result": str(r["result"]),
                # FIX (bug found in review): this key was previously never set, so
                # detail_rows extraction below always looked up None -> [] for every
                # finding, silently dropping all row-level detail_code output.
                "_check_index": r["check_index"],
            })

        # for each confirmed finding, extract row-level detail locally
        checks_by_index = {i: c for i, c in enumerate(state["proposed_checks"])}
        for finding in findings:
            check_idx = finding.get("_check_index")
            check = checks_by_index.get(check_idx)
            if check:
                finding["detail_rows"] = extract_detail_rows(check, state["df"], state["all_tables"])
            else:
                finding["detail_rows"] = []

        logger.info("Reflection complete for table %s - %d finding(s) confirmed",
                    state["table_name"], len(findings))
        return {"findings": findings}

    def node_finalize(state: TableExplorerState) -> Dict[str, Any]:
        return {}

    def node_human_review(state: TableExplorerState) -> Dict[str, Any]:
        logger.info("[human_review STUB] %d finding(s) pending review for table %s (not interactive yet)",
                    len(state["findings"]), state["table_name"])
        return {}

    graph = StateGraph(TableExplorerState)
    graph.add_node("plan_batch", node_plan_batch)
    graph.add_node("execute_all", node_execute_all)
    graph.add_node("reflect_batch", node_reflect_batch)
    graph.add_node("finalize", node_finalize)
    graph.add_node("human_review", node_human_review)

    graph.set_entry_point("plan_batch")
    graph.add_edge("plan_batch", "execute_all")
    graph.add_edge("execute_all", "reflect_batch")
    graph.add_edge("reflect_batch", "finalize")
    graph.add_edge("finalize", "human_review")
    graph.add_edge("human_review", END)

    return graph.compile()
