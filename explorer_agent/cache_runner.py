"""Run previously approved data-quality skills without planner regeneration.

On a cache hit, the Planner LLM is skipped because the approved check code is
already available. The check still runs against fresh data, and its result is
normally re-interpreted by the Reflector LLM. Set
``Config.SKIP_REFLECTION_ON_CACHE_HIT`` to use the rule-based fallback instead.
"""

from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from .config import Config
from .llm_providers import LLMChainExhaustedError
from .logging_config import get_logger
from .memory import skill_registry as registry
from .metrics import metrics
from .privacy_guard import sanitize_result_for_llm
from .sandbox import SandboxExecutor
from .schemas import Reflection

logger = get_logger("cache_runner")
sandbox = SandboxExecutor(
    timeout_seconds=Config.SANDBOX_TIMEOUT_SECONDS,
    mem_limit_mb=Config.SANDBOX_MEM_LIMIT_MB,
    load_timeout_seconds=Config.SANDBOX_LOAD_TIMEOUT_SECONDS,
)


MAX_DETAIL_ROWS = 50


def run_cached_skills(
    table_name: str,
    column: str,
    df: pd.DataFrame,
    reflector_llm: Optional[Any] = None,
    all_tables: Optional[Dict[str, pd.DataFrame]] = None,
) -> List[Dict[str, Any]]:
    """Execute approved skills for one table column and return fresh findings.

    A finding from a skill is as complete as a fresh one: it carries the skill's own classification
    (pillar, scope, fix type...) and, when the skill has `detail_code`, its row-level records. The other
    tables are available to the code (`tables[...]`), so a cross-table check does not fail for lack of them."""
    cached_skills = registry.get_skills_for_table_column(table_name, column)

    if not cached_skills:
        logger.debug("Cache MISS: no cached skills for %s.%s", table_name, column)
        metrics.cache_misses += 1
        return []

    logger.info(
        "Cache HIT: %d cached skill(s) found for %s.%s; Planner LLM call skipped",
        len(cached_skills),
        table_name,
        column,
    )
    metrics.cache_hits += 1

    findings: List[Dict[str, Any]] = []

    context = {"df": df, "tables": all_tables if all_tables is not None else {table_name: df}}
    with sandbox.session(context) as box:
        executed = []
        for skill in cached_skills:
            exec_result = box.run(skill["check_code"])
            detail_rows: List[Dict[str, Any]] = []
            if exec_result.get("success") and skill.get("detail_code"):
                detail = box.run(skill["detail_code"])
                metrics.sandbox_executions += 1
                if detail.get("success") and isinstance(detail.get("result"), list):
                    detail_rows = detail["result"][:MAX_DETAIL_ROWS]
                else:
                    logger.warning("Cached skill %s: its detail_code did not produce rows (%s)",
                                   skill["skill_id"][:8], detail.get("error") or "not a list")
            executed.append((skill, exec_result, detail_rows))

    for skill, exec_result, detail_rows in executed:
        skill_id = skill["skill_id"]
        logger.debug("Re-executed cached skill %s: %s", skill_id[:8], skill["hypothesis"])
        metrics.sandbox_executions += 1

        if not exec_result.get("success"):
            logger.warning(
                "Cached skill %s failed against current data (possible schema drift): %s",
                skill_id[:8],
                exec_result.get("error"),
            )
            continue

        sanitized = sanitize_result_for_llm(exec_result.get("result"))
        is_issue, severity, confidence, summary = _interpret_result(
            skill,
            sanitized,
            reflector_llm,
        )

        if is_issue:
            cls = registry.skill_classification(skill)
            findings.append(
                {
                    "table": table_name,
                    "column": column,
                    "hypothesis": skill["hypothesis"],
                    "check_code": skill["check_code"],
                    "detail_code": cls["detail_code"],
                    "detail_rows": detail_rows,
                    "category": cls["category"],
                    "sub_type": cls["sub_type"],
                    "rule_scope": cls["rule_scope"],
                    "industry": cls["industry"],
                    "fix_type": cls["fix_type"],
                    "is_anomaly": cls["is_anomaly"],
                    "summary": summary,
                    "severity": severity,
                    "confidence": confidence,
                    "reusable": True,
                    "raw_tool_result": str({"result": sanitized}),
                    "from_cache": True,
                    "source_skill_id": skill_id,
                }
            )
            logger.info(
                "Cached skill %s re-confirmed an issue on fresh data (severity=%s)",
                skill_id[:8],
                severity,
            )
        else:
            logger.info(
                "Cached skill %s issue is no longer present in fresh data",
                skill_id[:8],
            )

    return findings


def _heuristic_interpretation(skill: Dict[str, Any], sanitized: Any) -> Tuple[bool, str, str, str]:
    """Rule-based stand-in for the reflector: any non-empty/non-zero result is an issue."""
    is_issue = bool(sanitized) and sanitized not in (0, 0.0, False, "", None, {})
    severity = skill["severity_example"] if is_issue else "INFO"
    confidence = "MEDIUM"
    summary = (
        f"[Cached check re-run] {skill['description']} - result: {sanitized}"
        if is_issue
        else "[Cached check re-run] Previously flagged issue not detected "
        f"in current data (result: {sanitized})"
    )
    return is_issue, severity, confidence, summary


def _interpret_result(
    skill: Dict[str, Any],
    sanitized: Any,
    reflector_llm: Optional[Any],
) -> Tuple[bool, str, str, str]:
    """Interpret a cached-check result with the reflector or a safe fallback."""
    if Config.SKIP_REFLECTION_ON_CACHE_HIT or reflector_llm is None:
        return _heuristic_interpretation(skill, sanitized)

    try:
        reflection: Reflection = reflector_llm.invoke(
            "Re-evaluate this cached data-quality check against fresh data.\n"
            f"Original check hypothesis: {skill['hypothesis']}\n"
            f"Fresh result (sanitized): {sanitized}"
        )
    except LLMChainExhaustedError as exc:
        # The check code itself is already human-approved, so the rule-based
        # heuristic is a safe degradation when no LLM is reachable.
        logger.warning("Reflector unavailable for cached skill %s - using rule-based fallback (%s)",
                       skill["skill_id"][:8], exc)
        return _heuristic_interpretation(skill, sanitized)
    metrics.reflector_llm_calls += 1

    return (
        reflection.is_issue,
        reflection.severity,
        reflection.confidence,
        reflection.summary,
    )
