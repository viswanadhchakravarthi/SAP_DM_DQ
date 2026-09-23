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
    # observation_tool_calls: int = 0  # NEW - tracks "free" tool calls (no sandbox, no reflection)
    cache_hits: int = 0       # columns served (at least partially) from cache
    cache_misses: int = 0     # columns with no cached skills at all
    llm_call_failures: int = 0   # model attempts that failed (after SDK retries) - see llm_providers.py
    llm_fallback_calls: int = 0  # structured calls answered by a non-primary model
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

    def log_summary(self, logger):
        logger.info(
            "RUN METRICS SUMMARY | planner_llm_calls=%d reflector_llm_calls=%d "
            "duplicate_rule_llm_calls=%d duplicate_rule_hits=%d duplicate_rule_misses=%d "
            "sandbox_executions=%d cache_hits=%d cache_misses=%d "
            "llm_call_failures=%d llm_fallback_calls=%d "
            "sap_rules_evaluated=%d sap_rule_findings=%d sap_rule_rows=%d planner_checks_covered_by_rules=%d",
            self.planner_llm_calls, self.reflector_llm_calls,
            self.duplicate_rule_llm_calls, self.duplicate_rule_hits, self.duplicate_rule_misses,
            self.sandbox_executions, self.cache_hits, self.cache_misses,
            self.llm_call_failures, self.llm_fallback_calls,
            self.sap_rules_evaluated, self.sap_rule_findings, self.sap_rule_rows,
            self.planner_checks_covered_by_rules,
        )

    def reset(self):
        self.planner_llm_calls = 0
        self.reflector_llm_calls = 0
        self.sandbox_executions = 0
        # self.observation_tool_calls = 0
        self.cache_hits = 0
        self.cache_misses = 0
        self.llm_call_failures = 0
        self.llm_fallback_calls = 0
        self.duplicate_rule_llm_calls = 0
        self.duplicate_rule_hits = 0
        self.duplicate_rule_misses = 0
        self.sap_rules_evaluated = 0
        self.sap_rule_findings = 0
        self.sap_rule_rows = 0
        self.planner_checks_covered_by_rules = 0


metrics = RunMetrics()  # single shared instance, imported wherever needed