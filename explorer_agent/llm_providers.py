"""LLM backend construction: exactly one model, Gemini or a local GGUF.

`build_llms()` builds the provider selected by Config.LLM_PROVIDER
(llm.provider in config.yaml) and wraps each structured-output role (planner /
reflector / ...) with two layers of failure handling. There is deliberately no
fallback to another provider or model:

1. Transient errors (429, 5xx, timeouts) are retried INSIDE the provider's SDK
   with backoff, `Config.LLM_MAX_RETRIES` times.
2. Malformed/missing structured output (invalid JSON, skipped tool call) is
   retried on the SAME model `Config.LLM_STRUCTURED_OUTPUT_RETRIES` times.
3. If it still fails, an `LLMChainExhaustedError` is raised; callers decide how
   to degrade (main.py skips the table, cache_runner falls back to its
   rule-based heuristic).

Callers (graph.py, cache_runner.py) just call `.invoke(...)`.

The provider SDK is imported lazily, and the local GGUF model is only loaded
when provider is "local".
"""

import logging
from typing import Any, List, NamedTuple, Optional, Sequence, Tuple

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.runnables import Runnable, RunnableLambda

from .config import Config
from .local_llms import QwenCoderGGUFChatModel
from .logging_config import get_logger
from .metrics import metrics
from .schemas import CheckPlan, ColumnMappingPlan, DuplicateRulePlan, Reflection, ReflectionBatch

logger = get_logger("llm_providers")


class _DropAfcNotice(logging.Filter):
    """google-genai logs "Direct use of automatic function calling (AFC) ... is
    not recommended" once per process whenever a request doesn't explicitly
    configure AFC. LangChain never hands Gemini callable Python tools (structured
    output uses json_schema), so AFC can't actually run - the notice is noise."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "automatic function calling (AFC)" not in record.getMessage()


logging.getLogger("google_genai.models").addFilter(_DropAfcNotice())


class LLMCandidate(NamedTuple):
    label: str                    # "provider:model", used in logs and run metadata
    llm: BaseChatModel
    structured_kwargs: dict       # extra kwargs for .with_structured_output()


class LLMBundle(NamedTuple):
    planner_structured: Runnable
    reflector_structured: Runnable
    reflector_single: Runnable
    duplicate_rules_structured: Runnable   # drafts duplicate-matching rules (once per client+schema)
    column_mapping_structured: Runnable    # maps columns to business concepts (once per client+schema)
    chain_label: str              # e.g. "google:gemini-3.6-flash"


# --------------------------------------------------------------------------- #
# Local GGUF model (lazy)
# --------------------------------------------------------------------------- #
_local_llm_instance = None


def _get_local_llm():
    """Lazily instantiate (and cache) the local GGUF model. Only called when
    "local" is in the provider chain; never imports llama_cpp otherwise."""
    global _local_llm_instance
    if _local_llm_instance is None:
        from llama_cpp import Llama  # imported here, not at module load time
        import os
        logger.info(
            "Loading local GGUF model from %s (n_ctx=%d)...",
            Config.LOCAL_LLM_MODEL_PATH, Config.LOCAL_LLM_N_CTX,
        )
        _local_llm_instance = Llama(
            model_path=Config.LOCAL_LLM_MODEL_PATH, 
            n_ctx=Config.LOCAL_LLM_N_CTX,
            n_threads=os.cpu_count(),
            verbose=False,
        )
    return _local_llm_instance


def close_local_llm() -> None:
    """Release the local GGUF model if one was ever loaded. Safe no-op otherwise."""
    global _local_llm_instance
    if _local_llm_instance is not None:
        _local_llm_instance.close()
        _local_llm_instance = None


# --------------------------------------------------------------------------- #
# Per-provider model construction
# --------------------------------------------------------------------------- #
def _provider_candidates(
    provider: str, model: Optional[str] = None, temperature: Optional[float] = None,
) -> List[LLMCandidate]:
    """Chat models for one provider. `model`/`temperature` override config."""
    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        model_name = model or Config.GEMINI_MODEL
        kwargs: dict = dict(
            model=model_name,
            google_api_key=Config.GEMINI_API_KEY,
            # google-genai counts total ATTEMPTS (1 = no retries; 0 = SDK default of 6).
            max_retries=Config.LLM_MAX_RETRIES + 1,
            timeout=Config.LLM_TIMEOUT_SECONDS,
        )
        temp = Config.GEMINI_TEMPERATURE if temperature is None else temperature
        if temp is not None:
            kwargs["temperature"] = temp
        return [LLMCandidate(f"google:{model_name}", ChatGoogleGenerativeAI(**kwargs), {})]

    if provider == "local":
        return [LLMCandidate("local:gguf", QwenCoderGGUFChatModel(llm=_get_local_llm()), {})]

    raise ValueError(
        f"Unknown LLM provider {provider!r} (expected one of {Config.SUPPORTED_LLM_PROVIDERS})"
    )


def build_candidates(model: Optional[str] = None, temperature: Optional[float] = None) -> List[LLMCandidate]:
    """The configured provider's model. `model`/`temperature` are CLI overrides."""
    return _provider_candidates(Config.LLM_PROVIDER, model, temperature)


# --------------------------------------------------------------------------- #
# Structured-output chains
# --------------------------------------------------------------------------- #
def _last_error_line(run: Any) -> str:
    """Run.error holds a formatted traceback; the last line is 'ExcType: message'."""
    lines = [ln for ln in str(getattr(run, "error", "") or "").strip().splitlines() if ln.strip()]
    return (lines[-1] if lines else "unknown error")[:500]


class StructuredOutputError(ValueError):
    """The model answered, but not with usable structured output."""


class LLMChainExhaustedError(RuntimeError):
    """The configured model failed for one structured call, after its retries."""


# Provider error codes meaning "the model's output didn't fit the schema" (vs.
# outages or bad requests).
_OUTPUT_FAILURE_CODES = ("json_validate_failed", "tool_use_failed")


def _is_output_failure(exc: BaseException) -> bool:
    from langchain_core.exceptions import OutputParserException
    from pydantic import ValidationError

    if isinstance(exc, (StructuredOutputError, OutputParserException, ValidationError)):
        return True
    return any(code in str(exc) for code in _OUTPUT_FAILURE_CODES)


def _with_output_retries(structured: Runnable, label: str, schema_name: str, retries: int) -> Runnable:
    """Invoke `structured`, re-asking the SAME model up to `retries` extra times
    when its output is malformed or missing. Tool-calling parsers return None
    when the model skips the tool call; that is treated as a failure too, so the
    graph never receives None (which would crash on `plan.checks`)."""

    def call(input_: Any, config: Any) -> Any:
        for attempt in range(retries + 1):
            try:
                output = structured.invoke(input_, config)
                if output is None:
                    raise StructuredOutputError(f"Model returned no structured {schema_name} output")
                return output
            except Exception as exc:
                if attempt >= retries or not _is_output_failure(exc):
                    raise
                logger.warning("[%s] %s returned invalid structured output (%s) - retry %d/%d",
                               schema_name, label, str(exc).splitlines()[0][:200], attempt + 1, retries)

    return RunnableLambda(call, name=label)


def _structured_chain(candidates: Sequence[LLMCandidate], schema: type) -> Runnable:
    schema_name = schema.__name__
    cand = candidates[0]

    def on_error(run):
        metrics.llm_call_failures += 1
        logger.error("[%s] %s failed (%s)", schema_name, cand.label, _last_error_line(run))

    runnable = _with_output_retries(
        cand.llm.with_structured_output(schema, **cand.structured_kwargs),
        cand.label, schema_name, Config.LLM_STRUCTURED_OUTPUT_RETRIES,
    ).with_listeners(on_error=on_error)

    def call(input_: Any, config: Any) -> Any:
        try:
            return runnable.invoke(input_, config)
        except Exception as exc:
            raise LLMChainExhaustedError(
                f"{cand.label} failed for {schema_name}: {str(exc).splitlines()[0][:300]}"
            ) from exc

    return RunnableLambda(call, name=f"{schema_name}_chain")


def build_llms(model: Optional[str] = None, temperature: Optional[float] = None) -> LLMBundle:
    """Builds planner/reflector runnables for the configured provider (llm.provider)."""
    candidates = build_candidates(model, temperature)
    chain_label = candidates[0].label
    logger.info("LLM: %s", chain_label)
    return LLMBundle(
        planner_structured=_structured_chain(candidates, CheckPlan),
        reflector_structured=_structured_chain(candidates, ReflectionBatch),
        reflector_single=_structured_chain(candidates, Reflection),  # used by cache_runner
        duplicate_rules_structured=_structured_chain(candidates, DuplicateRulePlan),
        column_mapping_structured=_structured_chain(candidates, ColumnMappingPlan),
        chain_label=chain_label,
    )
