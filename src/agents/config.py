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

# This file is <project>/src/agents/config.py, so the repo root is three levels up.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
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

    # LLM provider selection (see src/agents/llm/llm_providers.py:build_llms).
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
    # Per-client uploaded data (review app page 1) - see src/agents/data_loader/client_workspace.py.
    CLIENT_DATA_DIR = _resolve_path(_env_str("EXPLORER_CLIENT_DATA_DIR", _get("data.client_data_dir", "data/clients")))
    # Answer keys live outside CLIENT_DATA_DIR on purpose: discover_table_files treats every CSV
    # under a client folder as a table, so a key stored there would be profiled and sent to the LLM.
    ANSWER_KEY_DIR = _resolve_path(_env_str("EXPLORER_ANSWER_KEY_DIR", _get("data.answer_key_dir", "data/answer_keys")))
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

    # "Why was this record flagged": the exact explanation is always available; the plain-language one uses the
    # LOCAL model and is the one place record values reach an LLM (explain.py). Off by default.
    EXPLAIN_LOCAL_LLM_ENABLED = _env_bool("EXPLORER_EXPLAIN_LOCAL_LLM", _get("explain.local_llm.enabled", False))
    # Local-model audit of small, ambiguous slices (src/agents/llm/local_auditor.py): borderline duplicate
    # pairs and free-text street strings. Off by default; never more than max_records records per process.
    LOCAL_AUDIT_ENABLED = _env_bool("EXPLORER_LOCAL_AUDIT", _get("local_audit.enabled", False))
    LOCAL_AUDIT_MAX_RECORDS = _env_int("EXPLORER_LOCAL_AUDIT_MAX_RECORDS", _get("local_audit.max_records_per_run", 50))
    LOCAL_AUDIT_MAX_TOKENS = _env_int("EXPLORER_LOCAL_AUDIT_MAX_TOKENS", _get("local_audit.max_tokens", 200))
    EXPLAIN_MAX_TOKENS =_env_int("EXPLORER_EXPLAIN_MAX_TOKENS", _get("explain.local_llm.max_tokens", 220))

    # Bounded repair loop (graph.py, repair.py): rounds in which planner checks that failed to run are
    # sent back to the planner once, together. 0 = off. Each round is at most ONE extra LLM call per table.
    MAX_REPAIR_ROUNDS = _env_int("EXPLORER_MAX_REPAIR_ROUNDS", _get("profiling.max_repair_rounds", 1))
    SKIP_KNOWN_FINDINGS = _env_bool("EXPLORER_SKIP_KNOWN_FINDINGS", _get("profiling.skip_known_findings", True))

    # Deterministic duplicate matching (src/agents/engines/duplicate_detector.py).
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
    DUPLICATE_RULES_FROM_MAPPING = _env_bool(
        "EXPLORER_DUPLICATE_RULES_FROM_MAPPING", _get("duplicates.rules.from_mapping", True)
    )
    # Local embedding model as a last look at borderline name pairs (same location block, fuzzy
    # similarity between min_fuzzy and fuzzy_name_threshold). Free and offline, but it is a model:
    # it only ever yields SIMILAR (a look-alike for the reviewer), never EXACT/PROBABLE.
    DUPLICATE_SEMANTIC_ENABLED = _env_bool("EXPLORER_DUPLICATE_SEMANTIC", _get("duplicates.semantic.enabled", True))
    DUPLICATE_SEMANTIC_MIN_FUZZY = _env_float("EXPLORER_DUPLICATE_SEMANTIC_MIN_FUZZY",
                                              _get("duplicates.semantic.min_fuzzy", 70))
    DUPLICATE_SEMANTIC_MIN_COSINE = _env_float("EXPLORER_DUPLICATE_SEMANTIC_MIN_COSINE",
                                               _get("duplicates.semantic.min_cosine", 0.85))
    DUPLICATE_SEMANTIC_MAX_PAIRS = _env_int("EXPLORER_DUPLICATE_SEMANTIC_MAX_PAIRS",
                                            _get("duplicates.semantic.max_pairs", 2000))
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

    # Composite DQ scorecard (src/agents/engines/scorecard.py).
    SCORECARD_WEIGHTS = {k: float(v) for k, v in (_get("scorecard.weights", None) or {
        "completeness": 0.35, "correctness": 0.35, "uniqueness": 0.30, "activeness": 0.0}).items()}

    READINESS_MAX_WORKLIST = _env_int("EXPLORER_READINESS_MAX_WORKLIST", _get("scorecard.max_worklist", 5000))
    READINESS_BANDS = {k: float(v) for k, v in (_get("scorecard.readiness_bands", None) or
                                                {"good": 0.95, "fair": 0.80}).items()}
    SCORECARD_BANDS = {k: float(v) for k, v in (_get("scorecard.bands", None) or {"good": 0.95, "fair": 0.85}).items()}

    # Handoff to / from the neighbouring agents (src/agents/contracts.py).
    HANDOFF_DIR = _resolve_path(_env_str("EXPLORER_HANDOFF_DIR", _get("handoff.dir", "storage/perm/handoff")))
    HANDOFF_MAX_DOMAIN_VALUES = _env_int("EXPLORER_HANDOFF_MAX_DOMAIN", _get("handoff.max_domain_values", 200))
    HANDOFF_MAPPING_FILE = _env_str("EXPLORER_HANDOFF_MAPPING_FILE", _get("handoff.mapping_file", "field_mapping.json"))
    HANDOFF_MIN_CONFIDENCE = _env_float("EXPLORER_HANDOFF_MIN_CONFIDENCE", _get("handoff.min_confidence", 90))
    HANDOFF_TARGET_DOMAINS_FILE = _env_str("EXPLORER_HANDOFF_TARGET_DOMAINS_FILE",
                                           _get("handoff.target_domains_file", "target_domains.json"))

    # Survivorship (src/agents/engines/survivorship.py): record quality score per duplicate
    # group member and a recommended survivor - a suggestion, never a verdict.
    SURVIVORSHIP_ENABLED = _env_bool("EXPLORER_SURVIVORSHIP_ENABLED", _get("survivorship.enabled", True))
    SURVIVORSHIP_WEIGHTS = {
        k: float(v) for k, v in (_get("survivorship.weights", None) or
                                 {"completeness": 0.4, "active": 0.25, "usage": 0.2, "recency": 0.15}).items()}
    SURVIVORSHIP_RECOMMEND_MATCH_TYPES = _env_list(
        "EXPLORER_SURVIVORSHIP_MATCH_TYPES", _get("survivorship.recommend_for", ["EXACT", "PROBABLE"]))

    # Deterministic SAP domain rules (src/agents/engines/sap_rules.py) - zero LLM calls.
    SAP_RULES_ENABLED = _env_bool("EXPLORER_SAP_RULES_ENABLED", _get("sap_rules.enabled", True))
    SAP_RULES_PACK_FILE = _resolve_path(_env_str(
        "EXPLORER_SAP_RULES_PACK", _get("sap_rules.pack_file", "rules/packs/sap_master_data.yaml")
    ))
    SAP_RULES_MAX_ROWS = _env_int("EXPLORER_SAP_RULES_MAX_ROWS", _get("sap_rules.max_rows_per_finding", 1000))
    SAP_RULES_DISABLED = _env_list("EXPLORER_SAP_RULES_DISABLED", _get("sap_rules.disabled_rules", []))
    SAP_RULES_CLIENT_OVERRIDES = _get("sap_rules.client_overrides", {}) or {}
    # Check the rule pack's static reference domains (Incoterms, ...) like a Metadata Repository domain.
    SAP_RULES_REFERENCE_DOMAINS = _env_bool("EXPLORER_SAP_RULES_REFERENCE_DOMAINS",
                                            _get("sap_rules.reference_domains", True))

    # Proposing a blank City / Postal Code from the client's own verified records (enrichment.py).
    ENRICHMENT_ENABLED = _env_bool("EXPLORER_ENRICHMENT_ENABLED", _get("enrichment.enabled", True))
    ENRICHMENT_MIN_COOCCURRENCE = _env_float("EXPLORER_ENRICHMENT_MIN_COOCCURRENCE",
                                             _get("enrichment.min_cooccurrence", 0.9))
    ENRICHMENT_POSTAL_FROM_CITY = _env_bool("EXPLORER_ENRICHMENT_POSTAL_FROM_CITY",
                                            _get("enrichment.postal_from_city", False))
    ENRICHMENT_MIN_SUPPORT =_env_int("EXPLORER_ENRICHMENT_MIN_SUPPORT", _get("enrichment.min_support", 2))

    # Static offline reference data bundled with the agent (see data/reference/README.md).
    REFERENCE_DATA_DIR = _resolve_path(_env_str("EXPLORER_REFERENCE_DATA_DIR",
                                                _get("reference_data.dir", "data/reference")))
    GEO_POSTAL_DB = Path(REFERENCE_DATA_DIR) / "geo_postal.db"

    # Statistical and formatting anomalies (src/agents/engines/anomaly_rules.py) - zero LLM calls.
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
    # Human-readable rule state (skills, client knowledge, duplicate rules, column mappings).
    # <MEMORY_BASE_DIR>/procedural is the source of truth for promoted skills.
    MEMORY_BASE_DIR = _resolve_path(
        _env_str("EXPLORER_MEMORY_DIR", _get("memory.base_dir", "rules"))
    )
    # The vector index is derived from the procedural registry (memory.reindex rebuilds it), so it
    # is a separate folder from the source-of-truth rules/procedural/. It stays under storage/perm/
    # so that a rebuild (which needs the embedding model) is never forced on an offline server.
    CHROMA_DIR = _resolve_path(
        _env_str("EXPLORER_CHROMA_DIR", _get("memory.chroma_dir", "storage/perm/chroma"))
    )
    CHROMA_COLLECTION_NAME = _env_str(
        "EXPLORER_CHROMA_COLLECTION", _get("memory.chroma_collection", "procedural_skills")
    )
    RETRIEVAL_TOP_K = _env_int("EXPLORER_RETRIEVAL_TOP_K", _get("memory.retrieval_top_k", 3))
    # Per-client knowledge (src/agents/memory/client_knowledge.py), e.g. remembered duplicate decisions.
    CLIENT_KNOWLEDGE_DIR = _resolve_path(
        _env_str("EXPLORER_CLIENTS_DIR", _get("memory.clients_dir", "rules/local/clients"))
    )
    # JSON Schemas of the handoff contracts, written by `python -m src.agents.contracts`.
    SCHEMA_DIR = _resolve_path(_env_str("EXPLORER_SCHEMA_DIR", _get("memory.schema_dir", "rules/schemas")))
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
        _env_str("EXPLORER_EPISODIC_DB", _get("storage.episodic_db_path", "storage/perm/episodic_memory.db"))
    )

    # Logging - anchored to PROJECT_ROOT regardless of current working directory
    LOG_LEVEL = _env_str("EXPLORER_LOG_LEVEL", _get("logging.level", "INFO"))
    LOG_DIR = _resolve_path(_env_str("EXPLORER_LOG_DIR", _get("logging.dir", "storage/tmp/logs")))

    # Cache fast-path settings
    ENABLE_CACHE_FAST_PATH = _env_bool("EXPLORER_ENABLE_CACHE", _get("cache.enable_fast_path", True))
    SKIP_REFLECTION_ON_CACHE_HIT = _env_bool(
        "EXPLORER_SKIP_REFLECTION_ON_CACHE", _get("cache.skip_reflection_on_cache_hit", False)
    )
    # Hybrid skill reuse: exact table+column first, else a skill matched by meaning (see config.yaml).
    SIMILARITY_REUSE = _env_bool("EXPLORER_SIMILARITY_REUSE", _get("cache.similarity_reuse", True))
    SIMILARITY_MIN_SCORE = _env_float("EXPLORER_SIMILARITY_MIN_SCORE", _get("cache.similarity_min_score", 0.45))
    SIMILARITY_MIN_MARGIN = _env_float("EXPLORER_SIMILARITY_MIN_MARGIN", _get("cache.similarity_min_margin", 0.10))
    SIMILARITY_MAX_PER_TABLE = _env_int("EXPLORER_SIMILARITY_MAX_PER_TABLE", _get("cache.similarity_max_per_table", 8))
    SIMILARITY_MAX_FLAG_RATIO = _env_float("EXPLORER_SIMILARITY_MAX_FLAG_RATIO",
                                           _get("cache.similarity_max_flag_ratio", 0.5))

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


def _migrate_legacy_storage() -> None:
    """Move state left in the project root by older versions into storage/ (once, before anything
    opens the database or a log file - which is why it runs here, at the end of the first import
    of Config). Idempotent and non-destructive; see storage_layout.py."""
    if _env_bool("EXPLORER_SKIP_STORAGE_MIGRATION", False):
        return
    from src.agents.tools.storage_layout import migrate_legacy_layout
    migrate_legacy_layout(
        PROJECT_ROOT, chroma_dir=Path(Config.CHROMA_DIR), memory_dir=Path(Config.MEMORY_BASE_DIR),
        client_data_dir=Path(Config.CLIENT_DATA_DIR), handoff_dir=Path(Config.HANDOFF_DIR),
        log_dir=Path(Config.LOG_DIR), episodic_db=Path(Config.EPISODIC_DB_PATH))


_migrate_legacy_storage()
