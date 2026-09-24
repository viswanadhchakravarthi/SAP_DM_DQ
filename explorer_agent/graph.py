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
from .check_executor import execute_checks, extract_detail_rows, sandbox_session
from .config import Config
from .metrics import metrics
from .logging_config import get_logger

logger = get_logger("graph")

PLANNER_SYSTEM_PROMPT = """You are an expert data quality exploration agent for SAP data migration \
and governance projects. You are given a statistical profile of an ENTIRE table (aggregated stats and \
masked top-values only - never raw data). Propose a batch of pandas checks covering columns likely to have issues.

BUILT-IN SAP RULES: a deterministic rule engine has already run the standard SAP checks listed in the user \
message (deletion flags/blocks, dormancy, mandatory fields, country keys, postal-code and tax-number formats by \
country, orphan records, flags not carried to org-level rows). Never propose a check that repeats one of them. \
Use your checks for what fixed rules cannot know: client-specific value sets and conventions, distribution \
anomalies, text hygiene, placeholder values, and cross-field or cross-table logic not already covered.

Structure checks across the FOUR MAJOR DATA PROFILING PILLARS:
1. ACTIVENESS:
   - Check if records are actively used or dormant/obsolete.
   - Deletion flags, blocks and dormancy (old ERDAT with no org-level extension) are built-in rules - only propose
     activeness checks they do not cover.
   - Tag category="ACTIVENESS".

2. DUPLICATE:
   - Do NOT propose any DUPLICATE checks. Duplicate records are detected separately by a built-in,
     deterministic matching engine; any DUPLICATE check you propose is discarded.

3. COMPLETENESS:
   - Standard SAP mandatory fields and tax numbers are built-in rules; look for blanks in other fields that matter here.
   - Classify each completeness issue as either:
     * fix_type="AUTO_FIXABLE" with auto_fix_value, ONLY for a harmless single default (e.g. a missing language key)
     * fix_type="MANUAL_FIX" for anything financial or identifying (reconciliation account, payment terms/methods,
       bank data, tax numbers) - these must come from the business, never from a guessed default
   - Tag category="COMPLETENESS".

4. CORRECTNESS & STATISTICAL ANOMALIES:
   - Format validation, invalid country codes (ISO length != 2), invalid special characters in names (e.g. '#', '$').
   - Built-in rules already check amount/quantity outliers and negatives, rare codes, currency keys, name/text
     hygiene and e-mail/phone formats. Codes such as payment terms (ZTERM) are KEYS, not numbers of days - never
     compute numeric statistics on them. Look for anomalies the built-in rules cannot see (cross-field logic,
     client-specific conventions).
   - Tag category="CORRECTNESS", and set is_anomaly=True if it is a statistical distribution outlier.
   - Also set `sub_type` for every CORRECTNESS check:
     * sub_type="VALUE_ERROR" for a single-field format/value/outlier problem (a "corrected value" makes sense here).
     * sub_type="RELATIONSHIP_INTEGRITY" for a cross-table/referential check (e.g. "vendor exists in LFB1 but has no
       row in LFA1") - there is no single corrected value for these, only a business decision, so never suggest one.

RULE SCOPE CLASSIFICATION (set rule_scope for each check):
- UNIVERSAL: Standard SAP integrity mandatory across all implementations (e.g. Reconciliation account present, Tax uniqueness, primary key).
- INDUSTRY_SPECIFIC: Rules specific to an industry (e.g. Banking & Financial Services, Healthcare & Biotech, Manufacturing, Consumer & Retail, Energy & Utilities). Specify the industry name in `industry`.
- CLIENT_SPECIFIC: Custom client naming conventions, allowed payment terms, internal code patterns.

For EACH check, provide TWO code fields:
1. `code` (REQUIRED): must set `result` to an AGGREGATE value (count/pct/bool/small dict).
   This is sent to an LLM for review - never include raw row values here.

2. `detail_code` (STRONGLY RECOMMENDED): pandas code that sets `result` to a list of dicts, one per offending row.
   Keys:
   - row_index (int): dataframe index
   - key_field (str): name of natural key column (e.g. 'LIFNR')
   - key_value: value of key field for this row
   - issue_detail (str): specific description of the issue for THIS row

CROSS-TABLE ENRICHMENT (make issue_detail actionable, not just an identifier): your `detail_code` has access to
`tables['<OTHER_TABLE>']` for EVERY other registered table (a dict of full DataFrames, keyed by table name), not
just `df` (the current table). PERFORMANCE - detail_code runs under a strict wall-clock timeout: first narrow to the
offending rows and slice to at most 50 of them (e.g. `subset = df[mask].head(50)`), and only THEN do any per-row
string building or cross-table lookups on that small subset - never enrich all matching rows before capping, and
build lookup dicts (`.to_dict()`) once outside any loop, never inside one. When a row references a business key
that also exists in another table (e.g. LIFNR in LFB1 also identifies a row in LFA1), look up human-readable
context BEFORE building issue_detail:
    name_lookup = tables['LFA1'].set_index('LIFNR')['NAME1'].to_dict()
    vendor_name = name_lookup.get(row['LIFNR'], 'Unknown')
Build each lookup dict ONCE outside any loop with `.to_dict()`, and ALWAYS read it with `.get(key, 'Unknown')`
(never direct indexing) since the key may not exist in the other table. Compose issue_detail as a business-readable
sentence, not a bare code: e.g. 'Vendor: 473 - ABC Supplies Pvt Ltd | Company Code: 1000 | Payment Terms: XXXX '
'(expected/common: YYYY) | Suggested Action: Confirm with the AP team whether this term is intentional.' Include
whatever of vendor id/name, company code, current value, expected/reference value, and a suggested action is
actually available from the current table plus one cross-table lookup - do not invent fields that aren't there.

Cap detail_rows at around 50 rows (use .head(50)).
Inside code use ONLY single quotes (') for string literals - never double quotes, and no f-strings; build strings with + and str().
"""

REFLECTOR_SYSTEM_PROMPT = """You are reviewing outcomes of MULTIPLE data quality checks that \
just ran, in one batch across Activeness, Completeness, and Correctness pillars. \
For EACH result (identified by check_index), decide if it's a genuine issue, its severity/confidence, \
a one-line summary, rule_scope, fix_type, auto_fix_value, is_anomaly, and whether it would \
generalize to other similar datasets/clients (reusable). For CORRECTNESS results, also confirm or set \
sub_type: VALUE_ERROR for a single wrong field value, RELATIONSHIP_INTEGRITY for a cross-table/referential \
mismatch (no single corrected value applies to those).
Each check's category is fixed by the check itself - echo it, never reclassify it. Base each summary ONLY on \
that check_index's own column, hypothesis and result value; never mention numbers or fields from another check."""


class TableExplorerState(TypedDict):
    table_name: str
    seed_prompt: str
    df: pd.DataFrame
    all_tables: Dict[str, pd.DataFrame]
    proposed_checks: List[Any]
    check_results: List[Dict[str, Any]]
    findings: List[Dict[str, Any]]
    rule_coverage: Any  # sap_rules.RuleCoverage - what the deterministic SAP rules already checked


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

        # Duplicates come from duplicate_detector.py (deterministic, no LLM) -
        # drop any DUPLICATE check the planner proposed anyway.
        checks = [c for c in plan.checks if c.category != "DUPLICATE"]
        if len(checks) < len(plan.checks):
            logger.info("Discarded %d planner DUPLICATE check(s) for table %s (handled by duplicate_detector)",
                        len(plan.checks) - len(checks), state["table_name"])
        # Same for checks that repeat a built-in SAP rule on the same column and
        # pillar: the prompt asks the planner not to, but models ignore it.
        coverage = state.get("rule_coverage")
        if coverage is not None:
            kept = [c for c in checks if not coverage.covers(c.column.upper(), c.category, c.sub_type)]
            if len(kept) < len(checks):
                dropped = [f"{c.column}/{c.category}" for c in checks if c not in kept]
                logger.info("Discarded %d planner check(s) for table %s already covered by SAP rules: %s",
                            len(dropped), state["table_name"], ", ".join(dropped))
                metrics.planner_checks_covered_by_rules += len(dropped)
            checks = kept
        checks = checks[:Config.MAX_TOTAL_CHECKS_PER_TABLE]

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

        proposed = state["proposed_checks"]
        summary_lines = [
            f"[{r['check_index']}] category={proposed[r['check_index']].category} column={r['column']} "
            f"hypothesis=\"{r['hypothesis']}\" result={r['result']}"
            for r in successful
        ]
        prompt = "Evaluate EACH check result below. Return a judgment per check_index.\n\n" + "\n".join(summary_lines)

        batch: ReflectionBatch = reflector_structured.invoke([
            SystemMessage(content=REFLECTOR_SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ])
        metrics.reflector_llm_calls += 1

        results_by_index = {r["check_index"]: r for r in successful}
        checks_by_index = {i: c for i, c in enumerate(state["proposed_checks"])}
        findings = []

        for j in batch.judgments:
            r = results_by_index.get(j.check_index)
            check = checks_by_index.get(j.check_index)
            if r is None or not j.is_issue:
                continue

            # The category belongs to the check the planner designed (its code tests
            # that pillar). FindingJudgment.category defaults to CORRECTNESS, so
            # letting the reflector win silently relabeled checks - e.g. a KOINH
            # completeness check stored as a DUPLICATE finding.
            category = check.category if check else (j.category or "CORRECTNESS")
            if category == "DUPLICATE":
                continue
            rule_scope = j.rule_scope or (check.rule_scope if check else "UNIVERSAL")
            industry = j.industry or (check.industry if check else None)
            fix_type = j.fix_type or (check.fix_type if check else None)
            auto_fix_value = j.auto_fix_value or (check.auto_fix_value if check else None)
            is_anomaly = bool(j.is_anomaly or (check.is_anomaly if check else False))
            sub_type = j.sub_type or (check.sub_type if check else None)

            findings.append({
                "table": state["table_name"], "column": r["column"],
                "hypothesis": r["hypothesis"], "check_code": r["check_code"],
                "summary": j.summary, "severity": j.severity,
                "confidence": j.confidence, "reusable": j.reusable,
                "category": category, "rule_scope": rule_scope,
                "industry": industry, "fix_type": fix_type,
                "auto_fix_value": auto_fix_value, "is_anomaly": is_anomaly,
                "sub_type": sub_type,
                "raw_tool_result": str(r["result"]),
                "_check_index": r["check_index"],
            })

        # for each confirmed finding, extract row-level detail locally (one sandbox worker for all)
        for finding in findings:
            finding["detail_rows"] = []
        with_detail = [(f, checks_by_index[f["_check_index"]]) for f in findings
                       if f.get("_check_index") in checks_by_index and checks_by_index[f["_check_index"]].detail_code]
        if with_detail:
            with sandbox_session(state["df"], state["all_tables"]) as box:
                for finding, check in with_detail:
                    finding["detail_rows"] = extract_detail_rows(check, state["df"], state["all_tables"], box=box)

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
