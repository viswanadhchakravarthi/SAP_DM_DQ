"""
In-process metrics - resets per script run. Purpose: make LLM-call
savings from the cache fast path CONCRETE and demonstrable (e.g. to
management), rather than an unverified claim.
"""

from dataclasses import dataclass


@dataclass
class RunMetrics:
    planner_llm_calls: int = 0
    reflector_llm_calls: int = 0
    sandbox_executions: int = 0
    cache_hits: int = 0       # columns served (at least partially) from cache
    cache_misses: int = 0     # columns with no cached skills at all
    preflight_rejected: int = 0  # planner checks rejected before execution (preflight.py)
    llm_call_failures: int = 0   # model attempts that failed (after SDK retries) - see llm_providers.py
    # Token counts of every LLM request (llm_usage.py); facts only, no prices.
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_reasoning_tokens: int = 0  # thinking tokens, when the provider reports them (already billed as output)
    # Duplicate matching rules: drafted by the LLM once per client+schema, then
    # served from memory (see duplicate_rules.resolve_rules).
    duplicate_rule_llm_calls: int = 0
    duplicate_rule_hits: int = 0    # tables whose rules came from saved memory
    duplicate_rule_misses: int = 0  # tables with no saved rules for this schema
    # Deterministic SAP rule pack (sap_rules.py) - zero LLM calls by design.
    sap_rules_evaluated: int = 0  # rule groups that ran (table/column present, not disabled)
    sap_rule_findings: int = 0
    sap_rule_rows: int = 0
    planner_checks_covered_by_rules: int = 0  # planner checks dropped because a rule already ran them
    anomaly_findings: int = 0          # of sap_rule_findings, from anomaly_rules.py
    # Column mapping (column_mapping.py): what each column means, for the rule engines.
    column_mapping_agent: int = 0      # tables mapped from the Mapping Agent's input file (free)
    mapping_candidates_below_threshold: int = 0  # PROPOSED candidates under handoff.min_confidence (not used)
    column_mapping_standard: int = 0   # tables mapped from the SAP-standard pack (free)
    column_mapping_hits: int = 0       # tables whose saved mapping was reused (free)
    column_mapping_llm_calls: int = 0  # tables mapped by an LLM call (first sight of a schema)
    column_mapping_failures: int = 0   # tables the LLM could not map (partial/no mapping used)

    def log_summary(self, logger):
        logger.info(
            "RUN METRICS SUMMARY | planner_llm_calls=%d reflector_llm_calls=%d "
            "duplicate_rule_llm_calls=%d duplicate_rule_hits=%d duplicate_rule_misses=%d "
            "sandbox_executions=%d cache_hits=%d cache_misses=%d "
            "preflight_rejected=%d llm_call_failures=%d llm_input_tokens=%d llm_output_tokens=%d llm_reasoning_tokens=%d "
            "sap_rules_evaluated=%d sap_rule_findings=%d sap_rule_rows=%d planner_checks_covered_by_rules=%d "
            "anomaly_findings=%d column_mapping_standard=%d column_mapping_hits=%d column_mapping_llm_calls=%d "
            "column_mapping_failures=%d",
            self.planner_llm_calls, self.reflector_llm_calls,
            self.duplicate_rule_llm_calls, self.duplicate_rule_hits, self.duplicate_rule_misses,
            self.sandbox_executions, self.cache_hits, self.cache_misses,
            self.preflight_rejected, self.llm_call_failures, self.llm_input_tokens, self.llm_output_tokens, self.llm_reasoning_tokens,
            self.sap_rules_evaluated, self.sap_rule_findings, self.sap_rule_rows,
            self.planner_checks_covered_by_rules, self.anomaly_findings, self.column_mapping_standard,
            self.column_mapping_hits, self.column_mapping_llm_calls, self.column_mapping_failures,
        )

    def reset(self):
        self.planner_llm_calls = 0
        self.reflector_llm_calls = 0
        self.sandbox_executions = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.preflight_rejected = 0
        self.llm_call_failures = 0
        self.llm_input_tokens = 0
        self.llm_output_tokens = 0
        self.llm_reasoning_tokens = 0
        self.duplicate_rule_llm_calls = 0
        self.duplicate_rule_hits = 0
        self.duplicate_rule_misses = 0
        self.sap_rules_evaluated = 0
        self.sap_rule_findings = 0
        self.sap_rule_rows = 0
        self.planner_checks_covered_by_rules = 0
        self.anomaly_findings = 0
        self.column_mapping_agent = 0
        self.mapping_candidates_below_threshold = 0
        self.column_mapping_standard = 0
        self.column_mapping_hits = 0
        self.column_mapping_llm_calls = 0
        self.column_mapping_failures = 0


metrics = RunMetrics()  # single shared instance, imported wherever needed