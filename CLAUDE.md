# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

`SAP_DM_DQ` is a memory-augmented agent for SAP data migration / data-quality profiling. It profiles tables statistically, has an LLM propose pandas checks, runs those checks locally in a sandbox, has an LLM judge the results, and lets a human approve/reject findings through a review UI before they are promoted into a reusable skill library.

Two independently runnable components:
- `explorer_agent/` — the LangGraph-based profiling/exploration pipeline (CLI).
- `review_app/` — a FastAPI + vanilla JS app for human review of findings and skill promotion.

## Commands

```bash
# Setup
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

# Run the explorer agent (profiles tables, generates findings)
python -m explorer_agent.main --data-dir <path> [--tables LFA1 LFB1] [--no-cache] [--llm-provider google|groq|local] [--fallback-providers groq ...]

# Rebuild the semantic (vector) index from the procedural registry (source of truth)
python -m explorer_agent.memory.reindex

# Promote human-approved findings into the skill registry from the CLI
python -m explorer_agent.memory.promotion [--run-id <id>] [--dedup-threshold <float>]

# Run the human review web app (serves review_app/static/ + JSON API)
uvicorn review_app.main:app --reload
```

There is no test suite, linter, or `pyproject.toml` configured in this repo currently.

### Configuration

`explorer_agent/config.py`'s `Config` class is the single place every module reads settings from. It layers two sources:
- **`config.yaml`** (project root) — non-secret, shareable defaults: LLM provider/model, the SAP table list (`data.tables`), sandbox limits, memory/log/DB paths, cache thresholds, etc. Committed to version control.
- **`.env`** (gitignored) — secrets (`GEMINI_API_KEY`, etc.) and machine-specific values (e.g. `EXPLORER_LOCAL_LLM_MODEL_PATH`). Any `config.yaml` value can also be overridden per-machine by the matching environment variable (see comments in both files for exact names).

All relative paths (`EPISODIC_DB_PATH`, `LOG_DIR`, `MEMORY_BASE_DIR`) are resolved against `Config.PROJECT_ROOT`, not the process's current working directory, so behavior doesn't depend on which directory a script is launched from.

### LLM providers and fallbacks (`explorer_agent/llm_providers.py`)

`Config.LLM_PROVIDER` (`llm.provider` / `--llm-provider`: `google`, `groq` or `local`) is the primary backend; `Config.LLM_FALLBACK_PROVIDERS` (`llm.fallback_providers` / `--fallback-providers`) are tried in order after it. `build_llms()` expands this into an ordered model chain (Groq contributes one candidate per entry in `llm.groq.models`) and returns an `LLMBundle` whose planner/reflector runnables are `first.with_fallbacks(rest)`. Transient errors are retried inside each provider SDK (`llm.max_retries`, kept low so overloads fail over fast); malformed/missing structured output is retried on the same model (`llm.structured_output_retries`); any remaining failure moves to the next model. When all fail, `LLMChainExhaustedError` is raised: `main.py` skips that table (exit code 1) and `cache_runner.py` falls back to its rule-based heuristic. `metrics.llm_call_failures` / `llm_fallback_calls` count this. Groq models are per-entry dicts in `llm.groq.models` (`structured_output_method`, `reasoning_effort`, `max_tokens`) — `json_schema` is far more reliable than tool-calling for the large `CheckPlan` schema. `Config.validate()` only requires credentials for the primary provider; a fallback missing its key is skipped with a warning. Callers (`graph.py`, `cache_runner.py`) just call `.invoke()` and are unaware of the chain.

The local model is loaded lazily (`llm_providers._get_local_llm()`) and only when `local` is in the chain; provider SDKs are imported per provider, so importing `explorer_agent.main` never pulls `llama_cpp` into memory.
## Architecture

### Tri-tier memory system

This is the central design concept and spans several files — understand it before changing any memory-related code:

1. **Episodic memory** (`explorer_agent/episodic_store.py`) — SQLite (`episodic_memory.db`). Every run and every finding (plus row-level `finding_items` detail) is logged here first, along with human review status (`PENDING`/`APPROVED`/`REJECTED`). This is the append-only history and the human-in-the-loop gate.
2. **Procedural memory** (`explorer_agent/memory/skill_registry.py`) — JSON registry (`memory_store/procedural/skill_registry.json`) + one `.py` file per skill. This is the **source of truth** for promoted, reusable checks — human-readable and auditable.
3. **Semantic memory** (`explorer_agent/memory/chroma_store.py`) — ChromaDB vector index, embedded locally with all-MiniLM-L6-v2 (Chroma's ONNX export via onnxruntime; no API calls, never Hugging Face — model files resolve from `memory.embedding.model_dir` → a Kaggle handle via kagglehub → Chroma's cache/S3 download). The collection's metadata records the embedding model; on mismatch it is dropped and `memory/__init__.py:get_memory_store()` rebuilds it from the registry. This is a **derived search index built from procedural memory**, never the other way around. If it's lost or corrupted, `explorer_agent/memory/reindex.py` rebuilds it entirely from the JSON registry.

Promotion (`explorer_agent/memory/promotion.py`) is the pipeline that moves data episodic → procedural → semantic. It only promotes findings that are (a) human-APPROVED via the review app, (b) marked `reusable` by the LLM's own judgment, and (c) have captured `check_code`. It also does near-duplicate detection against existing skills (vector distance threshold) before creating a new skill entry, to avoid redundant near-identical skills.

**Backend abstraction rule**: no module outside `explorer_agent/memory/chroma_store.py` may import `chromadb` directly. Everything goes through the `MemoryStore` ABC (`explorer_agent/memory/base.py`) so swapping vector backends means writing one new adapter class (see `explorer_agent/memory/__init__.py`'s `get_memory_store()` factory), not touching `promotion.py`/`retriever.py`/`main.py`.

`explorer_agent/memory/retriever.py` (`SkillRetriever`) is a separate layer on top of the generic `MemoryStore.search()` — it knows how to build a query from table/column/dtype/business-meaning and format results for prompt injection as *hints*, distinct from `skill_registry.get_skills_for_table_column()`'s exact-match lookup used by the cache fast path.

### Explorer graph (`explorer_agent/graph.py`)

A LangGraph `StateGraph` (linear, no cycles) run once per table via `explore_table()` in `main.py`:

1. `plan_batch` — ONE LLM call (`planner_structured`, `.with_structured_output(CheckPlan)`) proposes a batch of pandas checks for the whole table at once, from a privacy-sanitized statistical profile (never raw data).
2. `execute_all` — all proposed checks run locally in the sandbox, zero LLM involvement.
3. `reflect_batch` — ONE LLM call (`reflector_structured`, `.with_structured_output(ReflectionBatch)`) judges all check results at once (severity/confidence/reusability), then row-level `detail_rows` are extracted locally (via `detail_code`) only for CONFIRMED findings.
4. `finalize` → `human_review` (stub node; actual human review happens later in `review_app`).

This design intentionally costs exactly 2 LLM calls per table regardless of column count (contrast with an earlier per-column tool-calling design referenced in the module docstring).

Each `ProposedCheck` (`explorer_agent/schemas.py`) has two code fields: `code` (must set an AGGREGATE `result` — this is what gets sent to the reflector LLM) and optional `detail_code` (sets `detail_rows`, a list of offending rows for local human review only — **never sent to any LLM**).

### Cache fast path (`explorer_agent/cache_runner.py`)

Before invoking the planner for a column, `explore_table()` checks for existing approved skills for that exact table+column (`skill_registry.get_skills_for_table_column`). If found, the cached check code re-runs against fresh data and the planner LLM call is skipped entirely for that column (only the reflector may still run, unless `SKIP_REFLECTION_ON_CACHE_HIT` is set, in which case a rule-based heuristic replaces it). `explorer_agent/metrics.py` tracks LLM call counts and cache hit/miss counts per run to make these savings concrete.

### Privacy boundary

`explorer_agent/privacy_guard.py` (`sanitize_result_for_llm`) is the **last line of defense** before any locally-computed check result reaches an LLM: it withholds raw DataFrames/Series, masks string values (`profiler_primitives.mask_value`), and truncates long lists/strings. It is explicitly a best-effort heuristic, not a certified PII scanner. `explorer_agent/table_profiler.py` similarly uses an **allowlist** extraction from `ydata-profiling`'s raw JSON output — only explicitly named fields are pulled into the distilled profile sent to the planner LLM; anything not listed is dropped by default, since `value_counts_without_nan` etc. can contain raw values as dict keys even with `sensitive=True`.

`detail_code`/`detail_rows` output (row-level PII-bearing detail for confirmed findings) deliberately bypasses `privacy_guard` — it goes only to the local SQLite store (`finding_items` table) and the review UI, never to an LLM.

### Sandbox (`explorer_agent/sandbox.py`)

LLM-generated pandas code executes in a separate `multiprocessing` process (spawn context), with restricted builtins, a blocked-imports list (`os`, `sys`, `subprocess`, `socket`, etc.), and (Unix-only) CPU/memory rlimits plus a wall-clock timeout with forced termination. Documented as POC-grade isolation — "good enough to let an LLM experiment safely," not hardened against a determined adversary; harden further (containers/microVMs) before touching real client data.

### Review app (`review_app/`)

FastAPI app (`review_app/main.py`) serving a JSON API over `episodic_store` plus the promotion pipeline, with `review_app/static/` (vanilla HTML/CSS/JS) mounted last so `/api/*` routes take precedence. Key endpoints: `/api/runs`, `/api/findings`, `/api/findings/{id}/decision` (human approve/reject — the gate that `promotion.py` reads from), `/api/promote`, `/api/skills`, plus per-row endpoints (`/api/findings/{id}/items`, `/api/finding-items/{id}/decision`) for reviewing individual `detail_rows`.

### Local LLM support (`explorer_agent/local_llms.py`)

`QwenCoderGGUFChatModel` wraps a local GGUF model (via `llama-cpp-python`) as a LangChain `BaseChatModel`, emulating structured output/tool-calling through llama.cpp's grammar-constrained JSON decoding rather than prompt-based coaxing. It supports exactly one bound tool/schema at a time. Used when `local` appears in the provider chain built by `llm_providers.build_llms()`, alongside `ChatGoogleGenerativeAI` (Gemini) and `ChatGroq`. This module deliberately does NOT import `llama_cpp` at module level (it's imported unconditionally by `llm_providers.py`) — the `Llama` instance is constructed only inside `llm_providers._get_local_llm()`.
