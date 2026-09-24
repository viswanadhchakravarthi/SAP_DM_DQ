"""Token usage of every LLM call, so the cost of a run is visible.

A `UsageCollector` (LangChain callback) is attached to each structured call in
`llm_providers._structured_chain`. It reads the token counts the provider
already returns and hands them to the shared `usage` recorder, which

1. logs one line per call (console + logs/ file),
2. adds them to the in-process `metrics` totals, and
3. stores one row per call in SQLite (`llm_calls`, see episodic_store.py).

Only counts, timings, the model label and the schema name are kept - never a
prompt or a response, so this stays outside the privacy boundary. There are no
prices here on purpose: token counts are facts, prices change.

The run row doesn't exist yet while column mapping runs, so records are buffered
until `usage.set_run(run_id)` is called.
"""

import time
from typing import Any, Dict, List, Optional
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

from .logging_config import get_logger
from .metrics import metrics

logger = get_logger("llm_usage")

# Schema name -> what the call is for.
ROLES = {
    "CheckPlan": "planner",
    "CheckRepair": "repair",
    "ReflectionBatch": "reflector",
    "Reflection": "reflector",
    "DuplicateRulePlan": "duplicate_rules",
    "ColumnMappingPlan": "column_mapping",
}


class UsageRecorder:
    """Collects one record per LLM request for the current run."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.records: List[Dict[str, Any]] = []
        self.run_id: Optional[str] = None
        self.table: Optional[str] = None
        self._unsaved: List[Dict[str, Any]] = []

    def set_table(self, table: Optional[str]) -> None:
        """Table the following calls belong to (None for client-wide calls such as column mapping)."""
        self.table = table

    def set_run(self, run_id: str) -> None:
        """Run row now exists: store everything buffered so far, and store the rest as it happens."""
        self.run_id = run_id
        pending, self._unsaved = self._unsaved, []
        for record in pending:
            self._store(record)

    def record(self, *, schema: str, model: str, input_tokens: Optional[int], output_tokens: Optional[int],
               reasoning_tokens: Optional[int], total_tokens: Optional[int], seconds: float,
               success: bool = True, error: Optional[str] = None) -> Dict[str, Any]:
        record = {
            "table_name": self.table, "role": ROLES.get(schema, schema), "model": model,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "reasoning_tokens": reasoning_tokens, "total_tokens": total_tokens,
            "seconds": round(seconds, 2), "success": success, "error": (error or "")[:300] or None,
        }
        self.records.append(record)
        metrics.llm_input_tokens += input_tokens or 0
        metrics.llm_output_tokens += output_tokens or 0
        metrics.llm_reasoning_tokens += reasoning_tokens or 0
        if success:
            logger.info("[%s] %s tokens in=%s out=%s%s total=%s | %.1fs%s", record["role"], model,
                        _fmt(input_tokens), _fmt(output_tokens),
                        f" (thinking {_fmt(reasoning_tokens)})" if reasoning_tokens else "",
                        _fmt(total_tokens), seconds, f" | table {self.table}" if self.table else "")
        if self.run_id:
            self._store(record)
        else:
            self._unsaved.append(record)
        return record

    def _store(self, record: Dict[str, Any]) -> None:
        try:
            from . import episodic_store  # local import: episodic_store must not depend on this module
            episodic_store.save_llm_call(self.run_id, record)
        except Exception as exc:  # cost bookkeeping must never fail a run
            logger.warning("Could not store LLM usage record: %s", exc)

    def totals(self) -> Dict[str, Any]:
        ok = [r for r in self.records if r["success"]]
        return {
            "calls": len(ok), "failed_calls": len(self.records) - len(ok),
            "input_tokens": sum(r["input_tokens"] or 0 for r in ok),
            "output_tokens": sum(r["output_tokens"] or 0 for r in ok),
            "reasoning_tokens": sum(r["reasoning_tokens"] or 0 for r in ok),
            "total_tokens": sum(r["total_tokens"] or 0 for r in ok),
        }


def _fmt(n: Optional[int]) -> str:
    return "?" if n is None else f"{n:,}"


usage = UsageRecorder()  # single shared instance, like `metrics`


def _token_counts(response: Any) -> Dict[str, Optional[int]]:
    """input/output/reasoning/total tokens from a LangChain LLMResult (None when the provider gave none)."""
    meta: Dict[str, Any] = {}
    try:
        meta = dict(response.generations[0][0].message.usage_metadata or {})
    except (AttributeError, IndexError, TypeError):
        pass
    if not meta:
        meta = dict((getattr(response, "llm_output", None) or {}).get("token_usage") or {})
        meta = {"input_tokens": meta.get("prompt_tokens"), "output_tokens": meta.get("completion_tokens"),
                "total_tokens": meta.get("total_tokens")}
    details = meta.get("output_token_details") or {}
    return {"input_tokens": meta.get("input_tokens"), "output_tokens": meta.get("output_tokens"),
            "reasoning_tokens": details.get("reasoning") if isinstance(details, dict) else None,
            "total_tokens": meta.get("total_tokens")}


class UsageCollector(BaseCallbackHandler):
    """Attached to one structured call; records every model request made inside it
    (a structured-output retry is a second, separately billed request)."""

    def __init__(self, schema: str, model: str) -> None:
        self.schema = schema
        self.model = model
        self._started: Dict[UUID, float] = {}

    def on_chat_model_start(self, serialized: Any, messages: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._started[run_id] = time.perf_counter()

    def on_llm_start(self, serialized: Any, prompts: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self._started.setdefault(run_id, time.perf_counter())

    def on_llm_end(self, response: Any, *, run_id: UUID, **kwargs: Any) -> None:
        seconds = time.perf_counter() - self._started.pop(run_id, time.perf_counter())
        usage.record(schema=self.schema, model=self.model, seconds=seconds, **_token_counts(response))

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        seconds = time.perf_counter() - self._started.pop(run_id, time.perf_counter())
        usage.record(schema=self.schema, model=self.model, input_tokens=None, output_tokens=None,
                     reasoning_tokens=None, total_tokens=None, seconds=seconds, success=False,
                     error=str(error).splitlines()[0] if str(error) else type(error).__name__)
