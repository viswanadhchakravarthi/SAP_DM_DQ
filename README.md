
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

Non-secret, shareable settings (LLM provider/model, SAP table list, sandbox
limits, memory/log paths, etc.) live in [`config.yaml`](config.yaml) at the
project root - edit that file to change defaults for everyone.

Secrets and machine-specific values (API keys, tokens, a local GGUF model
path) go in a local, gitignored `.env` file instead. Every `config.yaml`
setting can also be overridden per-machine by an environment variable - see
the comments in `config.yaml` and `explorer_agent/config.py` for the exact
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

## Module Index

* `explorer_agent/` — the profiling pipeline (CLI: `python -m explorer_agent.main`)
  * `main.py` — entry point; runs the per-table pipeline. `graph.py` — LangGraph plan → execute → reflect flow.
  * `config.py` + `config.yaml` — all settings; secrets come from the `.env` named by `env_file`.
  * `llm_providers.py` — builds the single LLM (Gemini or local GGUF); `local_llms.py` — the GGUF chat model; `llm_usage.py` — token counts per LLM request.
  * `duplicate_detector.py`, `duplicate_rule_planner.py`, `duplicate_rules.py` — duplicate matching and its per-client rules.
  * `column_mapping.py`, `sap_rules.py`, `anomaly_rules.py`, `rule_packs/` — column meaning and the deterministic SAP rule engines.
  * `survivorship.py`, `scorecard.py` — golden-record recommendation and the DQ scorecard.
  * `structural_profile.py`, `contracts.py`, `events.py` — pipeline handoff documents and events.
  * `privacy_guard.py` — heuristic scrubbing of check results before they reach an LLM; `table_profiler.py` — allowlisted statistical profile.
  * `sandbox.py`, `check_executor.py` — isolated execution of LLM-generated checks; `preflight.py` — free static checks that reject code that can't run.
  * `episodic_store.py` — SQLite run/finding history and human review state; `client_knowledge.py`, `client_workspace.py` — per-client memory and uploaded data.
  * `evaluate.py` — scores a run against a client's answer key.
* `explorer_agent/memory/` — `base.py` (MemoryStore interface), `chroma_store.py` (Chroma adapter), `__init__.py` (backend factory `get_memory_store`), `skill_registry.py` (procedural JSON source of truth), `retriever.py`, `promotion.py` (episodic → procedural → semantic), `reindex.py` (rebuild the vector index), `duplicate_rule_store.py`.
* `review_app/` — FastAPI + vanilla JS review UI (`uvicorn review_app.main:app`); `job_manager.py` runs the explorer as a child process.
