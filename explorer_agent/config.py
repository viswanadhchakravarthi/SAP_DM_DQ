"""Centralized configuration.

Non-secret defaults live in ``config.yaml`` at the project root (single
source of truth, safe to commit). Environment variables - loaded from a
local, gitignored ``.env`` file - override individual values and are the
only source for secrets (API keys, tokens) and machine-specific values
(e.g. a local GGUF model path).

All relative paths (log dir, SQLite DB, memory store) are resolved against
PROJECT_ROOT rather than the process's current working directory, so
behavior does not depend on which directory a script happens to be
launched from.
"""

import os
import warnings
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_FILE = Path(os.environ.get("EXPLORER_CONFIG_FILE", PROJECT_ROOT / "config.yaml"))


def _load_yaml_config(path: Path) -> dict:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


_yaml_config = _load_yaml_config(_CONFIG_FILE)


def _load_env_file() -> Optional[Path]:
    """Load secrets from the .env file named by ``env_file`` in config.yaml.

    The file lives outside the repository (default: ``~/.env``, i.e. the user's
    home directory on Linux, macOS and Windows alike). ``~`` and ``%VARS%`` /
    ``$VARS`` are expanded. EXPLORER_ENV_FILE overrides the yaml value. Values
    already present in the real environment are never overridden. If the
    configured file is missing, a legacy ``<project>/.env`` is used when present.
    Returns the file that was loaded, or None.
    """
    configured = os.environ.get("EXPLORER_ENV_FILE") or (_yaml_config.get("env_file") or "~/.env")
    path = Path(os.path.expandvars(str(configured))).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    if path.is_file():
        load_dotenv(path)
        return path
    legacy = PROJECT_ROOT / ".env"
    if legacy.is_file():
        warnings.warn(
            f"env_file {path} not found; falling back to {legacy}. "
            "Move it to the configured location so secrets stay outside the repository.",
            stacklevel=2,
        )
        load_dotenv(legacy)
        return legacy
    warnings.warn(f"env_file {path} not found; relying on the process environment for secrets.", stacklevel=2)
    return None


ENV_FILE_LOADED = _load_env_file()


def _get(dotted_path: str, default: Any = None) -> Any:
    """Look up a dotted path (e.g. 'llm.gemini.model') in config.yaml."""
    node: Any = _yaml_config
    for part in dotted_path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return default if node is None else node


def _env_str(name: str, default: Any) -> Any:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    return int(val) if val is not None else default


def _env_float(name: str, default: float) -> float:
    val = os.environ.get(name)
    return float(val) if val is not None else default


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    return val.strip().lower() == "true" if val is not None else bool(default)


def _env_optional_float(name: str, default: Any) -> Optional[float]:
    """Like _env_float, but None (yaml null / empty env var) means 'provider default'."""
    val = os.environ.get(name)
    if val is not None:
        return float(val) if val.strip() else None
    return None if default is None else float(default)


def _env_list(name: str, default: Any) -> list:
    """Comma-separated env var, else a yaml list (or single scalar)."""
    val = os.environ.get(name)
    if val is not None:
        return [item.strip() for item in val.split(",") if item.strip()]
    if default is None:
        return []
    return list(default) if isinstance(default, (list, tuple)) else [default]


def _resolve_path(value: str) -> str:
    """Anchor a relative path to PROJECT_ROOT; leave absolute paths as-is."""
    p = Path(value)
    return str(p if p.is_absolute() else PROJECT_ROOT / p)


class Config:
    """Application configuration - class attributes for simple call-site access."""

    PROJECT_ROOT = PROJECT_ROOT

    # LLM provider selection (see explorer_agent/llm_providers.py:build_llms).
    # Exactly one backend, no fallback chain: "google" (Gemini) or "local" (GGUF).
    SUPPORTED_LLM_PROVIDERS = ("google", "local")
    LLM_PROVIDER = _env_str("EXPLORER_LLM_PROVIDER", _get("llm.provider", "google"))
    # Retries AFTER the first attempt, handled by the provider's SDK (with backoff).
    LLM_MAX_RETRIES = _env_int("EXPLORER_LLM_MAX_RETRIES", _get("llm.max_retries", 2))
    LLM_TIMEOUT_SECONDS = _env_optional_float("EXPLORER_LLM_TIMEOUT", _get("llm.timeout_seconds", 120))

    # Gemini settings. Temperature None = model default (some Gemini models,
    # e.g. gemini-3.6-flash, use fixed sampling and ignore/warn on temperature).
    GEMINI_MODEL = _env_str("GEMINI_MODEL", _get("llm.gemini.model", "gemini-2.5-flash"))
    GEMINI_TEMPERATURE = _env_optional_float("GEMINI_TEMPERATURE", _get("llm.gemini.temperature"))
    GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")  # secret - .env only, no yaml default

    # Extra attempts on the SAME model when it returns malformed/missing
    # structured output (invalid JSON, skipped tool call) - these failures are
    # often random, unlike outages.
    LLM_STRUCTURED_OUTPUT_RETRIES = _env_int(
        "EXPLORER_LLM_STRUCTURED_OUTPUT_RETRIES", _get("llm.structured_output_retries", 1)
    )

    # Local GGUF LLM settings. Only ever read/loaded when LLM_PROVIDER=="local".
    # "~" and %VAR% are expanded, so the yaml value can be portable across machines.
    LOCAL_LLM_MODEL_PATH: Optional[str] = (
        os.path.expanduser(os.path.expandvars(_p))
        if (_p := _env_str("EXPLORER_LOCAL_LLM_MODEL_PATH", _get("llm.local.model_path")))
        else None
    )
    LOCAL_LLM_N_CTX = _env_int("EXPLORER_LOCAL_LLM_N_CTX", _get("llm.local.n_ctx", 4096))

    # Data settings. Tables are not configured here: every CSV in a run's data
    # folder (except the dictionary) is a table - see data_loader.discover_table_files.
    DATA_DICTIONARY_FILE = _env_str(
        "EXPLORER_DICTIONARY_FILE", _get("data.dictionary_file", "Data_Dictionary.csv")
    )
    # Per-client uploaded data (review app page 1) - see explorer_agent/client_workspace.py.
    CLIENT_DATA_DIR = _resolve_path(_env_str("EXPLORER_CLIENT_DATA_DIR", _get("data.client_data_dir", "client_data")))
    MAX_UPLOAD_MB = _env_int("EXPLORER_MAX_UPLOAD_MB", _get("data.max_upload_mb", 200))
    # One-line business meaning per table, for review_app's hover tooltips.
    SAP_TABLE_DESCRIPTIONS = _get("data.table_descriptions", {})

    # Batch profiling settings
    PROFILING_TOP_N_FREQUENT = _env_int("EXPLORER_PROFILE_TOP_N", _get("profiling.top_n_frequent", 10))
    PROFILING_SAMPLE_ROWS = _env_int("EXPLORER_PROFILE_SAMPLE_ROWS", _get("profiling.sample_rows", 50000))
    PROFILING_SAMPLE_SEED = int(_get("profiling.sample_seed", 42))
    MAX_TOTAL_CHECKS_PER_TABLE = _env_int(
        "EXPLORER_MAX_CHECKS_PER_TABLE", _get("profiling.max_checks_per_table", 25)
    )

    # Legacy explorer-loop setting. Retained for compatibility with callers
    # that still use the pre-batch-profiling implementation.
    MAX_ITERATIONS_PER_COLUMN = _env_int(
        "EXPLORER_MAX_ITERATIONS_PER_COLUMN", _get("profiling.max_iterations_per_column", 5)
    )

    # Deterministic duplicate matching (explorer_agent/duplicate_detector.py).
    # The matching itself never calls an LLM; the rules it executes are drafted
    # by the LLM once per client+schema and then reused from memory
    # (duplicate_rule_planner / memory.duplicate_rule_store). duplicates.tables
    # pins a table's rules by hand and skips both.
    DUPLICATES_ENABLED = _env_bool("EXPLORER_DUPLICATES_ENABLED", _get("duplicates.enabled", True))
    DUPLICATE_TABLE_RULES = _get("duplicates.tables", {})
    # Masked example values per column sent with the rule-drafting prompt
    # (0 = none). mask_value() keeps only the last two characters.
    DUPLICATE_RULE_SAMPLE_VALUES = _env_int(
        "EXPLORER_DUPLICATE_RULE_SAMPLES", _get("duplicates.rules.sample_values", 3)
    )
    # A single column the LLM called an IDENTIFIER is rejected when fewer than
    # this share of its filled values are distinct - such a value covers many
    # records, so it cannot identify one (see duplicate_rule_planner).
    DUPLICATE_RULE_MIN_IDENTIFIER_DISTINCT = _env_float(
        "EXPLORER_DUPLICATE_RULE_MIN_DISTINCT", _get("duplicates.rules.min_identifier_distinct", 0.5)
    )
    # Reuse rules the LLM scoped UNIVERSAL/INDUSTRY_SPECIFIC at OTHER clients.
    # Off by default: the same (table, column) pair can mean different things at
    # different companies (repurposed and custom fields).
    DUPLICATE_RULES_REUSE_ACROSS_CLIENTS = _env_bool(
        "EXPLORER_DUPLICATE_RULES_SHARE", _get("duplicates.rules.reuse_across_clients", False)
    )
    DUPLICATE_FUZZY_NAME_THRESHOLD = _env_float(
        "EXPLORER_DUPLICATE_FUZZY_THRESHOLD", _get("duplicates.fuzzy_name_threshold", 85)
    )
    DUPLICATE_MAX_IDENTIFIER_SHARE = _env_int(
        "EXPLORER_DUPLICATE_MAX_IDENTIFIER_SHARE", _get("duplicates.max_identifier_share", 5)
    )
    DUPLICATE_MAX_BLOCK_SIZE = _env_int("EXPLORER_DUPLICATE_MAX_BLOCK", _get("duplicates.max_block_size", 500))
    DUPLICATE_MAX_GROUPS = _env_int("EXPLORER_DUPLICATE_MAX_GROUPS", _get("duplicates.max_groups", 500))
    DUPLICATE_HIDE_DECIDED_GROUPS = _env_bool(
        "EXPLORER_DUPLICATE_HIDE_DECIDED", _get("duplicates.hide_decided_groups", True))

    # Composite DQ scorecard (explorer_agent/scorecard.py).
    SCORECARD_WEIGHTS = {k: float(v) for k, v in (_get("scorecard.weights", None) or {
        "completeness": 0.35, "correctness": 0.35, "uniqueness": 0.30, "activeness": 0.0}).items()}

    READINESS_MAX_WORKLIST = _env_int("EXPLORER_READINESS_MAX_WORKLIST", _get("scorecard.max_worklist", 5000))
    READINESS_BANDS = {k: float(v) for k, v in (_get("scorecard.readiness_bands", None) or
                                                {"good": 0.95, "fair": 0.80}).items()}
    SCORECARD_BANDS = {k: float(v) for k, v in (_get("scorecard.bands", None) or {"good": 0.95, "fair": 0.85}).items()}

    # Handoff to / from the neighbouring agents (explorer_agent/contracts.py).
    HANDOFF_DIR = _resolve_path(_env_str("EXPLORER_HANDOFF_DIR", _get("handoff.dir", "handoff")))
    HANDOFF_MAX_DOMAIN_VALUES = _env_int("EXPLORER_HANDOFF_MAX_DOMAIN", _get("handoff.max_domain_values", 200))
    HANDOFF_MAPPING_FILE = _env_str("EXPLORER_HANDOFF_MAPPING_FILE", _get("handoff.mapping_file", "field_mapping.json"))
    HANDOFF_MIN_CONFIDENCE = _env_float("EXPLORER_HANDOFF_MIN_CONFIDENCE", _get("handoff.min_confidence", 90))
    HANDOFF_TARGET_DOMAINS_FILE = _env_str("EXPLORER_HANDOFF_TARGET_DOMAINS_FILE",
                                           _get("handoff.target_domains_file", "target_domains.json"))

    # Survivorship (explorer_agent/survivorship.py): record quality score per duplicate
    # group member and a recommended survivor - a suggestion, never a verdict.
    SURVIVORSHIP_ENABLED = _env_bool("EXPLORER_SURVIVORSHIP_ENABLED", _get("survivorship.enabled", True))
    SURVIVORSHIP_WEIGHTS = {
        k: float(v) for k, v in (_get("survivorship.weights", None) or
                                 {"completeness": 0.4, "active": 0.25, "usage": 0.2, "recency": 0.15}).items()}
    SURVIVORSHIP_RECOMMEND_MATCH_TYPES = _env_list(
        "EXPLORER_SURVIVORSHIP_MATCH_TYPES", _get("survivorship.recommend_for", ["EXACT", "PROBABLE"]))

    # Deterministic SAP domain rules (explorer_agent/sap_rules.py) - zero LLM calls.
    SAP_RULES_ENABLED = _env_bool("EXPLORER_SAP_RULES_ENABLED", _get("sap_rules.enabled", True))
    SAP_RULES_PACK_FILE = _resolve_path(_env_str(
        "EXPLORER_SAP_RULES_PACK", _get("sap_rules.pack_file", "explorer_agent/rule_packs/sap_master_data.yaml")
    ))
    SAP_RULES_MAX_ROWS = _env_int("EXPLORER_SAP_RULES_MAX_ROWS", _get("sap_rules.max_rows_per_finding", 1000))
    SAP_RULES_DISABLED = _env_list("EXPLORER_SAP_RULES_DISABLED", _get("sap_rules.disabled_rules", []))
    SAP_RULES_CLIENT_OVERRIDES = _get("sap_rules.client_overrides", {}) or {}

    # Statistical and formatting anomalies (explorer_agent/anomaly_rules.py) - zero LLM calls.
    ANOMALIES_ENABLED = _env_bool("EXPLORER_ANOMALIES_ENABLED", _get("anomalies.enabled", True))
    ANOMALY_OUTLIER_IQR_MULTIPLIER = _env_float(
        "EXPLORER_ANOMALY_IQR_MULTIPLIER", _get("anomalies.outlier_iqr_multiplier", 3.0))
    ANOMALY_OUTLIER_MIN_FENCE_DECADES = _env_float(
        "EXPLORER_ANOMALY_MIN_FENCE_DECADES", _get("anomalies.outlier_min_fence_decades", 1.0))
    ANOMALY_OUTLIER_FLAG_LOW = _env_bool("EXPLORER_ANOMALY_FLAG_LOW", _get("anomalies.outlier_flag_low", False))
    ANOMALY_OUTLIER_MIN_SAMPLES = _env_int(
        "EXPLORER_ANOMALY_MIN_SAMPLES", _get("anomalies.outlier_min_samples", 20))
    ANOMALY_RARE_CODE_MAX_COUNT = _env_int(
        "EXPLORER_ANOMALY_RARE_MAX_COUNT", _get("anomalies.rare_code_max_count", 2))
    ANOMALY_RARE_CODE_MAX_SHARE = _env_float(
        "EXPLORER_ANOMALY_RARE_MAX_SHARE", _get("anomalies.rare_code_max_share", 0.005))
    ANOMALY_RARE_CODE_MAX_DISTINCT = _env_int(
        "EXPLORER_ANOMALY_RARE_MAX_DISTINCT", _get("anomalies.rare_code_max_distinct", 50))
    ANOMALY_RARE_CODE_MIN_TYPICAL_COUNT = _env_int(
        "EXPLORER_ANOMALY_RARE_MIN_TYPICAL", _get("anomalies.rare_code_min_typical_count", 10))
    ANOMALY_RARE_CODE_MIN_ROWS = _env_int(
        "EXPLORER_ANOMALY_RARE_MIN_ROWS", _get("anomalies.rare_code_min_rows", 50))

    # Sandbox settings
    SANDBOX_TIMEOUT_SECONDS = _env_int("EXPLORER_SANDBOX_TIMEOUT", _get("sandbox.timeout_seconds", 10))
    SANDBOX_MEM_LIMIT_MB = _env_int("EXPLORER_SANDBOX_MEM_MB", _get("sandbox.mem_limit_mb", 512))
    SANDBOX_LOAD_TIMEOUT_SECONDS = _env_int("EXPLORER_SANDBOX_LOAD_TIMEOUT",
                                            _get("sandbox.load_timeout_seconds", 120))

    # Privacy guardrail
    MAX_RESULT_LIST_LEN = _env_int(
        "EXPLORER_MAX_RESULT_LIST_LEN", _get("privacy.max_result_list_len", 20)
    )

    # Memory and retrieval settings
    VECTOR_BACKEND = _env_str("EXPLORER_VECTOR_BACKEND", _get("memory.vector_backend", "chroma"))
    MEMORY_BASE_DIR = _resolve_path(
        _env_str("EXPLORER_MEMORY_DIR", _get("memory.base_dir", "memory_store"))
    )
    CHROMA_COLLECTION_NAME = _env_str(
        "EXPLORER_CHROMA_COLLECTION", _get("memory.chroma_collection", "procedural_skills")
    )
    RETRIEVAL_TOP_K = _env_int("EXPLORER_RETRIEVAL_TOP_K", _get("memory.retrieval_top_k", 3))
    # Per-client knowledge (explorer_agent/client_knowledge.py), e.g. remembered duplicate decisions.
    CLIENT_KNOWLEDGE_DIR = _resolve_path(
        _env_str("EXPLORER_CLIENTS_DIR", _get("memory.clients_dir", "memory_store/clients"))
    )
    DEDUP_DISTANCE_THRESHOLD = _env_float(
        "EXPLORER_DEDUP_THRESHOLD", _get("memory.dedup_distance_threshold", 0.25)
    )
    # Local all-MiniLM-L6-v2 ONNX embeddings (see memory/chroma_store.py).
    # Resolution order: model_dir -> kaggle_handle -> Chroma's cache/download.
    EMBEDDING_MODEL_DIR: Optional[str] = _env_str(
        "EXPLORER_EMBEDDING_MODEL_DIR", _get("memory.embedding.model_dir")
    )
    EMBEDDING_KAGGLE_HANDLE: Optional[str] = _env_str(
        "EXPLORER_EMBEDDING_KAGGLE_HANDLE", _get("memory.embedding.kaggle_handle")
    )
    EMBEDDING_KAGGLE_TYPE = _env_str(
        "EXPLORER_EMBEDDING_KAGGLE_TYPE", _get("memory.embedding.kaggle_type", "dataset")
    )
    EMBEDDING_ALLOW_DOWNLOAD = _env_bool(
        "EXPLORER_EMBEDDING_ALLOW_DOWNLOAD", _get("memory.embedding.allow_download", True)
    )

    # Storage - anchored to PROJECT_ROOT regardless of current working directory
    EPISODIC_DB_PATH = _resolve_path(
        _env_str("EXPLORER_EPISODIC_DB", _get("storage.episodic_db_path", "episodic_memory.db"))
    )

    # Logging - anchored to PROJECT_ROOT regardless of current working directory
    LOG_LEVEL = _env_str("EXPLORER_LOG_LEVEL", _get("logging.level", "INFO"))
    LOG_DIR = _resolve_path(_env_str("EXPLORER_LOG_DIR", _get("logging.dir", "logs")))

    # Cache fast-path settings
    ENABLE_CACHE_FAST_PATH = _env_bool("EXPLORER_ENABLE_CACHE", _get("cache.enable_fast_path", True))
    SKIP_REFLECTION_ON_CACHE_HIT = _env_bool(
        "EXPLORER_SKIP_REFLECTION_ON_CACHE", _get("cache.skip_reflection_on_cache_hit", False)
    )

    # Review app
    REVIEW_APP_CORS_ORIGINS = _get("review_app.cors_origins", ["*"])

    @classmethod
    def provider_missing_settings(cls, provider: str) -> list:
        """Names of required settings that are unset for `provider` (empty = usable)."""
        if provider == "google":
            return [] if cls.GEMINI_API_KEY else ["GEMINI_API_KEY (.env)"]
        if provider == "local":
            return [] if cls.LOCAL_LLM_MODEL_PATH else [
                "llm.local.model_path (config.yaml) or EXPLORER_LOCAL_LLM_MODEL_PATH (.env)"
            ]
        raise EnvironmentError(
            f"Unknown LLM provider {provider!r} in config.yaml "
            f"(expected one of {', '.join(cls.SUPPORTED_LLM_PROVIDERS)})."
        )

    @classmethod
    def validate(cls) -> None:
        """Fail early when the configured LLM provider lacks required settings."""
        missing = cls.provider_missing_settings(cls.LLM_PROVIDER)

        if missing:
            raise EnvironmentError(
                f"Missing required configuration: {', '.join(missing)}. "
                "Set them in config.yaml (non-secret settings) or in the .env "
                "file named by env_file in config.yaml (secrets)."
            )
