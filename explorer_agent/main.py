
import json
import sys
import time
import argparse
from pathlib import Path

from .config import Config
from .data_loader import (discover_table_files, load_all_tables, load_data_dictionary,
                          dictionary_column_types, get_field_description)
# from .tools import TOOLS, register_dataframe
from .graph import build_explorer_graph
# from .schemas import Reflection
from .schemas import Reflection, CheckPlan, ReflectionBatch
from .table_profiler import profile_table
from .cache_runner import run_cached_skills
from .duplicate_detector import detect_table_duplicates
from .duplicate_rule_planner import RulePlanner
from .sap_rules import RuleCoverage, load_pack, run_sap_rules
from . import column_mapping, events, scorecard, survivorship, structural_profile
from .duplicate_detector import LAST_STATS as DUPLICATE_STATS
from .data_loader import load_data_dictionary_structured
from . import client_knowledge
from .memory.retriever import SkillRetriever
from . import episodic_store as store
# from . import profiler_primitives as prim
from .metrics import metrics
from .logging_config import get_logger

from .llm_providers import LLMChainExhaustedError, build_llms, close_local_llm

logger = get_logger("main")


def explore_table(graph, table_name, df, dictionary, all_tables, skill_retriever, reflector_single,
                  rule_coverage=None) -> list:
    logger.info("=== Exploring table %s (batch mode) ===", table_name)
    columns = list(df.columns)

    cached_findings = []
    columns_with_cache = set()
    if Config.ENABLE_CACHE_FAST_PATH:
        for column in columns:
            col_cached = run_cached_skills(table_name, column, df, reflector_llm=reflector_single)
            if col_cached:
                cached_findings.extend(col_cached)
                columns_with_cache.add(column)
    # Per-column cache_hits/cache_misses are recorded inside run_cached_skills()
    # itself (see cache_runner.py) - counting them again here at table
    # granularity would double-count hits against the same metric.

    profile = profile_table(df, table_name)
    for var in profile["variables"]:
        var["business_meaning"] = get_field_description(dictionary, table_name, var["column"])

    hint_lines = []
    for var in profile["variables"]:
        col = var["column"]
        if col in columns_with_cache:
            continue
        hints = skill_retriever.retrieve_hints(table=table_name, column=col,
                                                dtype=var.get("detected_type", ""),
                                                business_meaning=var.get("business_meaning", ""))
        if hints:
            hint_lines.append(f"- {col}: " + "; ".join(h["hypothesis"] for h in hints[:2]))
    hints_text = ("Relevant hints from past projects (suggestions, not rules - verify relevance):\n"
                  + "\n".join(hint_lines)) if hint_lines else "No relevant memory hints for this table."

    def _describe_other_table(name: str) -> str:
        key_cols = sorted({
            field for (tbl, field), desc in dictionary.items()
            if tbl == name and "key" in desc.lower()
        })
        return f"{name} (key: {', '.join(key_cols)})" if key_cols else name

    other_tables_note = (
        "Other registered tables available via tables['<name>'] for cross-table lookups: "
        f"{[_describe_other_table(t) for t in all_tables if t != table_name]}"
        if len(all_tables) > 1 else "No other tables registered."
    )

    # Rule descriptions only (no data) - so the planner does not re-invent them.
    # Naming the untouched columns too: told only what NOT to do, a model can
    # return an empty plan.
    if rule_coverage and rule_coverage.lines:
        touched = {col for col, _, _ in rule_coverage.pairs}
        untouched = [c for c in columns if c.upper() not in touched]
        rules_note = (
            "Checks ALREADY RUN by the built-in SAP rule engine on this table - do NOT propose these again:\n"
            + "\n".join(f"- {line}" for line in rule_coverage.lines)
            + f"\nColumns no built-in rule checks at all: {untouched}. Covered columns can still have other "
              "problems (e.g. text hygiene in a name, a client-specific value set), so a check on them is fine "
              "as long as it tests something the rules above do not. Propose checks as usual otherwise."
        )
    else:
        rules_note = "No built-in SAP rules apply to this table."

    cache_note = (f"Columns with EXISTING approved checks (deprioritize unless new insight): "
                  f"{sorted(columns_with_cache)}" if columns_with_cache else "No cached checks exist yet.")

    if profile["table"]["profiled_rows"] < profile["table"]["n_rows"]:
        profile_note = (f"Column profiles (statistical, privacy-sanitized) - computed on a random sample of "
                        f"{profile['table']['profiled_rows']} of the {profile['table']['n_rows']} rows: counts "
                        f"(n, n_missing, n_distinct, top value counts) refer to the sample, p_* ratios estimate the "
                        f"whole table, and is_unique only means unique within the sample. Your checks run on all rows.")
    else:
        profile_note = "Column profiles (statistical, privacy-sanitized):"

    seed_prompt = f"""Table: {table_name}
Row count: {profile['table']['n_rows']}

{profile_note}
{json.dumps(profile['variables'], indent=2, default=str)}

{rules_note}
{cache_note}
{hints_text}
{other_tables_note}

Propose roughly 1-2 checks per notable column (skip clean-looking columns). Do not exceed
~{Config.MAX_TOTAL_CHECKS_PER_TABLE} checks total.
"""

    initial_state = {
        "table_name": table_name, "seed_prompt": seed_prompt, "df": df,
        "all_tables": all_tables, "proposed_checks": [], "check_results": [], "findings": [],
        "rule_coverage": rule_coverage or RuleCoverage(),
    }
    final_state = graph.invoke(initial_state)
    fresh_findings = final_state["findings"]

    logger.info("Table %s complete - cached=%d fresh=%d", table_name, len(cached_findings), len(fresh_findings))
    return cached_findings + fresh_findings


def _rule_chain_factory(model, temperature, chain="duplicate_rules_structured"):
    """Builds a structured chain on demand (see RulePlanner) - nothing is
    constructed, and no local model loaded, until a table needs new rules
    (``chain`` picks duplicate-rule drafting or column mapping)."""
    def build():
        bundle = build_llms(model, temperature)
        return getattr(bundle, chain), bundle.chain_label
    return build


def main():
    store.init_db()
    metrics.reset()
    start_time = time.perf_counter()

    parser = argparse.ArgumentParser(description="Week 4: Batch table-level Explorer")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument(
        "--client", required=True,
        help="Client/company the data belongs to, e.g. \"Acme Retail\". Runs, findings and remembered "
             "review decisions (memory_store/clients/<client>/) are linked to it.",
    )
    parser.add_argument("--dictionary-file", default=Config.DATA_DICTIONARY_FILE)
    parser.add_argument("--model", default=None,
                        help="Model for the PRIMARY provider (default: its model in config.yaml).")
    parser.add_argument("--temperature", type=float, default=None,
                        help="Temperature for the PRIMARY provider (default: config.yaml).")
    parser.add_argument("--max-iterations", type=int, default=Config.MAX_ITERATIONS_PER_COLUMN)
    parser.add_argument("--tables", nargs="*", default=None)
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument(
        "--duplicates-only", action="store_true",
        help="Only run deterministic duplicate matching. Free for tables whose rules are already "
             "saved for this client; a table/schema never seen before costs one call to draft them "
             "(and is limited to identical rows if no LLM is configured).",
    )
    parser.add_argument(
        "--mapping-file", default=None,
        help="The Mapping Agent's field/value mapping (contract sap-dm.field-value-mapping). Default: "
             "<data-dir>/" + Config.HANDOFF_MAPPING_FILE + " when it exists.",
    )
    parser.add_argument(
        "--target-domains-file", default=None,
        help="Allowed SAP values per target field (contract sap-dm.target-domains). Default: "
             "<data-dir>/" + Config.HANDOFF_TARGET_DOMAINS_FILE + " when it exists.",
    )
    parser.add_argument(
        "--deterministic-only", action="store_true",
        help="Run only the LLM-free engines: duplicate matching plus the built-in SAP rule pack "
             "(sap_rules.py). No planner or reflector call; duplicate rules for a schema never seen "
             "before still cost one call, as with --duplicates-only.",
    )
    parser.add_argument(
        "--llm-provider", default=Config.LLM_PROVIDER, choices=Config.SUPPORTED_LLM_PROVIDERS,
        help="Primary LLM backend (overrides config.yaml/EXPLORER_LLM_PROVIDER).",
    )
    parser.add_argument(
        "--fallback-providers", nargs="*", default=None, choices=Config.SUPPORTED_LLM_PROVIDERS,
        help="Ordered fallback backends (overrides config.yaml/EXPLORER_LLM_FALLBACKS). "
             "Pass the flag with no values to disable fallbacks.",
    )
    args = parser.parse_args()

    if args.no_cache:
        Config.ENABLE_CACHE_FAST_PATH = False
    Config.LLM_PROVIDER = args.llm_provider
    if args.fallback_providers is not None:
        Config.LLM_FALLBACK_PROVIDERS = args.fallback_providers

    if args.duplicates_only and args.deterministic_only:
        parser.error("--duplicates-only and --deterministic-only are mutually exclusive")
    no_planner = args.duplicates_only or args.deterministic_only
    if not no_planner:
        Config.validate()
    if Config.SAP_RULES_ENABLED and not args.duplicates_only:
        load_pack()  # fail fast on a broken rule pack, before any table is processed
    try:
        client = client_knowledge.ensure_client(args.client)
    except ValueError as exc:
        parser.error(str(exc))

    logger.info(
        "Starting run | client=%s provider=%s fallbacks=%s tables=%s cache_enabled=%s duplicates_only=%s "
        "deterministic_only=%s sap_rules=%s",
        client["name"], Config.LLM_PROVIDER, Config.LLM_FALLBACK_PROVIDERS, args.tables,
        Config.ENABLE_CACHE_FAST_PATH, args.duplicates_only, args.deterministic_only, Config.SAP_RULES_ENABLED,
    )

    dictionary_path = str(Path(args.data_dir) / args.dictionary_file)
    dictionary = load_data_dictionary(dictionary_path)
    # SAP data types from the dictionary, so CHAR keys keep their zero padding
    # and are not silently turned into numbers - see data_loader.load_table.
    column_types = dictionary_column_types(dictionary_path)
    discovered = discover_table_files(args.data_dir, args.dictionary_file)
    wanted = {t.upper() for t in args.tables} if args.tables else None
    table_files = {k: v for k, v in discovered.items() if wanted is None or k in wanted}
    if not table_files:
        parser.error(f"No table CSV files found in {args.data_dir}"
                     + (f" matching --tables {' '.join(args.tables)}" if args.tables else ""))
    tables = load_all_tables(args.data_dir, table_files, column_types)

    if no_planner:
        llms = graph = reflector_single = skill_retriever = None
        run_label = "duplicate-detector (no LLM)" if args.duplicates_only else "deterministic rules (no LLM)"
        # Matching rules already saved for this client are reused as they are, so a
        # duplicates-only run normally stays free. A table whose schema has never
        # been seen for this client still needs one call to draft its rules, so the
        # chain is built lazily - only if such a table actually turns up, and only
        # when the primary provider is configured.
        rule_planner = mapping_planner = None
        if not Config.provider_missing_settings(Config.LLM_PROVIDER):
            rule_planner = RulePlanner(_rule_chain_factory(args.model, args.temperature))
            mapping_planner = RulePlanner(_rule_chain_factory(args.model, args.temperature,
                                                              "column_mapping_structured"))
        else:
            logger.warning("No LLM credentials configured: tables without saved duplicate rules "
                           "will only be checked for identical rows.")
    else:
        llms = build_llms(args.model, args.temperature)
        graph = build_explorer_graph(llms.planner_structured, llms.reflector_structured)
        reflector_single = llms.reflector_single
        skill_retriever = SkillRetriever()
        run_label = llms.chain_label
        rule_planner = RulePlanner(lambda: (llms.duplicate_rules_structured, llms.chain_label),
                                   label=llms.chain_label)
        mapping_planner = RulePlanner(lambda: (llms.column_mapping_structured, llms.chain_label),
                                      label=llms.chain_label)

    # What every column means, for the deterministic rules - resolved for ALL tables
    # first, because a rule on one table reads the mapping of others (orphans,
    # dormancy). SAP-standard layouts and saved mappings cost nothing; a new
    # non-standard layout costs one LLM call per table, once per client.
    mapping_file = args.mapping_file or str(Path(args.data_dir) / Config.HANDOFF_MAPPING_FILE)
    domains_file = args.target_domains_file or str(Path(args.data_dir) / Config.HANDOFF_TARGET_DOMAINS_FILE)
    try:
        mapping_agent = column_mapping.load_mapping_agent_file(mapping_file)
        target_domains = column_mapping.load_target_domains(domains_file)
    except Exception as exc:  # a broken handoff must not be silently half-used
        parser.error(f"Handoff input is invalid ({mapping_file} / {domains_file}): {exc}")
    mappings = {}
    if Config.SAP_RULES_ENABLED and not args.duplicates_only:
        mappings = column_mapping.resolve_mappings(tables, load_pack(), dictionary, client_id=client["client_id"],
                                                   client_name=client["name"], planner=mapping_planner,
                                                   mapping_agent=mapping_agent)
    if target_domains and mappings:
        logger.info("Target domains attached to %d column(s)",
                    column_mapping.attach_target_domains(mappings, target_domains))
    # What the rules see: values after the Mapping Agent's value mapping (all tables).
    rule_tables = column_mapping.apply_value_maps(tables, mappings)

    run_id = store.create_run(model=run_label, table_names=list(tables.keys()),
                              client_id=client["client_id"], client_name=client["name"])
    logger.info("Run ID: %s", run_id)

    total_findings = 0
    failed_tables = []
    table_scores = []
    for table_name, df in tables.items():
        table_start = time.perf_counter()
        # Deterministic duplicate detection first: zero LLM cost, and its
        # findings are kept even if the LLM chain fails for this table.
        findings = []
        duplicate_finding = detect_table_duplicates(table_name, df, client_id=client["client_id"],
                                                    dictionary=dictionary, client_name=client["name"],
                                                    rule_planner=rule_planner)
        if duplicate_finding:
            findings.append(duplicate_finding)
        # Known SAP standards next - also zero LLM cost, also kept if the LLM fails.
        rule_coverage, rule_findings = RuleCoverage(), []
        if not args.duplicates_only:
            rule_findings, rule_coverage = run_sap_rules(table_name, rule_tables[table_name], rule_tables, dictionary,
                                                         client_id=client["client_id"], mappings=mappings)
            findings += rule_findings
        # Quality score + recommended survivor per duplicate group - a pre-selection
        # for the reviewer, never a verdict. Uses the rules' per-row defects.
        if duplicate_finding:
            survivorship.annotate(duplicate_finding, table_name, rule_tables.get(table_name, df), mappings,
                                  rule_tables, rule_findings)
        if not args.duplicates_only and Config.SAP_RULES_ENABLED:
            table_scores.append(scorecard.score_table(
                table_name, rule_tables[table_name], mappings.get(table_name), rule_coverage, rule_findings,
                duplicate_finding, DUPLICATE_STATS.get(table_name),
                scorecard.migration_object(table_name, mappings.get(table_name), load_pack(),
                                           table_files.get(table_name))))
        if not no_planner:
            try:
                findings += explore_table(graph, table_name, df, dictionary, tables, skill_retriever,
                                          reflector_single, rule_coverage=rule_coverage)
            except LLMChainExhaustedError as exc:
                # One table's LLM outage shouldn't discard the rest of the run.
                logger.error("[%s] LLM exploration skipped - %s", table_name, exc)
                failed_tables.append(table_name)

        for f in findings:
            finding_id = store.save_finding(
                run_id=run_id, table=f["table"], column=f["column"],
                hypothesis=f.get("hypothesis", ""), check_code=f.get("check_code", ""),
                result_summary=f["summary"], severity=f["severity"],
                confidence=f["confidence"], reusable=f["reusable"],
                raw_result=f.get("raw_tool_result"),
                category=f.get("category", "CORRECTNESS"),
                rule_scope=f.get("rule_scope", "UNIVERSAL"),
                industry=f.get("industry"),
                fix_type=f.get("fix_type"),
                auto_fix_value=f.get("auto_fix_value"),
                is_anomaly=bool(f.get("is_anomaly", False)),
                sub_type=f.get("sub_type"),
            )
            detail_rows = f.get("detail_rows", [])
            if detail_rows:
                store.save_finding_items(finding_id, detail_rows)
                logger.info("Saved %d detail row(s) for finding %s", len(detail_rows), finding_id[:8])

        total_findings += len(findings)
        logger.info("[%s] done in %.2fs - %d finding(s)", table_name, time.perf_counter() - table_start, len(findings))

    # Composite DQ scorecard per table, migration object and run (deterministic engines only).
    dq = None
    if table_scores:
        entries = scorecard.build(table_scores)
        store.save_scorecard(run_id, client["client_id"], entries, Config.SCORECARD_WEIGHTS)
        dq = entries[-1]

    # Handoff to the Mapping / Value Mapping Agent: what every source column looks like.
    handoff_path = None
    try:
        doc = structural_profile.build_profile(tables, table_files, load_data_dictionary_structured(dictionary_path),
                                               mappings, client, run_id)
        handoff_path = structural_profile.write_profile(doc)
    except Exception as exc:
        logger.error("Structural profile (handoff) could not be written: %s", exc)

    elapsed = time.perf_counter() - start_time
    metrics.log_summary(logger)

    print(f"\nRun {run_id} complete.")
    print(f"Total findings: {total_findings}")
    print(f"Total execution time: {elapsed:.2f}s ({elapsed/60:.2f} min)")
    print(f"LLM calls - Planner: {metrics.planner_llm_calls} | Reflector: {metrics.reflector_llm_calls} | "
          f"Duplicate rules: {metrics.duplicate_rule_llm_calls}")
    print(f"Duplicate rules - Reused from memory: {metrics.duplicate_rule_hits} | "
          f"Drafted for a new schema: {metrics.duplicate_rule_misses}")
    print(f"SAP rules (no LLM) - Rule groups run: {metrics.sap_rules_evaluated} | "
          f"Findings: {metrics.sap_rule_findings} (anomalies: {metrics.anomaly_findings}) | "
          f"Rows flagged: {metrics.sap_rule_rows} | "
          f"Planner checks dropped as already covered: {metrics.planner_checks_covered_by_rules}")
    status = "COMPLETED" if not failed_tables else "PARTIAL"
    try:
        events.publish("profiling.completed", client["client_id"], client["name"], run_id, status, {
            "tables": list(tables.keys()), "findings": total_findings, "failed_tables": failed_tables,
            "structural_profile": {
                "path": str(handoff_path) if handoff_path else None,
                "url": f"/api/clients/{client['client_id']}/handoff/structural-profile?run_id={run_id}"
                       if handoff_path else None},
            "duplicate_decisions_url": f"/api/clients/{client['client_id']}/duplicate-decisions.csv",
            "mapping_input": "mapping-agent" if mapping_agent else None,
            "target_domains": len(target_domains) or None,
            "dq_index": dq["dq_index"] if dq else None,
            "record_readiness": dq["readiness"]["score"] if dq else None,
            "scorecard_url": f"/api/scorecard?client_id={client['client_id']}&run_id={run_id}" if dq else None,
        })
    except Exception as exc:
        logger.error("profiling.completed event could not be published: %s", exc)
    if dq:
        r = dq["readiness"]
        print(f"Record readiness: {'n/a' if r['score'] is None else format(r['score'], '.1%')} - {r['ready']:,} of "
              f"{r['in_scope']:,} in-scope records loadable as they are; {r['not_ready']:,} need work; "
              f"{r['out_of_scope']:,} out of scope (deleted/dormant)")
        print("DQ Index (all tables): " + ("n/a" if dq["dq_index"] is None else f"{dq['dq_index']:.1%}") + " | " +
              " | ".join(f"{p.capitalize()}: {'n/a' if not dq['pillars'][p] else format(dq['pillars'][p]['score'], '.1%')}"
                         for p in scorecard.PILLARS))
    print(f"Handoff - structural profile: {handoff_path or 'NOT written (see log)'}")
    print(f"Column mapping - Mapping Agent: {metrics.column_mapping_agent} | SAP standard (free): "
          f"{metrics.column_mapping_standard} | Reused from memory: "
          f"{metrics.column_mapping_hits} | Mapped by LLM: {metrics.column_mapping_llm_calls} | "
          f"Failed: {metrics.column_mapping_failures}")
    print(f"Cache - Hits: {metrics.cache_hits} | Misses: {metrics.cache_misses}")
    print(f"LLM fallbacks - Failed attempts: {metrics.llm_call_failures} | "
          f"Served by fallback: {metrics.llm_fallback_calls}")
    print(f"\nReview at: http://localhost:8000")

    # One machine-readable line for the job manager (review_app/job_manager.py).
    print("RESULT_JSON: " + json.dumps({"run_id": run_id, "client_id": client["client_id"], "status": status,
                                        "structural_profile": str(handoff_path) if handoff_path else None}))
    if failed_tables:
        print(f"\nTables whose LLM exploration was skipped because every LLM failed: {failed_tables} "
              f"- re-run with --tables {' '.join(failed_tables)}")
        return 1
    return 0

if __name__ == "__main__":
    try:
        exit_code = main()
    finally:
        close_local_llm()
    sys.exit(exit_code)
