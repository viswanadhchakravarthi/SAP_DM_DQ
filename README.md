
# SAP_DM_DQ

Agentic SAP Data Migration and Data Quality (DQ) Profiler equipped with modular episodic, procedural, and semantic memory layers.

---

## Overview

`SAP_DM_DQ` is an intelligent, memory-augmented exploration agent built for SAP data migration and quality assessment workflows. The agent shifts from basic pandas-style table profiling into persistent, reusable data check generation.

**Key Features:**
* **LangGraph Exploration:** Manages structured batch data profiling workflows.
* **Tri-Tier Memory System:**
  * **Episodic Store (SQLite):** Tracks execution history, individual run logs, and pending validation items.
  * **Procedural Registry (JSON/Filesystem):** Serves as the human-readable, auditable source of truth for promoted check skills.
  * **Semantic Memory (ChromaDB Vector Store):** Enables fuzzy/semantic retrieval of relevant historical checks for prompt injection during profiling.
* **Gated Promotion Pipeline:** Near-duplicate check detection and human-in-the-loop approval before promoting findings into reusable skills.
* **Isolated Sandbox Execution:** Runs agent-generated data checks safely using multiprocessing isolation.
* **Privacy & PII Protection:** Heuristic-based result scrubbing (`privacy_guard.py`) and strict column allow-listing (`ydata-profiling`).

---

## Prerequisites

* **Python**: 3.13+
* **OS**: Windows / Linux / macOS
* **Virtual Environment**: `venv` or `conda`

---

## Installation

### 1. Clone Repository & Setup Virtual Environment

```bash
git clone <repository-url>
cd SAP_DM_DQ

# Create virtual environment
python -m venv .venv

# Activate virtual environment
# Windows (CMD / PowerShell):
.venv\Scripts\activate

# Linux / macOS:
source .venv/bin/activate


### 2. Install Core Dependencies

```bash
pip install -r requirements.txt

```

---

## Installing `llama-cpp-python` (Windows / Python 3.13)

Compiling `llama-cpp-python` on Windows typically requires Visual Studio C++ Build Tools (`MSVC`) and `CMake`. To bypass compilation errors and avoid installing heavy C++ toolchains on Python 3.13, install pre-compiled wheels.

### Workaround 1: Local Binary Wheel (Recommended)

1. Download the pre-compiled binary wheel matching your system architecture:
* **File:** `llama_cpp_python-0.3.35-py3-none-win_amd64.whl`


2. Place the file in a local path (e.g., `D:\Downloads\`).
3. Run the following command inside your active `.venv`:

```bash
pip install "D:\Downloads\llama_cpp_python-0.3.35-py3-none-win_amd64.whl"

```

### Workaround 2: Remote Wheel Index

If you do not have the local `.whl` file, pull pre-built wheels directly:

* **CPU Only:**
```bash
pip install llama-cpp-python==0.3.35 --extra-index-url [https://abetlen.github.io/llama-cpp-python/wheels/cpu](https://abetlen.github.io/llama-cpp-python/wheels/cpu)

```


* **CUDA Acceleration:**
```bash
pip install llama-cpp-python==0.3.35 --extra-index-url [https://abetlen.github.io/llama-cpp-python/wheels/cu121](https://abetlen.github.io/llama-cpp-python/wheels/cu121)

```



---

## Local Model Download (`kagglehub`)

To run inference locally using GGUF quantization formats without external APIs, download the preferred model weights via `kagglehub`.

### 1. Install `kagglehub`

```bash
pip install kagglehub

```

### 2. Download Model Weights (Python / Jupyter Notebook)

```python
import kagglehub

# Download latest GGUF version of Qwen2.5-Coder 3B Instruct
path = kagglehub.model_download("qwen-lm/qwen2.5-coder/gguf/3b-instruct")

print("Path to model files:", path)

```

---

## Configuration

Non-secret, shareable settings (LLM provider/model, dictionary and upload settings, sandbox
limits, memory/log paths, etc.) live in [`config.yaml`](config.yaml) at the
project root - edit that file to change defaults for everyone.

Secrets and machine-specific values (API keys, tokens, a local GGUF model
path) go in a local, gitignored `.env` file instead. Every `config.yaml`
setting can also be overridden per-machine by an environment variable - see
the comments in `config.yaml` and `src/agents/config.py` for the exact
variable names.

## Environment Setup

Create a `.env` file in your **home directory** (`C:\Users\<you>\.env` on Windows,
`~/.env` on Linux/macOS), outside the repository. The location is set by
`env_file` in `config.yaml` (`~` and `%VAR%` are expanded; `EXPLORER_ENV_FILE`
overrides it). If that file is missing, a legacy `<project>/.env` is used with a warning.

```env
GEMINI_API_KEY="your-gemini-key"
KAGGLE_API_TOKEN="kaggle-api-token"


```

The local GGUF model path is not a secret: set `llm.local.model_path` in
`config.yaml` (only used when `llm.provider` is `local`; the other option is `google`).

---

## Verification

Confirm that `llama-cpp-python` and its C++ bindings load correctly without DLL issues:

```bash
python -c "import llama_cpp; print(llama_cpp.__file__)"

```

*Expected Output:*

```text
D:\GitHub\SAP_DM_DQ\.venv\Lib\site-packages\llama_cpp\__init__.py

```

---

## Usage

```bash
# Profile a client's tables (--client is required; tables are every CSV under --data-dir)
python -m orchestrator.runner --client "<client name>" --data-dir <path>

# Client has no data dictionary: column meaning comes from names, statistics and the rule pack
python -m orchestrator.runner --client "<client name>" --data-dir <path> --no-dictionary

# Recreate findings that earlier runs already produced (by default they are skipped)
python -m orchestrator.runner --client "<client name>" --data-dir <path> --no-skip-known

# LLM-free engines only (duplicate matching + built-in SAP rules)
python -m orchestrator.runner --client "<client name>" --data-dir <path> --deterministic-only

# Human review app: page 1 picks the client and uploads its data, page 2 reviews findings
uvicorn review_app.main:app --reload
```

* **Data dictionary is optional.** On page 1, tick "I don't have a data dictionary" to proceed with tables alone.
* **Re-runs don't repeat themselves.** A finding that is in progress or already decided by a human is not recreated unless the table file changed (`profiling.skip_known_findings`).
* **Skills are reused by meaning.** A promoted skill first applies to the exact table + column it was learned on; otherwise it can be matched to a differently named column by its meaning, using the local embedding model (`cache.similarity_*`). Adapted code is pre-flight checked and still goes through human review.

---

## Module Index

Four top-level concerns: **where the orchestrator lives**, **where the rules sit**, **where the
datasets belong**, and the agent's domain logic. See `implementation.md` for the full layout.

* `orchestrator/` — the execution subsystem (CLI: `python -m orchestrator.runner`)
  * `runner.py` — entry point; discovers tables, resolves mappings, runs the deterministic engines, invokes the graph. `graph.py` — LangGraph plan → execute → repair → reflect flow.
  * `sandbox.py`, `check_executor.py` — isolated execution of LLM-generated checks; `preflight.py` — free static checks that reject code that can't run; `repair.py` — the bounded loop that sends failed checks back to the planner once.
  * `cache_runner.py` — re-runs promoted skills instead of calling the planner.
  * `job_manager.py` — runs the orchestrator as a child process for the review app.
* `rules/` — version-controlled, human-auditable rule state
  * `packs/sap_master_data.yaml` — the deterministic SAP standards, ISO lists and tax/IBAN formats.
  * `local/clients/<client_id>/` — per-client `column_mappings.json` (the manual correction path), `duplicate_rules.json`, `duplicate_decisions.json`, `client.json`. `local/industries/` — industry baselines.
  * `procedural/` — `skill_registry.json` (source of truth for promoted checks) and the shared duplicate rules.
  * `schemas/` — JSON Schemas and examples for the pipeline handoff contracts.
* `data/` — datasets only, no databases: `clients/<client_id>/` (uploaded CSVs, `workspace.json`; gitignored), `answer_keys/` (ground truth, deliberately outside the table-discovery path), `reference/` (offline GeoNames postal DB).
* `src/agents/` — the agent's domain logic
  * `config.py` + `config.yaml` — all settings; secrets come from the `.env` named by `env_file`. `schemas.py`, `contracts.py`, `events.py`, `metrics.py`, `logging_config.py`.
  * `engines/` — `sap_rules.py`, `anomaly_rules.py`, `rule_context.py` (deterministic rule engines); `column_mapping.py` (column meaning); `duplicate_detector.py`, `duplicate_rule_planner.py`, `duplicate_rules.py` (matching and its per-client rules); `survivorship.py`, `scorecard.py` (golden record and DQ index); `enrichment.py`, `checksums.py`, `geo_reference.py`.
  * `memory/` — `episodic_store.py` (SQLite run/finding history and the human review gate), `client_knowledge.py` (per-client durable memory), `skill_registry.py` (procedural source of truth), `base.py` (MemoryStore interface), `chroma_store.py` (Chroma adapter), `__init__.py` (backend factory `get_memory_store`), `retriever.py`, `promotion.py` (episodic → procedural → semantic), `reindex.py`, `duplicate_rule_store.py`.
  * `profilers/` — `table_profiler.py` (allowlisted statistical profile), `structural_profile.py` (the OUT handoff document), `profiler_primitives.py`, `privacy_guard.py` (heuristic scrubbing before any LLM egress).
  * `llm/` — `llm_providers.py` (builds the single LLM: Gemini or local GGUF), `local_llms.py` (the GGUF chat model), `llm_usage.py` (token counts per request), `local_auditor.py`.
  * `data_loader/` — `data_loader.py` (CSV ingestion and table discovery), `client_workspace.py` (uploaded data on disk).
  * `tools/` — `explain.py` ("Why flagged?"), `evaluate.py` (scores a run against an answer key), `skill_reuse.py` (reuse a skill by meaning), `storage_layout.py`, `build_geo_postal.py`.
* `review_app/` — FastAPI + vanilla JS review UI (`uvicorn review_app.main:app`).
* `storage/` — gitignored runtime artifacts only: the episodic SQLite DB, the derived vector index, the handoff outbox and logs.
