"""
CRITICAL PRIVACY BOUNDARY.

The sandbox executes agent-generated code locally over REAL data. That code
sets a `result` variable, and we send THAT result back to the LLM during the
"reflect" step so the LLM can judge severity/confidence.

Problem: nothing stops agent-generated code from accidentally putting raw
PII into `result` (e.g. `result = df[df['col'].isna()]` - a full DataFrame
with real values). This module is the last line of defense before anything
computed locally is shown to the LLM.

This is a best-effort heuristic layer, NOT a certified PII scanner.
Harden this before using with real client data (e.g. add regex-based PII
detectors, structured-type allowlists, or a dedicated scrubber library).
"""

from typing import Any, Optional
import pandas as pd

from .profiler_primitives import mask_value

from .config import Config


def sanitize_result_for_llm(result: Any, max_list_len: Optional[int] = None) -> Any:
    # NOTE: default is None (not a literal), otherwise this would always
    # shadow Config.MAX_RESULT_LIST_LEN and the env var could never take effect.
    max_list_len = max_list_len if max_list_len is not None else Config.MAX_RESULT_LIST_LEN
    if isinstance(result, (pd.DataFrame, pd.Series)):
        return {
            "_type": "DataFrame/Series (WITHHELD)",
            "shape": getattr(result, "shape", None),
            "note": ("Raw tabular result withheld from LLM for privacy. "
                     "Check code should return an aggregate (count/pct/bool), "
                     "not raw rows/columns.")
        }

    if isinstance(result, dict):
        # Recursively sanitize dict values (common for structured results)
        return {k: sanitize_result_for_llm(v, max_list_len) for k, v in result.items()}

    if isinstance(result, (list, tuple)):
        if len(result) > max_list_len:
            return {
                "_type": "list (TRUNCATED + MASKED)",
                "length": len(result),
                "sample": [mask_value(v) for v in list(result)[:5]],
            }
        # Short lists: still mask if elements look like raw strings (heuristic)
        if all(isinstance(v, str) for v in result):
            return [mask_value(v) for v in result]
        return list(result)

    if isinstance(result, str) and len(result) > 200:
        return result[:200] + "...(TRUNCATED)"

    # scalars (int, float, bool, short str, None) pass through unchanged
    return result
