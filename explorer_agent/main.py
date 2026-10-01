
import json
import sys
import time
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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
from .memory import skill_registry as registry
from .skill_reuse import reuse_by_similarity
from . import episodic_store as store
# from . import profiler_primitives as prim
from .llm_usage import usage
from .metrics import metrics
from .logging_config import get_logger

from .llm_providers import LLMChainExhaustedError, build_llms, close_local_llm

logger = get_logger("main")


_OTHER_TABLE_COLUMN_CAP = 40   # columns listed per other table in the planner prompt


def _known_findings_for(client_id: str, data_dir: str, table_file: Optional[str], table_name: str) -> dict:
    """Findings this client already has on a table that this run should not create again (empty when the
    feature is off). 'Same data' = the table's file is not newer than the earlier run: if it was uploaded
    again since, what those findings describe is gone, so nothing is skipped."""
    if not Config.SKIP_KNOWN_FINDINGS:
        return {}
    changed_at = None
    if table_file:
        path = Path(data_dir) / table_file
        if path.exists():
            changed_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    try:
        return store.known_findings(client_id, table_name, changed_at)
    except Exception as exc:   # an unreadable history must never stop a run: just recreate everything
        logger.warning("[%s] could not look up the client's earlier findings (%s) - recreating them", table_name, exc)
        return {}


def explore_table(graph, table_name, df, dictionary, all_tables, skill_retriever, reflector_single,
                  rule_coverage=None, known_checks=None, column_mapping=None) -> list:
    """`known_checks`: {(COLUMN, category)} the client already has findings for on this table (same data):
    no planner check is made for them, and the planner is told which they are.
    `column_mapping`: this table's mapping (what each column means), used to match skills by meaning."""
    known_checks = known_checks or set()
    logger.info("=== Exploring table %s (batch mode) ===", table_name)
    columns = list(df.columns)

    cached_findings = []
    columns_with_cache = set()
    skill_covered = set()   # {(COLUMN, pillar)} a promoted skill already checks - the planner must not redo them
    if Config.ENABLE_CACHE_FAST_PATH:
        for column in columns:
            skills = registry.get_skills_for_table_column(table_name, column)
            if skills:
                columns_with_cache.add(column)
                skill_covered |= {(str(column).upper(), registry.skill_classification(s)["category"]) for s in skills}
            col_cached = run_cached_skills(table_name, column, df, reflector_llm=reflector_single,
                                           all_tables=all_tables)
            if col_cached:
                cached_findings.extend(col_cached)
        # Hybrid: a column with no EXACT skill may still get a promoted skill by meaning (its mapped concept,
        # name and dictionary text against what each skill checks) - adapted to this column, run, and judged.
        bindings = (column_mapping or {}).get("columns", {})
        by_meaning, meaning_cover = reuse_by_similarity(
            table_name, df, all_tables,
            [c for c in columns if c not in columns_with_cache],
            {c: (bindings.get(c) or {}).get("concept") for c in columns},
            {c: get_field_description(dictionary, table_name, c) for c in columns},
            getattr(skill_retriever, "memory_store", None), reflector_llm=reflector_single, covered=known_checks,
            rule_coverage=rule_coverage)
        cached_findings.extend(by_meaning)
        skill_covered |= meaning_cover
    # Per-column cache_hits/cache_misses are recorded inside run_cached_skills()
    # itself (see cache_runner.py) - counting them again here at table
    # granularity would double-count hits against the same metric.
    if known_checks and cached_findings:
        # A promoted skill re-runs every time; its finding for a column the client already has one for
        # would only be a second copy.
        kept = [f for f in cached_findings
                if (str(f.get("column", "")).upper(), f.get("category") or "CORRECTNESS") not in known_checks]
        if len(kept) < len(cached_findings):
            metrics.known_findings_skipped += len(cached_findings) - len(kept)
            logger.info("[%s] %d cached-skill finding(s) the client already has were not recreated",
                        table_name, len(cached_findings) - len(kept))
        cached_findings = kept
    # What the planner must not propose again: findings the client already has, plus what promoted skills
    # check (even when the skill found nothing this time - the check exists, re-inventing it is waste).
    known_checks = known_checks | skill_covered

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
        # Column names (metadata only, like the repair and column-mapping prompts). Without them - and
        # without a dictionary there is no key hint either - the planner can only guess what to look up,
        # and copies a key name from the prompt's example instead (KeyError in production). Capped so a
        # wide table doesn't flood the prompt.
        cols = [str(c) for c in all_tables[name].columns]
        shown = ", ".join(cols[:_OTHER_TABLE_COLUMN_CAP]) + (f", ... (+{len(cols) - _OTHER_TABLE_COLUMN_CAP} more)"
                                                             if len(cols) > _OTHER_TABLE_COLUMN_CAP else "")
        return (f"{name} (key: {', '.join(key_cols)}; columns: {shown})" if key_cols
                else f"{name} (columns: {shown})")

    other_tables_note = (
        "Other registered tables available via tables['<name>'] for cross-table lookups, with their columns - "
        "use only these names: "
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
    if known_checks:
        cache_note += ("\nALREADY CHECKED for this client in an earlier run on this same data - do NOT propose these "
                       "again, look for other problems: "
                       + ", ".join(f"{col}/{cat}" for col, cat in sorted(known_checks)))

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
        "rule_coverage": rule_coverage or RuleCoverage(), "repair_round": 0, "to_run": None,
        "known_checks": known_checks,
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
    usage.reset()
    start_time = time.perf_counter()

    parser = argparse.ArgumentParser(description="Week 4: Batch table-level Explorer")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument(
        "--client", required=True,
        help="Client/company the data belongs to, e.g. \"Acme Retail\". Runs, findings and remembered "
             "review decisions (memory_store/clients/<client>/) are linked to it.",
    )
    parser.add_argument("--dictionary-file", default=Config.DATA_DICTIONARY_FILE)
    parser.add_argument(
        "--no-dictionary", action="store_true",
        help="Run without a data dictionary (the client has none): column meaning then comes from "
             "names, statistics and the SAP rule pack only, and number columns are typed by a safe "
             "guess (values with leading zeros stay text).",
    )
    parser.add_argument("--model", default=None,
                        help="Model for the PRIMARY provider (default: its model in config.yaml).")
    parser.add_argument("--temperature", type=float, default=None,
                        help="Temperature for the PRIMARY provider (default: config.yaml).")
    parser.add_argument("--max-repair-rounds", type=int, default=Config.MAX_REPAIR_ROUNDS,
                        help="Rounds in which failed planner checks are sent back to the planner (0 = off).")
    parser.add_argument(
        "--no-skip-known", action="store_true",
        help="Recreate every finding even when this client already has it from an earlier run on the same "
             "data (default: skip those, see profiling.skip_known_findings in config.yaml).")
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
        help="LLM backend, google or local (overrides config.yaml/EXPLORER_LLM_PROVIDER).",
    )
    args = parser.parse_args()

    if args.no_cache:
        Config.ENABLE_CACHE_FAST_PATH = False
    if args.no_skip_known:
        Config.SKIP_KNOWN_FINDINGS = False
    Config.LLM_PROVIDER = args.llm_provider
    Config.MAX_REPAIR_ROUNDS = max(0, args.max_repair_rounds)

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
        "Starting run | client=%s provider=%s tables=%s cache_enabled=%s duplicates_only=%s "
        "deterministic_only=%s sap_rules=%s",
        client["name"], Config.LLM_PROVIDER, args.tables,
        Config.ENABLE_CACHE_FAST_PATH, args.duplicates_only, args.deterministic_only, Config.SAP_RULES_ENABLED,
    )

    if args.no_dictionary:
        # Nothing to read, and nothing in the folder to skip: every CSV there is a table.
        dictionary_path, dictionary, column_types, dictionary_file = None, {}, {}, None
        logger.info("Running without a data dictionary (--no-dictionary): no column descriptions or "
                    "declared data types; column meaning comes from names, statistics and the rule pack.")
    else:
        dictionary_path = str(Path(args.data_dir) / args.dictionary_file)
        dictionary = load_data_dictionary(dictionary_path)
        # SAP data types from the dictionary, so CHAR keys keep their zero padding
        # and are not silently turned into numbers - see data_loader.load_table.
        column_types = dictionary_column_types(dictionary_path)
        dictionary_file = args.dictionary_file
    discovered = discover_table_files(args.data_dir, dictionary_file)
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
        # when the provider is configured.
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
        graph = build_explorer_graph(llms.planner_structured, llms.reflector_structured, llms.repair_structured)
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
    usage.set_run(run_id)  # stores the calls made so far (column mapping) and every later one

    total_findings = 0
    failed_tables = []
    table_scores = []
    for table_name, df in tables.items():
        table_start = time.perf_counter()
        usage.set_table(table_name)
        known = _known_findings_for(client["client_id"], args.data_dir, table_files.get(table_name), table_name)
        known_checks = {(key[2], key[3]) for key in known if key[0] == "CHECK"}
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
                                          reflector_single, rule_coverage=rule_coverage, known_checks=known_checks,
                                          column_mapping=mappings.get(table_name))
            except LLMChainExhaustedError as exc:
                # One table's LLM outage shouldn't discard the rest of the run.
                logger.error("[%s] LLM exploration skipped - %s", table_name, exc)
                failed_tables.append(table_name)

        # Findings this client already has from an earlier run on the same data are not created again
        # (the scorecard above was computed from the full rule output, so it is unaffected).
        if known:
            to_save = [f for f in findings if f.get("category") == "DUPLICATE"
                       or store.finding_key(f["table"], f.get("column"), f.get("category"), f.get("check_code")) not in known]
            skipped = len(findings) - len(to_save)
            if skipped:
                metrics.known_findings_skipped += skipped
                logger.info("[%s] %d finding(s) the client already has (earlier run, same data) were not recreated",
                            table_name, skipped)
            findings = to_save

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
                detail_code=f.get("detail_code"),
            )
            detail_rows = f.get("detail_rows", [])
            if detail_rows:
                store.save_finding_items(finding_id, detail_rows)
                logger.info("Saved %d detail row(s) for finding %s", len(detail_rows), finding_id[:8])
            if Config.SKIP_KNOWN_FINDINGS and f.get("category") != "DUPLICATE":
                # The new finding takes over from an earlier copy of the same check that is still pending,
                # unpromoted and has no records (it would otherwise pile up, one more per run). A finding a
                # human decided, or that became a skill, is never replaced.
                replaced = store.supersede_empty_findings(
                    client["client_id"], table_name,
                    store.finding_key(f["table"], f.get("column"), f.get("category"), f.get("check_code")),
                    finding_id)
                if replaced:
                    logger.info("[%s] %s: replaced %d earlier pending finding(s) that had no row-level records",
                                table_name, f.get("column"), replaced)

        total_findings += len(findings)
        logger.info("[%s] done in %.2fs - %d finding(s)", table_name, time.perf_counter() - table_start, len(findings))

    usage.set_table(None)

    # Composite DQ scorecard per table, migration object and run (deterministic engines only).
    dq = None
    if table_scores:
        entries = scorecard.build(table_scores)
        store.save_scorecard(run_id, client["client_id"], entries, Config.SCORECARD_WEIGHTS)
        dq = entries[-1]

    # Handoff to the Mapping / Value Mapping Agent: what every source column looks like.
    handoff_path = None
    try:
        doc = structural_profile.build_profile(tables, table_files,
                                               load_data_dictionary_structured(dictionary_path) if dictionary_path else {},
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
    if Config.SKIP_KNOWN_FINDINGS:
        print(f"Findings the client already had (earlier run, same data) and were not recreated: "
              f"{metrics.known_findings_skipped}   (--no-skip-known recreates them)")
    status = "COMPLETED" if not failed_tables else "PARTIAL"
    try:
        events.publish("profiling.completed", client["client_id"], client["name"], run_id, status, {
            "tables": list(tables.keys()), "findings": total_findings, "failed_tables": failed_tables,
            "known_findings_skipped": metrics.known_findings_skipped,
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
            "llm_usage": usage.totals(),
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
    print(f"Pre-flight - checks rejected before running: {metrics.preflight_rejected} | Repair - calls: "
          f"{metrics.repair_llm_calls}, checks fixed: {metrics.checks_repaired}")
    print(f"LLM - Failed calls: {metrics.llm_call_failures}")
    tokens = usage.totals()
    print(f"LLM tokens - {tokens['calls']} call(s): in {tokens['input_tokens']:,} | out {tokens['output_tokens']:,}"
          + (f" (of which thinking {tokens['reasoning_tokens']:,})" if tokens["reasoning_tokens"] else "")
          + f" | total {tokens['total_tokens']:,}")
    print(f"\nReview at: http://localhost:8000")

    # One machine-readable line for the job manager (review_app/job_manager.py).
    print("RESULT_JSON: " + json.dumps({"run_id": run_id, "client_id": client["client_id"], "status": status,
                                        "structural_profile": str(handoff_path) if handoff_path else None,
                                        "llm_usage": usage.totals()}))
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
