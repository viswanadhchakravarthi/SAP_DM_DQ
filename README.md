# SAP_DM_DQ

Agentic SAP Data Migration and Data Quality (DQ) Profiler equipped with modular episodic, procedural, and semantic memory layers.

---

## Overview

`SAP_DM_DQ` is an intelligent, memory-augmented exploration agent built for enterprise SAP data migration and quality assessment workflows. The platform moves beyond basic static profiling into persistent, reusable data check generation, deterministic SAP business validation, and human-in-the-loop governance.

> **New here?** There are two entry points: the CLI (`python -m orchestrator.runner`) and the review
> web app (`uvicorn review_app.main:app`). Go to [Installation](#installation--setup), then
> [Quick Start Step 1](#1-provide-client-data-start-here---a-fresh-clone-has-none) - **a fresh clone
> ships no data to profile**, so that step comes first. For how the pieces fit together, read
> [The Core Workflow](#the-core-workflow-and-why-the-second-run-is-cheaper); for the project's limits,
> [Project Status](#project-status).

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
│   ├── clients/                           # [GITIGNORED - EMPTY ON A FRESH CLONE] see "Step 1" below
│   │   ├── acme-retail/
│   │   │   ├── Data_Dictionary.csv        # Table and field descriptions
│   │   │   ├── LFA1.csv                   # Table extracts
│   │   │   ├── LFB1.csv
│   │   │   └── workspace.json             # Workspace metadata & helper column preferences
│   │   └── preflight-test/
│   ├── answer_keys/                       # Ground-truth benchmark files (isolated from discovery)
│   │   └── acme-retail_ANSWER_KEY.csv     # [not bundled - bring your own, see "Benchmark Evaluation"]
│   └── reference/                         # Offline reference datasets
│       ├── README.md
│       └── geo_postal.db                  # [GENERATED, optional] see "Offline Reference Data"
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
│   │   ├── skill_registry.json            # [GENERATED on first skill promotion]
│   │   └── skills/                        # [GENERATED] one .py per promoted skill
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

* **Python**: 3.11 or newer (developed on 3.13)
* **OS**: Windows / Linux / macOS
* **Virtual Environment**: Python `venv` or `conda`
* **Disk / network**: ~2 GB for dependencies. On the **first run that touches semantic memory**, the
  all-MiniLM-L6-v2 ONNX embedding model (~80 MB) is downloaded and cached by ChromaDB. Everything
  after that is offline. Set `memory.embedding.allow_download: false` in `config.yaml` to fail fast
  instead of downloading on a locked-down machine.
* **No API key is needed** to get started - the deterministic engines in Step 2 run entirely offline.
  A `GEMINI_API_KEY` is only required for LLM-backed exploration (Step 3).

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

### 3. `llama-cpp-python` fallbacks (only if Step 2 failed)

**Usually you can skip this.** `requirements.txt` already pins the pre-compiled CPU-only wheel, so
Step 2 installs it for you. The notes below are fallbacks for when that pinned URL is unreachable or
your platform differs.

Compiling `llama-cpp-python` from source on Windows requires Visual Studio C++ Build Tools (`MSVC`) and `CMake`. To bypass compilation errors and avoid installing heavy C++ toolchains, install pre-compiled wheels:

> **Do not relax the pin to a bare `llama-cpp-python`.** PyPI ships only an sdist for 0.3.35, and the
> CUDA wheel statically imports `cudart64_12.dll`, which makes `import llama_cpp` fail on any machine
> without an NVIDIA GPU - with a misleading "could not find llama.dll" message.

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

### 4. Local Model Download (`kagglehub`) - optional

Only needed if you want `--llm-provider local`, the local-model duplicate/address audit
(`local_audit.enabled`) or the "Explain in plain language" button in the review app. Skip it if you
are using Gemini or only the deterministic engines.

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
Create a `.env` file in your **home directory** (`~/.env` or `C:\Users\<user>\.env`) - **not** in the
repository, so secrets never sit in a working tree. Override the location with `EXPLORER_ENV_FILE`.
```env
GEMINI_API_KEY="your-gemini-api-key"      # required only for llm.provider: google (LLM runs)
KAGGLE_API_TOKEN="your-kaggle-api-token"  # optional: only for PRIVATE kagglehub items
```
Neither key is needed for `--deterministic-only` runs or the review app.

*Note: Any setting in `config.yaml` can be overridden per-machine via matching environment variables (see `src/agents/config.py`).*

---

## Quick Start & Usage

### 1. Provide Client Data (start here - a fresh clone has none)

`data/clients/` is **gitignored**, so cloning this repository gives you the rules and the code but
**no CSVs to profile**. There is no bundled dataset. Running the agent before this step fails with:

```
FileNotFoundError: [Errno 2] No such file or directory: '...\data\clients\acme-retail\Data_Dictionary.csv'
```
or, if a dictionary exists but no tables do, `No table CSV files found in <data-dir>`.

Pick one of:

**(a) Upload through the review app (easiest, no file juggling)** - skip ahead to Step 5, start the
server, open page 1, create a client and upload your dictionary and table CSVs. The app writes them
into `data/clients/<client_id>/` for you. You can then come back and run the CLI.

**(b) Drop files in by hand:**
```bash
mkdir -p data/clients/my-client          # Windows: md data\clients\my-client
# copy in one CSV per table plus the dictionary:
#   data/clients/my-client/Data_Dictionary.csv   (columns: Table, Field, Description [, Data_Type])
#   data/clients/my-client/LFA1.csv              (file name = table name; LFA1.csv -> table LFA1)
#   data/clients/my-client/LFB1.csv
```
Every CSV under `--data-dir` is discovered **recursively** as a table, named after its file, except
the dictionary. Sub-folders are cosmetic grouping only (`vendor-master/LFA1.csv` is still table
`LFA1`), and two files resolving to the same table name abort the run.

**(c) Get the synthetic `acme-retail` dataset from a teammate.** It is referenced throughout this
README because it is the project's test bench, but it is not redistributable through git. Note that
`rules/local/clients/acme-retail/` **is** in git, so once you have its CSVs its duplicate-matching
rules are already saved and a `--deterministic-only` run costs nothing at all.

No data dictionary? Add `--no-dictionary` (see the CLI reference) - column meaning then comes from
names, statistics and the SAP rule pack.

### 2. Run the Deterministic Engines (no LLM, no API key)

The fastest way to confirm the install. This runs duplicate matching plus the built-in SAP rule pack
with zero planner/reflector calls. `--client` and `--data-dir` are both required:
```bash
python -m orchestrator.runner --client acme-retail --data-dir data/clients/acme-retail --deterministic-only
```

One caveat: a table whose **schema has never been seen for this client** costs a single LLM call to
draft its duplicate-matching rules. With no API key configured that call is skipped and the table
falls back to **identical-row matching only** - everything else still runs. Once drafted, the rules
are saved to `rules/local/clients/<client>/duplicate_rules.json` and every later run is free.

### 3. Read What a Run Produced

A run prints a summary block and writes to four places. Knowing these four saves a lot of guessing:

| Where | What |
|---|---|
| **Console summary** | Findings count, LLM calls and tokens, rules evaluated, cache hits, **DQ Index** (cell-level quality) and **Record readiness** (share of records with no open defect - the go/no-go number). |
| `storage/perm/episodic_memory.db` | Every run, finding and row-level record, plus human review state. This is what the review app reads. |
| `storage/perm/handoff/<client>/<run_id>/structural_profile.json` | The OUT contract for the downstream Mapping Agent. Also `handoff/events.jsonl` gets a `profiling.completed` event. |
| `storage/tmp/logs/` | Rotating log file plus one full stdout/stderr log per review-app-triggered run. |

The last line of stdout is machine-readable, which is how the review app picks up results:
```
RESULT_JSON: {"run_id": "...", "client_id": "...", "status": "COMPLETED", ...}
```
`status` is `COMPLETED`, or `PARTIAL` when some tables were skipped (exit code 1). **To actually look
at the findings, start the review app (Step 5)** - the CLI only summarizes.

### 4. Full Profiling Run (with LLM Exploration)

Needs `GEMINI_API_KEY` in your `.env` (or `--llm-provider local`). Budget: **2 LLM calls per table**
(planner + reflector), plus one-off calls per new schema for column mapping and duplicate rules.
```bash
# Profile all tables for a client
python -m orchestrator.runner --client acme-retail --data-dir data/clients/acme-retail

# Profile without a data dictionary (infers types and meanings dynamically)
python -m orchestrator.runner --client acme-retail --data-dir data/clients/acme-retail --no-dictionary

# Duplicate detection only
python -m orchestrator.runner --client acme-retail --data-dir data/clients/acme-retail --duplicates-only

# Just two tables, and recreate findings this client already has
python -m orchestrator.runner --client acme-retail --data-dir data/clients/acme-retail --tables LFA1 LFB1 --no-skip-known
```

### 5. Launch the Human Review Web Application

Start the FastAPI review server:
```bash
uvicorn review_app.main:app --reload
# Or on Windows:
start_appl.bat
```
* **Page 1 (`http://localhost:8000/`)**: Select or create a client workspace, upload CSV tables and data dictionaries, pick "helper columns" for extra review context.
* **Page 2 (`http://localhost:8000/review.html?client=<client_id>`)**: Interactive review dashboard, DQ scorecard, survivorship acceptance, and skill promotion.

You can also trigger runs from the UI (the **Run** dialog) instead of the CLI; the server spawns
`python -m orchestrator.runner` as a child process and streams its log. **Only one run at a time** is
allowed, because everything shares a single SQLite file.

### 6. Benchmark Evaluation

Score run findings against the client's answer key to measure precision and recall. This needs
`data/answer_keys/<client_id>_ANSWER_KEY.csv`, which is **not bundled** - supply your own key
(columns: `Object,Table,Key_Field,Key,Org_Key,Field,Issue_Type,Issue_Description`) or pass one
with `--answer-key <path>`. Keys must stay out of `data/clients/`, where every CSV is profiled
as a table:
```bash
python -m src.agents.tools.evaluate --client acme-retail
```

---

## The Core Workflow (and why the second run is cheaper)

The point of the memory layers is that the agent gets cheaper and better as it is used. The loop:

```
 1. Upload data            review app page 1  ->  data/clients/<id>/
 2. Run the agent          CLI or Run dialog  ->  findings in episodic_memory.db
 3. Review findings        review app page 2  ->  per-record dispositions, duplicate verdicts
 4. Promote good checks    "Promote Approved Skills"  ->  rules/procedural/skill_registry.json
 5. Re-run                 cheaper: mappings, duplicate rules and skills come from memory
```

What gets remembered between runs, and where you can hand-edit it:

| Decision | Saved to | Effect on the next run |
|---|---|---|
| What a column **means** | `rules/local/clients/<id>/column_mappings.json` | No column-mapping LLM call. **This is the correction path** - fix a wrong concept here and the deterministic engines immediately behave differently, with no code change. |
| How to **match duplicates** | `rules/local/clients/<id>/duplicate_rules.json` | No duplicate-rule LLM call. |
| Duplicate **verdicts** | `rules/local/clients/<id>/duplicate_decisions.json` | Settled groups are not shown again; remembered verdicts are pre-filled (shown as ↺). |
| An **approved reusable check** | `rules/procedural/skill_registry.json` + `rules/procedural/skills/*.py` | Re-runs for free instead of asking the planner, and can match a differently-named column by meaning. |

Re-runs also **do not recreate findings this client already has** on unchanged data
(`profiling.skip_known_findings`; `--no-skip-known` turns it off). Measured on `acme-retail` with the
deterministic engines: first run 32 findings, immediate re-run 3.

---

## CLI Reference

`python -m orchestrator.runner` - the profiling pipeline:

| Flag | Meaning |
|---|---|
| `--client <name>` | **Required.** Client/company the data belongs to. Drives `rules/local/clients/<slug>/` and links runs, findings and remembered decisions. |
| `--data-dir <path>` | **Required.** Folder holding the client's CSVs. Searched recursively; every CSV except the dictionary is a table. |
| `--dictionary-file <name>` | Dictionary file name (default `Data_Dictionary.csv`, from `data.dictionary_file`). |
| `--no-dictionary` | The client has no dictionary. Column meaning comes from names, statistics and the SAP rule pack; numeric columns are typed by a safe guess (leading-zero values stay text). |
| `--deterministic-only` | Only the LLM-free engines: duplicate matching + the SAP rule pack. No planner/reflector. |
| `--duplicates-only` | Only duplicate matching. |
| `--tables LFA1 LFB1` | Restrict to these tables. Note that cross-table rules (orphans, flag cascades) are skipped when the other table is not loaded. |
| `--no-skip-known` | Recreate findings this client already has from an earlier run on the same data. |
| `--no-cache` | Ignore promoted skills for this run (forces fresh planner calls). |
| `--max-repair-rounds <n>` | Rounds in which failed checks are sent back to the planner (`0` = off, default `1`). |
| `--llm-provider google\|local` | Override the backend from `config.yaml`. |
| `--model <id>` / `--temperature <f>` | Override the primary provider's model/temperature. |
| `--mapping-file <path>` | The Mapping Agent's field/value mapping (`sap-dm.field-value-mapping`). Defaults to `<data-dir>/field_mapping.json` if present. |
| `--target-domains-file <path>` | Allowed SAP values per target field (`sap-dm.target-domains`). Defaults to `<data-dir>/target_domains.json` if present. |

Exit codes: `0` = completed, `1` = at least one table was skipped (`PARTIAL`), `2` = bad arguments.

### Other Command-Line Tools

```bash
# Score a run against an answer key (recall per issue type, precision per finding)
python -m src.agents.tools.evaluate --client <id> [--run-id <id>] [--source rules|duplicates|llm|all] [--answer-key <path>]

# Promote human-approved findings into the skill registry (the review app's button does the same)
python -m src.agents.memory.promotion [--run-id <id>] [--dedup-threshold <float>]

# Rebuild the ChromaDB vector index from the procedural registry (the registry is the source of truth)
python -m src.agents.memory.reindex

# Regenerate the handoff contract JSON Schemas into rules/schemas/ after editing contracts.py
python -m src.agents.contracts

# Build the optional offline postal directory (see below)
python -m src.agents.tools.build_geo_postal
```

---

## Offline Reference Data (`data/reference/geo_postal.db`)

**Optional, generated, and not in git.** This is a developer-time build step, not a runtime
dependency - the agent never downloads anything while it runs.

**What it is:** a small SQLite lookup of `country + postal code -> place, region`, compiled from the
[GeoNames](https://www.geonames.org) postal-code extracts.

**Why you might want it:** when a required *City* field is blank, the agent first tries to infer the
value from the client's own verified records (same country + postal code). If the dataset has no
answer, it asks this database. With it you get more `AUTO_FIXABLE` completeness proposals instead of
manual ones.

**What happens without it:** every lookup returns nothing and the agent behaves exactly as before -
no errors, no crash, just fewer inferred cities. **You do not need it to run anything in this README.**

```bash
# Default country set (IN, US, DE, GB, FR, NL, ... - a few MB), downloads from GeoNames:
python -m src.agents.tools.build_geo_postal

# Only the countries you care about:
python -m src.agents.tools.build_geo_postal --countries IN US DE

# Fully offline, from pre-downloaded <CC>.zip files (https://download.geonames.org/export/zip/):
python -m src.agents.tools.build_geo_postal --from-dir C:\geonames
```

**Licence:** GeoNames data is Creative Commons Attribution 4.0. Keep the attribution in
[`data/reference/README.md`](data/reference/README.md) with the data if you redistribute it.

Other reference lists (ISO 3166-1 countries, ISO 4217 currencies, Incoterms 2020, postal and tax
formats per country) are **not** here - they live in `rules/packs/sap_master_data.yaml` and ship with
the repository.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `FileNotFoundError: ...Data_Dictionary.csv` or `No table CSV files found` | No client data. See **Step 1** - a fresh clone has none. Or pass `--no-dictionary` if the client genuinely has no dictionary. |
| `error: the following arguments are required: --data-dir` | Both `--client` and `--data-dir` are required on every run. |
| `ValueError: Files 'a.csv' and 'b.csv' both map to table LFA1` | Discovery is recursive and names tables after files. Remove or rename the duplicate; don't point `--data-dir` at `data/clients/` itself (every client has an `LFA1.csv`). |
| `EnvironmentError: Missing required configuration: GEMINI_API_KEY (.env)` | Secrets come from the `.env` named by `env_file` in `config.yaml` (default `~/.env`, i.e. your **home directory**), **not** from a `.env` in the repo. Or run with `--deterministic-only`, which needs no key. |
| Dictionary upload rejected | The dictionary CSV must have the columns `Table`, `Field`, `Description` (a `Data_Type` column is used for numeric typing when present). |
| `import llama_cpp` fails, or "could not find llama.dll" | You have a CUDA wheel on a machine without an NVIDIA GPU + CUDA runtime. Reinstall the pinned CPU wheel - see Installation Step 3. Only matters for `--llm-provider local`. |
| `An Explorer Agent run is already in progress` (HTTP 409) | One run at a time by design (shared SQLite). Wait, or stop it via `POST /api/jobs/{id}/stop`. |
| Review app serves stale JS after an update | Hard-reload the browser. Responses are sent `Cache-Control: no-cache`, but an already-cached copy can survive. |
| Embedding model download fails on an offline machine | Pre-place the model and point `memory.embedding.model_dir` at it, or set `allow_download: false` and accept that semantic skill reuse is off. |
| Findings from an earlier run don't reappear | By design - see `profiling.skip_known_findings`. Use `--no-skip-known`. |
| Want to start completely fresh | Delete `storage/` (or run `clean_memory.bat`). **Read the next section first** - this no longer wipes everything. |

---

## Conventions & Gotchas for New Developers

* **No test suite, linter, type checker or `pyproject.toml`** in this repository yet. Verify changes by
  running the pipeline on a synthetic client and comparing against its answer key - not by reasoning
  about the code alone. (`python -m compileall .` is the cheapest smoke check.)
* **All intra-project imports are absolute** (`from src.agents.engines.sap_rules import ...`), so each
  import names the layer it reads from. `orchestrator/` depends on `src/agents/`, never the reverse.
* **Everything must run from the repository root**, which is what puts `orchestrator/`, `src/` and
  `review_app/` on the import path. All configured relative paths resolve against
  `Config.PROJECT_ROOT`, not your current directory, so data paths are safe either way - imports are not.
* **Agent state is split three ways, and only one third is disposable:**
  `rules/` is version-controlled and meant to be hand-edited; `data/` holds datasets; `storage/` is
  disposable (run history, vector index, outbox, logs). `clean_memory.bat` deletes **only `storage/`** -
  it no longer wipes learned rules or datasets, which are now a git concern.
* **`rules/local/clients/*` is gitignored except the synthetic `acme-retail`.** Remembered duplicate
  decisions carry vendor numbers and reviewer names, so a real client must stay untracked. Real client
  data must never be committed.
* **Answer keys never go inside `data/clients/`.** Every CSV there is profiled as a table and its
  contents reach the planner LLM - a key stored there leaks the answers into the run.
* **Never add a per-column or per-row LLM call.** The budget is 2 calls per table regardless of column
  count, and any new call site must be visible in `src/agents/metrics.py`.
* **New LLM call sites need the same privacy treatment** as existing ones (allowlisted profile or
  `privacy_guard.sanitize_result_for_llm`). Adding a field to a prompt means adding it to an
  allowlist, never removing a filter.
* **Only `chroma_store.py` may import `chromadb`**; everything else goes through the `MemoryStore` ABC.
* **Configuration goes through `Config`** (`src/agents/config.py`). Non-secrets in `config.yaml` with an
  env override, secrets only in `.env`. Never hard-code a path or key.

---

## Architectural Principles & Invariants

1. **No Raw Data to LLMs:** Planner inputs use distilled statistical profiles with allowlisted fields. Reflector inputs pass through `privacy_guard.py`. Only column names, masked values (`mask_value`), and metadata reach LLMs.
2. **Bounded LLM Budget:** Every table costs at most 2 LLM calls (planner + reflector). Column mappings and duplicate matching rules are drafted once per schema signature and saved as human-auditable JSON in `rules/local/` for future free runs.
3. **Deterministic Where Feasible:** Known SAP standards, ISO lookups, tax checksums, and duplicate blocking algorithms run as native Python engines without LLM dependencies.
4. **Human-in-the-Loop Gate:** Proposed checks require explicit human approval in the review UI before they can be promoted into permanent procedural skills (`rules/procedural/skill_registry.json`).
5. **Procedural JSON is the Source of Truth:** Procedural rules and check registries are stored in human-readable JSON files. The vector index (`ChromaDB`) is a derived search index that can be completely rebuilt anytime (`python -m src.agents.memory.reindex`).
6. **LLM-generated code runs only in the sandbox** (`orchestrator/sandbox.py`), never through `exec` in the main process.

---

## Project Status

This is a **proof of concept**, currently focused on the Data Profiling Agent (the exploration
pipeline and its review loop). Cleansing, transformation, load and reconciliation are **not built**.

Know these limits before pointing it at anything real:

* **The review app has no authentication or authorization**, and CORS is open. Reviewer identity is a
  free-text name on duplicate decisions only.
* **Sandbox isolation is POC-grade.** CPU/memory `rlimits` are Unix-only, so **on Windows only the
  wall-clock timeout applies**. It is good enough to let an LLM experiment safely, not hardened
  against a determined adversary.
* **Data sits unencrypted on local disk** - raw CSVs in `data/clients/`, row-level PII in
  `finding_items` inside the SQLite store.
* **Masked samples and the client data dictionary are sent to a hosted LLM** (Gemini) unless you run
  `--llm-provider local` or the deterministic engines only.
* **One run at a time**, job state is in memory, and SQLite is the only store.
* **Evaluation is manual.** `evaluate.py` is a scoring harness, not a CI gate, and the synthetic
  answer keys are known to be incomplete - low precision on a rule means "read the rows", not
  "the rule is wrong".
