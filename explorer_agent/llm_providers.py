"""LLM backend construction with cross-provider / cross-model fallbacks.

`build_llms()` turns Config's provider chain (llm.provider followed by
llm.fallback_providers) into an ordered list of chat models, e.g.

    google:gemini-3.6-flash -> groq:qwen/qwen3.8-27b -> groq:openai/gpt-oss-20b

and wraps each structured-output role (planner / reflector) as
`first.with_fallbacks(rest)`. Failure handling is layered:

1. Transient errors (429, 5xx, timeouts) are retried INSIDE each provider's SDK
   with backoff, `Config.LLM_MAX_RETRIES` times (Groq's SDK also honours
   `retry-after`). Keep this low so an overloaded model fails over quickly.
2. Malformed/missing structured output (invalid JSON, skipped tool call) is
   retried on the SAME model `Config.LLM_STRUCTURED_OUTPUT_RETRIES` times.
3. Anything still failing - including non-retryable errors such as "request
   too large" on a small free-tier quota - moves on to the next model.
4. Only if every model fails is an `LLMChainExhaustedError` raised; callers
   decide how to degrade (main.py skips the table, cache_runner falls back to
   its rule-based heuristic).

Callers (graph.py, cache_runner.py) keep calling `.invoke(...)` unchanged.

Provider SDKs are imported lazily per provider, and the local GGUF model is
only loaded when "local" is actually part of the chain.
"""

import logging
from typing import Any, List, NamedTuple, Optional, Sequence, Tuple

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.runnables import Runnable, RunnableLambda

from .config import Config
from .local_llms import QwenCoderGGUFChatModel
from .logging_config import get_logger
from .metrics import metrics
from .schemas import CheckPlan, DuplicateRulePlan, Reflection, ReflectionBatch

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
    chain_label: str              # e.g. "google:gemini-3.6-flash -> groq:openai/gpt-oss-20b"


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

    if provider == "groq":
        from langchain_groq import ChatGroq

        temp = Config.GROQ_TEMPERATURE if temperature is None else temperature
        entries = Config.GROQ_MODELS
        if model:  # CLI override: reuse that model's configured options if it is listed
            entries = [next((e for e in entries if e["name"] == model),
                            {"name": model, "structured_output_method": Config.GROQ_STRUCTURED_OUTPUT_METHOD})]
        candidates = []
        for entry in entries:
            kwargs = dict(
                model=entry["name"],
                api_key=Config.GROQ_API_KEY,
                max_retries=Config.LLM_MAX_RETRIES,
                timeout=Config.LLM_TIMEOUT_SECONDS,
            )
            if temp is not None:
                kwargs["temperature"] = temp
            for option in ("reasoning_effort", "max_tokens"):
                if entry.get(option) is not None:
                    kwargs[option] = entry[option]
            candidates.append(LLMCandidate(
                f"groq:{entry['name']}", ChatGroq(**kwargs),
                {"method": entry["structured_output_method"]},
            ))
        return candidates

    if provider == "local":
        return [LLMCandidate("local:gguf", QwenCoderGGUFChatModel(llm=_get_local_llm()), {})]

    raise ValueError(
        f"Unknown LLM provider {provider!r} (expected one of {Config.SUPPORTED_LLM_PROVIDERS})"
    )


def build_candidates(model: Optional[str] = None, temperature: Optional[float] = None) -> List[LLMCandidate]:
    """Ordered candidates for the primary provider followed by each fallback.

    `model`/`temperature` (CLI overrides) apply to the primary provider only.
    Fallback providers missing required settings are skipped with a warning.
    """
    candidates = _provider_candidates(Config.LLM_PROVIDER, model, temperature)

    seen = {Config.LLM_PROVIDER}
    for provider in Config.LLM_FALLBACK_PROVIDERS:
        if provider in seen:
            continue
        seen.add(provider)
        missing = Config.provider_missing_settings(provider)
        if missing:
            logger.warning("Skipping fallback provider %r - missing %s", provider, ", ".join(missing))
            continue
        candidates.extend(_provider_candidates(provider))
    return candidates


# --------------------------------------------------------------------------- #
# Structured-output chains with fallbacks
# --------------------------------------------------------------------------- #
def _last_error_line(run: Any) -> str:
    """Run.error holds a formatted traceback; the last line is 'ExcType: message'."""
    lines = [ln for ln in str(getattr(run, "error", "") or "").strip().splitlines() if ln.strip()]
    return (lines[-1] if lines else "unknown error")[:500]


class StructuredOutputError(ValueError):
    """The model answered, but not with usable structured output."""


class LLMChainExhaustedError(RuntimeError):
    """Every model in the provider chain failed for one structured call."""


# Provider error codes meaning "the model's output didn't fit the schema" (vs.
# outages or bad requests) - Groq returns these as HTTP 400.
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


def _raise_exhausted(chain: Runnable, chain_label: str, schema_name: str) -> Runnable:
    """Re-raise the chain's final failure as one clear LLMChainExhaustedError
    (with_fallbacks itself re-raises only the FIRST model's error)."""

    def call(input_: Any, config: Any) -> Any:
        try:
            return chain.invoke(input_, config)
        except Exception as exc:
            raise LLMChainExhaustedError(
                f"All LLMs failed for {schema_name} ({chain_label}); "
                f"see the warnings above for each model. First error: {str(exc).splitlines()[0][:300]}"
            ) from exc

    return RunnableLambda(call, name=f"{schema_name}_chain")


def _structured_chain(candidates: Sequence[LLMCandidate], schema: type) -> Runnable:
    schema_name = schema.__name__
    wrapped = []
    for i, cand in enumerate(candidates):
        next_label = candidates[i + 1].label if i + 1 < len(candidates) else None

        def on_error(run, label=cand.label, next_label=next_label):
            metrics.llm_call_failures += 1
            if next_label:
                logger.warning("[%s] %s failed (%s) - falling back to %s",
                               schema_name, label, _last_error_line(run), next_label)
            else:
                logger.error("[%s] %s failed (%s) - no fallback models left",
                             schema_name, label, _last_error_line(run))

        def on_end(run, label=cand.label, is_fallback=i > 0):
            if is_fallback:
                metrics.llm_fallback_calls += 1
                logger.info("[%s] served by fallback model %s", schema_name, label)

        runnable = _with_output_retries(
            cand.llm.with_structured_output(schema, **cand.structured_kwargs),
            cand.label, schema_name, Config.LLM_STRUCTURED_OUTPUT_RETRIES,
        ).with_listeners(on_end=on_end, on_error=on_error)
        wrapped.append(runnable)

    chain = wrapped[0] if len(wrapped) == 1 else wrapped[0].with_fallbacks(wrapped[1:])
    return _raise_exhausted(chain, " -> ".join(c.label for c in candidates), schema_name)


def build_llms(model: Optional[str] = None, temperature: Optional[float] = None) -> LLMBundle:
    """Builds planner/reflector runnables over the configured provider chain
    (config.yaml's llm.provider + llm.fallback_providers)."""
    candidates = build_candidates(model, temperature)
    chain_label = " -> ".join(c.label for c in candidates)
    logger.info("LLM chain: %s", chain_label)
    return LLMBundle(
        planner_structured=_structured_chain(candidates, CheckPlan),
        reflector_structured=_structured_chain(candidates, ReflectionBatch),
        reflector_single=_structured_chain(candidates, Reflection),  # used by cache_runner
        duplicate_rules_structured=_structured_chain(candidates, DuplicateRulePlan),
        chain_label=chain_label,
    )
