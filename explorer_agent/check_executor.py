"""Execute a batch of structured ProposedCheck objects locally.

The planner uses structured output rather than iterative tool calls, so this
module runs checks directly through the sandbox. Detail rows remain local: they
are stored for the review UI and are never passed to an LLM.
"""

from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from .config import Config
from .logging_config import get_logger
from .metrics import metrics
from .preflight import preflight
from .privacy_guard import sanitize_result_for_llm
from .sandbox import SandboxExecutor
from .schemas import ProposedCheck

logger = get_logger("check_executor")
_sandbox = SandboxExecutor(
    timeout_seconds=Config.SANDBOX_TIMEOUT_SECONDS,
    mem_limit_mb=Config.SANDBOX_MEM_LIMIT_MB,
    load_timeout_seconds=Config.SANDBOX_LOAD_TIMEOUT_SECONDS,
)


def _is_structural_metadata(result: Any, known_columns: Iterable[str]) -> bool:
    """Return whether the result is merely a list of known column names."""
    if isinstance(result, list) and all(isinstance(value, str) for value in result):
        return set(result).issubset(set(known_columns))
    return False


def execute_checks(
    checks: List[ProposedCheck],
    df: pd.DataFrame,
    all_tables: Dict[str, pd.DataFrame],
) -> List[Dict[str, Any]]:
    """Run each proposed check and sanitize results before LLM consumption."""
    results: List[Dict[str, Any]] = []
    if not checks:
        return results

    # Free static checks first: code that can't run is rejected with a precise reason instead of
    # crashing in the sandbox. Only `code` blocks; a detail_code problem just loses row detail later.
    executed: List[Dict[str, Any]] = [{}] * len(checks)
    runnable = []
    for index, check in enumerate(checks):
        problems = preflight(check.code, df, all_tables)
        if problems:
            metrics.preflight_rejected += 1
            executed[index] = {"success": False, "result": None, "error": "Pre-flight: " + "; ".join(problems),
                               "preflight": True}
        else:
            runnable.append(index)
        if check.detail_code:
            detail_problems = preflight(check.detail_code, df, all_tables)
            if detail_problems:
                logger.warning("Check #%d on column %s: detail_code will probably fail - %s",
                               index, check.column, "; ".join(detail_problems))
    if runnable:
        with sandbox_session(df, all_tables) as box:
            for index in runnable:
                executed[index] = box.run(checks[index].code)

    for index, (check, exec_result) in enumerate(zip(checks, executed)):
        if not exec_result.get("preflight"):
            metrics.sandbox_executions += 1

        raw_result = exec_result.get("result")
        sanitized = (
            {"_type": "structural_metadata", "value": raw_result}
            if _is_structural_metadata(raw_result, df.columns)
            else sanitize_result_for_llm(raw_result)
        )

        results.append(
            {
                "check_index": index,
                "column": check.column,
                "hypothesis": check.hypothesis,
                "check_code": check.code,
                "success": exec_result.get("success"),
                "result": sanitized,
                "error": exec_result.get("error"),
                "preflight_rejected": bool(exec_result.get("preflight")),
            }
        )

        if not exec_result.get("success"):
            logger.warning(
                "Check #%d on column %s failed: %s",
                index,
                check.column,
                exec_result.get("error"),
            )

    return results


def sandbox_session(df: pd.DataFrame, all_tables: Dict[str, pd.DataFrame]):
    """One sandbox worker for several checks on this table (the data is sent once)."""
    return _sandbox.session({"df": df, "tables": all_tables})


def extract_detail_rows(
    check: ProposedCheck,
    df: pd.DataFrame,
    all_tables: Dict[str, pd.DataFrame],
    max_rows: int = 50,
    box: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    """Extract local row-level detail for a finding confirmed by the reflector.

    ``detail_code`` is intentionally not passed through ``privacy_guard``. Its
    output goes only to the local SQLite store and the human-review UI.
    ``box`` is an open ``sandbox_session`` to reuse; without one, a worker is
    started for this check alone.
    """
    if not check.detail_code:
        return []

    exec_result = (box.run(check.detail_code) if box is not None
                   else _sandbox.run(check.detail_code, context={"df": df, "tables": all_tables}))
    metrics.sandbox_executions += 1

    if not exec_result.get("success"):
        logger.warning(
            "detail_code failed for column %s: %s",
            check.column,
            exec_result.get("error"),
        )
        return []

    detail_rows = exec_result.get("result")
    if not isinstance(detail_rows, list):
        logger.warning(
            "detail_code for %s did not return a list; got %s",
            check.column,
            type(detail_rows).__name__,
        )
        return []

    return detail_rows[:max_rows]
