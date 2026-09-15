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

    def log_summary(self, logger):
        logger.info(
            "RUN METRICS SUMMARY | planner_llm_calls=%d reflector_llm_calls=%d "
            "sandbox_executions=%d cache_hits=%d cache_misses=%d "
            "llm_call_failures=%d llm_fallback_calls=%d",
            self.planner_llm_calls, self.reflector_llm_calls, self.sandbox_executions,
            self.cache_hits, self.cache_misses,
            self.llm_call_failures, self.llm_fallback_calls,
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


metrics = RunMetrics()  # single shared instance, imported wherever needed