# SAP_DM_DQ

Agentic SAP Data Migration and Data Quality (DQ) Profiler equipped with modular episodic, procedural, and semantic memory layers.

---

## Overview

`SAP_DM_DQ` is an intelligent, memory-augmented exploration agent built for enterprise SAP data migration and quality assessment workflows. The platform moves beyond basic static profiling into persistent, reusable data check generation, deterministic SAP business validation, and human-in-the-loop governance.

### Key Capabilities

* **LangGraph Orchestration:** Manages structured batch data profiling workflows (Plan → Execute → Repair → Reflect).
* **Tri-Tier Memory System:**
  * **Episodic Store (SQLite):** Tracks execution history, individual run logs, scorecard history, and pending validation items.
  * **Procedural Registry (JSON/Filesystem):** Serves as the human-readable, auditable source of truth for promoted check skills and schema-compiled duplicate matching rules.
  * **Semantic Memory (ChromaDB Vector Store):** Enables fuzzy/semantic retrieval of historical checks for prompt injection during profiling.
* **Deterministic SAP Master Data Rules:** Built-in validation pack (`rules/packs/sap_master_data.yaml`) executing Priority 1 master data checks (activeness, completeness, ISO formats, tax/IBAN checksums) with **zero LLM cost**.
* **Concept-Driven Duplicate Detection:** Vectorized blocking and clustering with automated survivorship scoring to establish Golden Records.
* **Gated Promotion Pipeline:** Near-duplicate check detection and human review before promoting findings into permanent procedural skills.
* **Isolated Sandbox Execution:** Runs agent-generated data checks safely using multiprocessing process isolation.
* **Privacy & PII Protection:** Heuristic-based result scrubbing (`privacy_guard.py`) and strict column allow-listing (`table_profiler.py`) ensuring no raw data reaches hosted LLMs.

---

## Repository Architecture & Directory Layout

The repository is organized into distinct, modular boundaries:

```text
SAP_DM_DQ/
├── data/                                  # ─── DATASETS ROOT ────────────────────────────────────
│   ├── clients/                           # Active client working datasets (gitignored)
│   │   ├── acme-retail/
│   │   │   ├── Data_Dictionary.csv        # Table and field descriptions
│   │   │   ├── LFA1.csv                   # Table extracts
│   │   │   ├── LFB1.csv
│   │   │   └── workspace.json             # Workspace metadata & helper column preferences
│   │   └── preflight-test/
│   ├── answer_keys/                       # Ground-truth benchmark files (isolated from discovery)
│   │   └── acme-retail_ANSWER_KEY.csv
│   └── reference/                         # Offline reference datasets
│       ├── README.md
│       └── geo_postal.db                  # GeoNames postal/city database
│
├── rules/                                 # ─── CONFIGURATION RULES (.JSON & .YAML) ─────────────
│   ├── local/                             # Local & client-specific configuration rules (.json)
│   │   ├── clients/                       # Per-client rule configurations
│   │   │   └── acme-retail/
│   │   │       ├── client.json            # Client profile and domain metadata (.json)
│   │   │       ├── duplicate_rules.json   # Schema-compiled duplicate detection rules (.json)
│   │   │       ├── duplicate_decisions.json# Remembered reviewer decisions (.json)
│   │   │       └── column_mappings.json   # Business concept column mappings (.json)
│   │   └── industries/                    # Industry-specific configuration rules (.json)
│   │       ├── fmcg/industry.json
│   │       ├── manufacturing/industry.json
│   │       ├── pharma/industry.json
│   │       └── retail/industry.json
│   ├── procedural/                        # Shared procedural rules & promoted check skills
│   │   ├── duplicate_rules.json           # Universal duplicate matching rules (.json)
│   │   ├── skill_registry.json            # Promoted DQ check registry (.json)
│   │   └── skills/                        # Extracted Python check code for skills
│   ├── packs/                             # Standard deterministic rule packs (.yaml)
│   │   └── sap_master_data.yaml           # SAP P1 standards, ISO formats, tax/IBAN rules
│   └── schemas/                           # Pipeline handoff contract schemas & examples (.json)
│       ├── sap-dm.field-value-mapping.v1.schema.json
│       ├── sap-dm.pipeline-event.v1.schema.json
│       ├── sap-dm.structural-profile.v1.schema.json
│       └── sap-dm.target-domains.v1.schema.json
│
├── orchestrator/                          # ─── WORKFLOW ORCHESTRATOR ────────────────────────────
│   ├── runner.py                          # Primary CLI runner & table exploration loop
│   ├── graph.py                           # LangGraph StateGraph (plan -> execute -> repair -> reflect)
│   ├── cache_runner.py                    # Exact & semantic skill cache execution router
│   ├── check_executor.py                  # Executes checks in sandbox with pre-flight checks
│   ├── preflight.py                       # AST static validator rejecting invalid code
│   ├── repair.py                          # Bounded LLM self-repair loop for failed checks
│   ├── sandbox.py                         # Multiprocessing isolated execution environment
│   └── job_manager.py                     # Async child process supervisor for web review runs
│
├── src/agents/                            # ─── AGENT DOMAIN ENGINES & LOGIC ─────────────────────
│   ├── config.py                          # Central settings resolver (config.yaml + .env)
│   ├── schemas.py                         # Pydantic data models for checks, plans, and judgments
│   ├── contracts.py                       # Pydantic schemas for handoff contracts
│   ├── events.py                          # Pipeline outbox event emitter
│   ├── metrics.py                         # RunMetrics instrumentation
│   ├── logging_config.py                  # Structured console and rotating file logger
│   │
│   ├── engines/                           # Core profiling & evaluation engines
│   │   ├── sap_rules.py                   # P1 standard SAP business rules engine
│   │   ├── anomaly_rules.py               # P2 statistical anomalies, IQR fence, text hygiene
│   │   ├── duplicate_detector.py          # Deterministic duplicate clustering & blocking
│   │   ├── duplicate_rule_planner.py      # Metadata-only LLM duplicate rule generator
│   │   ├── duplicate_rules.py             # Duplicate rule resolution hierarchy
│   │   ├── column_mapping.py              # Semantic concept mapper (SAP standard + LLM)
│   │   ├── survivorship.py                # Golden record quality scoring & survivorship
│   │   ├── scorecard.py                   # 4-pillar DQ Index and readiness calculator
│   │   ├── enrichment.py                  # City/postal code inference engine
│   │   ├── checksums.py                   # Mod-97 IBAN and tax registration check digits
│   │   └── geo_reference.py               # Offline GeoNames spatial resolver
│   │
│   ├── memory/                            # Tri-Tier Memory System
│   │   ├── base.py                        # MemoryStore ABC abstraction layer
│   │   ├── episodic_store.py              # Tier 1: SQLite episodic store & review gate
│   │   ├── skill_registry.py              # Tier 2: Procedural skill registry manager
│   │   ├── duplicate_rule_store.py        # Procedural & client duplicate rule store
│   │   ├── client_knowledge.py            # Client-specific durable memory manager
│   │   ├── chroma_store.py                # Tier 3: ChromaDB vector store adapter
│   │   ├── retriever.py                   # Semantic skill retriever for prompt injection
│   │   ├── promotion.py                   # Gated episodic -> procedural promotion
│   │   └── reindex.py                     # Vector index rebuild utility from procedural store
│   │
│   ├── profilers/                         # Statistical profiling & privacy boundary
│   │   ├── table_profiler.py              # ydata-profiling distillation & allowlist
│   │   ├── structural_profile.py          # Structural profiling generator (OUT contract)
│   │   ├── profiler_primitives.py         # Text hygiene, address normalizer, IQR fence
│   │   └── privacy_guard.py               # PII sanitization boundary before LLM egress
│   │
│   ├── llm/                               # Model providers and token governance
│   │   ├── llm_providers.py               # Gemini & local provider factory
│   │   ├── llm_usage.py                   # Token usage tracking & SQLite logger
│   │   ├── local_llms.py                  # QwenCoder GGUF llama-cpp wrapper
│   │   └── local_auditor.py               # Offline local LLM audit for duplicates/addresses
│   │
│   ├── data_loader/                       # Data ingestion & client workspace management
│   │   ├── data_loader.py                 # CSV ingestion, dynamic typing, dictionary parser
│   │   └── client_workspace.py            # Workspace directory discovery & metadata
│   │
│   └── tools/                             # Utilities & offline builders
│       ├── build_geo_postal.py            # GeoNames offline postal DB compiler
│       ├── explain.py                     # Deterministic & LLM "Why flagged?" explainer
│       ├── evaluate.py                    # Answer-key precision/recall scoring harness
│       ├── skill_reuse.py                 # Semantic skill similarity matcher
│       └── storage_layout.py              # Legacy storage migration helper
│
├── review_app/                            # ─── HUMAN-IN-THE-LOOP WEB APPLICATION ────────────────
│   ├── main.py                            # FastAPI backend REST API
│   └── static/                            # Lightweight frontend (HTML, CSS, JS)
│       ├── index.html                     # Client selection & dataset upload portal
│       ├── review.html                    # Profiling review dashboard & disposition matrix
│       ├── setup.js                       # Workspace management scripts
│       ├── app.js                         # Review UI interaction & mini-windows
│       └── style.css                      # Application styling
│
├── storage/                               # ─── RUNTIME PERSISTENCE (GITIGNORED) ─────────────────
│   ├── perm/
│   │   ├── episodic_memory.db             # SQLite episodic store (runs, findings, verdicts)
│   │   ├── chroma/                        # Derived ChromaDB vector index
│   │   └── handoff/                       # Pipeline outbox (events.jsonl, structural_profile)
│   └── tmp/
│       └── logs/                          # Process log tails and run logs
│
├── config.yaml                            # Global framework settings and defaults
├── requirements.txt                       # Core Python dependencies
├── start_appl.bat                         # Windows quick start script for review app
└── clean_memory.bat                       # Local cache cleanup utility
```

---

## Where Key Components Live: Quick Reference

| Component | Directory Location | Description |
|---|---|---|
| **The Orchestrator** | [`orchestrator/`](orchestrator/) | State machine ([`graph.py`](orchestrator/graph.py)), CLI runner ([`runner.py`](orchestrator/runner.py)), execution sandbox, AST pre-flight checks, and repair loops. |
| **Local Configuration Rules** | [`rules/local/`](rules/local/) | Client-specific JSON files (`column_mappings.json`, `duplicate_rules.json`, `client.json`). Edit here to adjust column concepts or matching rules without changing code. |
| **Procedural Rules & Packs** | [`rules/procedural/`](rules/procedural/), [`rules/packs/`](rules/packs/) | Promoted skill catalog (`skill_registry.json`), shared duplicate rules, and YAML SAP rule packs (`sap_master_data.yaml`). |
| **Contract Schemas** | [`rules/schemas/`](rules/schemas/) | Versioned JSON schemas defining data migration handoff contracts. |
| **Datasets & Answer Keys** | [`data/`](data/) | Client CSV tables under [`data/clients/<client_id>/`](data/clients/), ground-truth defect answer keys under [`data/answer_keys/`](data/answer_keys/), and offline reference DBs under [`data/reference/`](data/reference/). |
| **Web Review Application** | [`review_app/`](review_app/) | FastAPI service and frontend for interactive review, dispositioning, and skill promotion. |
| **Runtime Persistence** | [`storage/`](storage/) | Ephemeral run databases, vector indexes, and log files. Gitignored and safe to reset. |

---

## Prerequisites

* **Python**: 3.13+ (or 3.11+)
* **OS**: Windows / Linux / macOS
* **Virtual Environment**: Python `venv` or `conda`

---

## Installation & Setup

### 1. Clone Repository & Setup Virtual Environment

```bash
git clone <repository-url>
cd SAP_DM_DQ

# Create virtual environment
python -m venv .venv

# Activate virtual environment
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# Windows (CMD):
.venv\Scripts\activate.bat
# Linux / macOS:
source .venv/bin/activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Installing `llama-cpp-python` (Windows / Python 3.13)

Compiling `llama-cpp-python` on Windows typically requires Visual Studio C++ Build Tools (`MSVC`) and `CMake`. To bypass compilation errors and avoid installing heavy C++ toolchains on Python 3.13, install pre-compiled wheels:

#### Workaround 1: Local Binary Wheel (Recommended)

1. Download the pre-compiled binary wheel matching your system architecture:
   * **File:** `llama_cpp_python-0.3.35-py3-none-win_amd64.whl`
2. Run inside your active `.venv`:
   ```bash
   pip install "path\to\llama_cpp_python-0.3.35-py3-none-win_amd64.whl"
   ```

#### Workaround 2: Remote Wheel Index

```bash
# CPU Only:
pip install llama-cpp-python==0.3.35 --extra-index-url https://abetlen.github.io/llama-cpp-python/wheels/cpu

# CUDA Acceleration:
pip install llama-cpp-python==0.3.35 --extra-index-url https://abetlen.github.io/llama-cpp-python/wheels/cu121
```

### 4. Local Model Download (`kagglehub`)

To run inference locally using GGUF quantization formats without external APIs:

```bash
pip install kagglehub
```

Download the model weights via Python:
```python
import kagglehub

# Download GGUF version of Qwen2.5-Coder 3B Instruct
path = kagglehub.model_download("qwen-lm/qwen2.5-coder/gguf/3b-instruct")
print("Path to model files:", path)
```

Configure `llm.local.model_path` in `config.yaml` with the printed path.

---

## Configuration & Environment Variables

### `config.yaml` (Project Root)
Contains non-secret, shareable defaults:
* LLM provider selection (`google` or `local`)
* Sandbox execution limits (timeouts, worker counts)
* Data and memory path bindings
* Composite scorecard weights and readiness thresholds

### `.env` File (Secrets)
Create a `.env` file in your **home directory** (`~/.env` or `C:\Users\<user>\.env`):
```env
GEMINI_API_KEY="your-gemini-api-key"
KAGGLE_API_TOKEN="your-kaggle-api-token"
```
*Note: Any setting in `config.yaml` can be overridden per-machine via matching environment variables (see `src/agents/config.py`).*

---

## Quick Start & Usage

### 1. Verify Environment

Verify that the local runtime and C++ bindings load correctly:
```bash
python -c "import llama_cpp; print('llama_cpp loaded successfully:', llama_cpp.__file__)"
```

### 2. Run Deterministic Engine Test (No LLM Required)

Test the profiling pipeline immediately on the bundled synthetic client `acme-retail` using deterministic engines (zero API costs):
```bash
python -m orchestrator.runner --client acme-retail --deterministic-only
```

### 3. Full Profiling Run (with LLM Exploration)

```bash
# Profile all tables for a client
python -m orchestrator.runner --client acme-retail --data-dir data/clients/acme-retail

# Profile without a data dictionary (infers types and meanings dynamically)
python -m orchestrator.runner --client acme-retail --data-dir data/clients/acme-retail --no-dictionary

# Duplicate detection only
python -m orchestrator.runner --client acme-retail --duplicates-only
```

### 4. Benchmark Evaluation

Score run findings against the client's answer key to measure precision and recall:
```bash
python -m src.agents.tools.evaluate --client acme-retail
```

### 5. Launch Human Review Web Application

Start the FastAPI review server:
```bash
uvicorn review_app.main:app --reload
# Or on Windows:
start_appl.bat
```
* **Page 1 (`http://localhost:8000/`)**: Select or create a client workspace, upload CSV tables and data dictionaries.
* **Page 2 (`http://localhost:8000/review.html?client=<client_id>`)**: Interactive review dashboard, DQ scorecard, survivorship acceptance, and skill promotion.

---

## Architectural Principles & Invariants

1. **No Raw Data to LLMs:** Planner inputs use distilled statistical profiles with allowlisted fields. Reflector inputs pass through `privacy_guard.py`. Only column names, masked values (`mask_value`), and metadata reach LLMs.
2. **Bounded LLM Budget:** Every table costs at most 2 LLM calls (planner + reflector). Column mappings and duplicate matching rules are drafted once per schema signature and saved as human-auditable JSON in `rules/local/` for future free runs.
3. **Deterministic Where Feasible:** Known SAP standards, ISO lookups, tax checksums, and duplicate blocking algorithms run as native Python engines without LLM dependencies.
4. **Human-in-the-Loop Gate:** Proposed checks require explicit human approval in the review UI before they can be promoted into permanent procedural skills (`rules/procedural/skill_registry.json`).
5. **Procedural JSON is the Source of Truth:** Procedural rules and check registries are stored in human-readable JSON files. The vector index (`ChromaDB`) is a derived search index that can be completely rebuilt anytime (`python -m src.agents.memory.reindex`).
