
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

Create a `.env` file in the project root:

```env
GEMINI_API_KEY="your-gemini-key"
KAGGLE_API_TOKEN="kaggle-api-token"

GROQ_API_KEY="gsk_your-groq-key" # LLM fallback provider (config.yaml llm.fallback_providers)
ANTHROPIC_API_KEY="your-anthropic-key" # not required for now
OPENAI_API_KEY="sk-proj-your-openai-key" # not required for now
HF_TOKEN="your-huggingface-token" # not required for now

# Only read when config.yaml's llm.provider is "local":
# EXPLORER_LOCAL_LLM_MODEL_PATH="C:\path\to\your-model.gguf"
```

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

* `memory/` — Base interface adapters (`base.py`), vector backend factory (`factory.py`), Chroma implementation (`chroma.py`), retriever module (`retriever.py`), and skill registry (`skill_registry.py`).
* `promotion.py` — Pipeline logic for deduplicating and promoting approved findings from episodic to procedural memory.
* `reindex.py` — Utility script to rebuild the ChromaDB vector index from the procedural JSON source of truth.
* `privacy_guard.py` — Heuristic-based output scrubbing to safeguard sensitive client data.
* `sandbox.py` — Isolated execution context for dynamic check generation and evaluation.

```

```