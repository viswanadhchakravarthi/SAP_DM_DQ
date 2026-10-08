# Architecture Refactoring Proposal: SAP_DM_DQ

## Executive Summary

The `SAP_DM_DQ` repository currently houses a sophisticated, memory-augmented Data Quality and SAP Data Migration profiler. However, as the platform has grown, key architectural components have become conflated:
1. **The Orchestrator** is buried inside a flat `src/agents/` directory among ~45 sibling Python scripts containing rules, primitives, and memory stores.
2. **Local Configuration Rules (`.json`)** are scattered between gitignored SQLite/memory directories (`rules/local/clients/`), contract schemas, and rule packs, making rule governance difficult for new developers to locate and inspect.
3. **Datasets** are stored deep within runtime cache paths (`data/clients/`), mixing input test benches and ground-truth answer keys with ephemeral database artifacts.

This document outlines a clean, modular refactoring plan to reorganize the repository so that a new engineer can immediately understand **where the orchestrator lives**, **where the local configuration rules (.json) sit**, and **where datasets belong**.

---

#proposed Directory layout

```text
SAP_DM_DQ/
├── .gitignore
├── .gitattributes
├── CLAUDE.md
├── README.md
├── implementation.md                      # [THIS PROPOSAL] Architecture and directory layout guide
├── requirements.txt
├── config.yaml                            # Global framework settings and defaults
├── playground.ipynb                       # Interactive exploration and scratchpad
├── clean_memory.bat                       # Maintenance utility for resetting runtime cache
├── start_appl.bat                         # Startup utility for review dashboard
│
├── data/                                  # ─── DATASETS ROOT ────────────────────────────────────
│   │                                      # Dedicated location for all input datasets, isolated
│   │                                      # from runtime databases and execution code.
│   ├── clients/                           # Active client data workspaces
│   │   ├── acme-retail/
│   │   │   ├── Data_Dictionary.csv        # Client data dictionary
│   │   │   ├── LFA1.csv                   # Table extracts
│   │   │   ├── LFB1.csv
│   │   │   └── workspace.json             # Workspace metadata and helper column selections
│   │   └── preflight-test/
│   ├── synthetic/                         # Bundled synthetic test datasets for quick onboarding
│   │   └── vendor_sample/
│   ├── answer_keys/                       # Ground-truth evaluation files (isolated from table loader)
│   │   ├── acme-retail_ANSWER_KEY.csv
│   │   └── preflight-test_ANSWER_KEY.csv
│   └── reference/                         # Offline reference databases
│       ├── README.md
│       └── geo_postal.db                  # GeoNames postal/city database
│
├── rules/                                 # ─── CONFIGURATION RULES (.JSON & .YAML) ─────────────
│   │                                      # Version-controlled, human-auditable rule definitions,
│   │                                      # client overrides, concept mappings, and contracts.
│   ├── local/                             # Local & client-specific configuration rules (.json)
│   │   ├── clients/
│   │   │   ├── acme-retail/
│   │   │   │   ├── client.json            # Client profile and domain metadata (.json)
│   │   │   │   ├── duplicate_rules.json   # Schema-compiled duplicate detection rules (.json)
│   │   │   │   ├── duplicate_decisions.json# Remembered reviewer decisions (.json)
│   │   │   │   └── column_mappings.json   # Business concept column mappings (.json)
│   │   │   └── preflight-test/
│   │   │       ├── client.json
│   │   │       └── duplicate_rules.json
│   │   └── industries/                    # Industry-specific configuration rules (.json)
│   │       ├── fmcg/industry.json
│   │       ├── manufacturing/industry.json
│   │       ├── pharma/industry.json
│   │       └── retail/industry.json
│   ├── procedural/                        # Shared procedural rules & promoted check skills
│   │   ├── duplicate_rules.json           # Universal duplicate matching rules (.json)
│   │   ├── skill_registry.json            # Promoted DQ check registry (.json)
│   │   └── skills/                        # Python implementations of promoted skills
│   ├── packs/                             # Standard deterministic rule packs (.yaml)
│   │   └── sap_master_data.yaml           # SAP P1 standards, ISO formats, tax/IBAN rules
│   └── schemas/                           # Pipeline handoff contract schemas & examples (.json)
│       ├── README.md
│       ├── sap-dm.field-value-mapping.v1.schema.json
│       ├── sap-dm.pipeline-event.v1.schema.json
│       ├── sap-dm.structural-profile.v1.schema.json
│       ├── sap-dm.target-domains.v1.schema.json
│       └── examples/
│           ├── field_value_mapping.example.json
│           ├── pipeline_event.example.json
│           ├── structural_profile.example.json
│           └── target_domains.example.json
│
├── orchestrator/                          # ─── WORKFLOW ORCHESTRATOR ────────────────────────────
│   │                                      # LangGraph state machine, pipeline dispatch, AST
│   │                                      # pre-flight validation, repair cycle, and sandboxing.
│   ├── __init__.py
│   ├── graph.py                           # LangGraph StateGraph (plan -> execute -> repair -> reflect)
│   ├── runner.py                          # Batch runner & table exploration loop (formerly main.py)
│   ├── cache_runner.py                    # Exact & semantic skill cache execution router
│   ├── check_executor.py                  # Orchestration bridge for running checks in sandbox
│   ├── preflight.py                       # Zero-LLM AST syntax and safety pre-flight validator
│   ├── repair.py                          # Bounded LLM check self-repair orchestration loop
│   ├── sandbox.py                         # Isolated multiprocessing sandbox executor
│   └── job_manager.py                     # Async child process & background job supervisor
│
├── src/agents/                        # ─── CORE AGENT DOMAIN LOGIC & ENGINES ────────────────
│   ├── __init__.py
│   ├── config.py                          # Centralized configuration resolver
│   ├── schemas.py                         # Pydantic data models for checks, plans, and judgments
│   ├── contracts.py                       # Pydantic schemas for handoff contracts
│   ├── events.py                          # Pipeline outbox event emitter
│   ├── metrics.py                         # RunMetrics call, token, and timing instrumentation
│   ├── logging_config.py                  # Structured console and rotating file logger
│   │
│   ├── engines/                           # Deterministic & specialized profiling engines
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
│   │   ├── __init__.py
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
│   └── static/                            # Lightweight vanilla frontend
│       ├── index.html                     # Client selection & dataset upload portal
│       ├── review.html                    # Profiling review dashboard & disposition matrix
│       ├── setup.js                       # Workspace management scripts
│       ├── app.js                         # Review UI interaction & mini-windows
│       └── style.css                      # Application styling
│
└── storage/                               # ─── RUNTIME PERSISTENCE (GITIGNORED) ─────────────────
    │                                      # Purely ephemeral and compiled runtime artifacts.
    ├── perm/
    │   ├── episodic_memory.db             # SQLite episodic store (runs, findings, verdicts)
    │   ├── chroma/                        # Derived ChromaDB vector index
    │   └── handoff/                       # Pipeline outbox (events.jsonl, structural_profile)
    └── tmp/
        └── logs/                          # Process log tails and run logs
```

---

## Detailed File Migration Mapping

The table below explicitly maps every existing file in the repository to its proposed new location and documents the architectural rationale.

| Existing File Path | Proposed New Path | Category / Pillar | Rationale |
|---|---|---|---|
| **Orchestrator Components** | | | |
| `orchestrator/graph.py` | `orchestrator/graph.py` | Orchestrator | Core LangGraph StateGraph governing `plan_batch` -> `execute_all` -> `repair_batch` -> `reflect_batch`. |
| `orchestrator/runner.py` | `orchestrator/runner.py` | Orchestrator | Primary workflow coordinator; discovers tables, resolves mappings, runs deterministic engines, and executes graph. |
| `orchestrator/cache_runner.py` | `orchestrator/cache_runner.py` | Orchestrator | Directs execution flow between cached skills and LLM generation. |
| `orchestrator/check_executor.py` | `orchestrator/check_executor.py` | Orchestrator | Coordinates AST pre-flight checks and sandbox execution across check batches. |
| `orchestrator/preflight.py` | `orchestrator/preflight.py` | Orchestrator | Pre-execution AST validator guarding sandbox entry. |
| `orchestrator/repair.py` | `orchestrator/repair.py` | Orchestrator | Manages the single conditional loop in LangGraph for self-correcting broken checks. |
| `orchestrator/sandbox.py` | `orchestrator/sandbox.py` | Orchestrator | Process isolation execution environment for generated check code. |
| `orchestrator/job_manager.py` | `orchestrator/job_manager.py` | Orchestrator | Manages background child processes and live log streaming for orchestrator runs. |
| **Local Configuration Rules (.json & .yaml)** | | | |
| `rules/local/clients/<id>/duplicate_rules.json` | `rules/local/clients/<id>/duplicate_rules.json` | Local Rules (.json) | Client-specific compiled duplicate matching rules (.json). Visible and auditable. |
| `rules/local/clients/<id>/column_mappings.json` | `rules/local/clients/<id>/column_mappings.json` | Local Rules (.json) | Client-specific semantic concept mappings (.json); primary manual correction path. |
| `rules/local/clients/<id>/client.json` | `rules/local/clients/<id>/client.json` | Local Rules (.json) | Client metadata and industry classification (.json). |
| `rules/local/clients/<id>/duplicate_decisions.json` | `rules/local/clients/<id>/duplicate_decisions.json` | Local Rules (.json) | Reviewer remembered duplicate decisions (.json). |
| `rules/local/industries/<ind>/industry.json` | `rules/local/industries/<ind>/industry.json` | Local Rules (.json) | Domain and industry baseline configurations (.json). |
| `rules/procedural/duplicate_rules.json` | `rules/procedural/duplicate_rules.json` | Procedural Rules (.json) | Shared cross-client duplicate matching rules (.json). |
| `rules/procedural/skill_registry.json` | `rules/procedural/skill_registry.json` | Procedural Rules (.json) | Source-of-truth procedural check skill registry (.json). |
| `rules/packs/sap_master_data.yaml` | `rules/packs/sap_master_data.yaml` | Rule Packs (.yaml) | Declarative SAP master data validation standards, regexes, and mandatory rules. |
| `rules/schemas/*.schema.json` | `rules/schemas/*.schema.json` | Contracts (.json) | JSON Schemas for external pipeline interfaces. |
| `rules/schemas/examples/*.json` | `rules/schemas/examples/*.json` | Contracts (.json) | Contract payload example instances (.json). |
| **Datasets** | | | |
| `data/clients/<id>/*.csv` | `data/clients/<id>/*.csv` | Datasets | Active client input extracts (`LFA1.csv`, `MARA.csv`, `Data_Dictionary.csv`). |
| `data/clients/<id>/workspace.json` | `data/clients/<id>/workspace.json` | Datasets | Dataset workspace configuration and helper column selections. |
| `data/clients/<id>_ANSWER_KEY.csv` | `data/answer_keys/<id>_ANSWER_KEY.csv` | Datasets | Benchmark defect ground-truth files, separated from client table discovery directories. |
| `data/reference/README.md` | `data/reference/README.md` | Datasets | Guidance for offline postal and geo datasets. |
| `data/reference/geo_postal.db` | `data/reference/geo_postal.db` | Datasets | GeoNames postal lookup database. |
| **Engines & Domain Logic** | | | |
| `src/agents/engines/sap_rules.py` | `src/agents/engines/sap_rules.py` | Business Logic | Priority 1 deterministic SAP validation rules. |
| `src/agents/engines/anomaly_rules.py` | `src/agents/engines/anomaly_rules.py` | Business Logic | Priority 2 statistical anomalies, IQR fence, text hygiene. |
| `src/agents/engines/duplicate_detector.py` | `src/agents/engines/duplicate_detector.py` | Business Logic | Deterministic blocking and duplicate pair matching engine. |
| `src/agents/engines/duplicate_rule_planner.py` | `src/agents/engines/duplicate_rule_planner.py` | Business Logic | LLM duplicate role classification logic. |
| `src/agents/engines/duplicate_rules.py` | `src/agents/engines/duplicate_rules.py` | Business Logic | Resolution hierarchy for duplicate matching configurations. |
| `src/agents/engines/column_mapping.py` | `src/agents/engines/column_mapping.py` | Business Logic | Semantic column concept resolver. |
| `src/agents/engines/survivorship.py` | `src/agents/engines/survivorship.py` | Business Logic | Golden record scoring & survivorship recommendation. |
| `src/agents/engines/scorecard.py` | `src/agents/engines/scorecard.py` | Business Logic | Composite DQ index and readiness scoring. |
| `src/agents/engines/enrichment.py` | `src/agents/engines/enrichment.py` | Business Logic | City-from-postal-code inference. |
| `src/agents/engines/checksums.py` | `src/agents/engines/checksums.py` | Business Logic | IBAN & tax check digit calculation. |
| `src/agents/engines/geo_reference.py` | `src/agents/engines/geo_reference.py` | Business Logic | Postal/city geographic lookup interface. |
| `src/agents/engines/rule_context.py` | `src/agents/engines/rule_context.py` | Business Logic | Context holder and coverage tracker for rule execution. |
| **Memory System** | | | |
| `src/agents/memory/episodic_store.py` | `src/agents/memory/episodic_store.py` | Memory | SQLite episodic store manager. |
| `src/agents/memory/base.py` | `src/agents/memory/base.py` | Memory | Base abstract class for memory backends. |
| `src/agents/memory/chroma_store.py` | `src/agents/memory/chroma_store.py` | Memory | Vector memory backend. |
| `src/agents/memory/skill_registry.py` | `src/agents/memory/skill_registry.py` | Memory | Procedural registry driver. |
| `src/agents/memory/duplicate_rule_store.py` | `src/agents/memory/duplicate_rule_store.py` | Memory | Duplicate rule storage manager. |
| `src/agents/memory/promotion.py` | `src/agents/memory/promotion.py` | Memory | Finding promotion pipeline. |
| `src/agents/memory/retriever.py` | `src/agents/memory/retriever.py` | Memory | Semantic skill retrieval for prompts. |
| `src/agents/memory/reindex.py` | `src/agents/memory/reindex.py` | Memory | Vector index re-indexing utility. |
| `src/agents/memory/client_knowledge.py` | `src/agents/memory/client_knowledge.py` | Memory | Client persistent memory access layer. |
| **Profilers & Privacy** | | | |
| `src/agents/profilers/table_profiler.py` | `src/agents/profilers/table_profiler.py` | Profiling | ydata-profiling wrapper and allowlist extractor. |
| `src/agents/profilers/structural_profile.py` | `src/agents/profilers/structural_profile.py` | Profiling | Generates handoff structural profile JSON. |
| `src/agents/profilers/profiler_primitives.py` | `src/agents/profilers/profiler_primitives.py` | Profiling | String normalization, address expansion, outlier maths. |
| `src/agents/profilers/privacy_guard.py` | `src/agents/profilers/privacy_guard.py` | Profiling | Sanitizes data checks before sending to LLM. |
| **LLM & Infrastructure** | | | |
| `src/agents/llm/llm_providers.py` | `src/agents/llm/llm_providers.py` | LLM | Model provider factory (Gemini, local GGUF). |
| `src/agents/llm/llm_usage.py` | `src/agents/llm/llm_usage.py` | LLM | Token usage collector and metrics recorder. |
| `src/agents/llm/local_llms.py` | `src/agents/llm/local_llms.py` | LLM | Local GGUF chat model wrapper. |
| `src/agents/llm/local_auditor.py` | `src/agents/llm/local_auditor.py` | LLM | Local LLM auditor for borderline pairs and addresses. |
| `src/agents/data_loader/data_loader.py` | `src/agents/data_loader/data_loader.py` | Data Loading | Ingests CSVs and discovers tables. |
| `src/agents/data_loader/client_workspace.py` | `src/agents/data_loader/client_workspace.py` | Data Loading | Manages workspace files on disk. |
| `src/agents/schemas.py` | `src/agents/schemas.py` | Core | Pydantic schemas for findings and checks. |
| `src/agents/contracts.py` | `src/agents/contracts.py` | Core | Pydantic models for pipeline handoff. |
| `src/agents/events.py` | `src/agents/events.py` | Core | Outbox pipeline event publisher. |
| `src/agents/metrics.py` | `src/agents/metrics.py` | Core | Instrumentation and run metrics. |
| `src/agents/logging_config.py` | `src/agents/logging_config.py` | Core | Logging configuration. |
| `src/agents/config.py` | `src/agents/config.py` | Core | Settings and environment variable resolver. |
| `src/agents/tools/storage_layout.py` | `src/agents/tools/storage_layout.py` | Utilities | Storage migration compatibility script. |
| `src/agents/tools/explain.py` | `src/agents/tools/explain.py` | Utilities | Plain-language finding explanations. |
| `src/agents/tools/evaluate.py` | `src/agents/tools/evaluate.py` | Utilities | Scoring harness against answer keys. |
| `src/agents/tools/skill_reuse.py` | `src/agents/tools/skill_reuse.py` | Utilities | Semantic skill similarity matcher. |
| `src/agents/tools/build_geo_postal.py` | `src/agents/tools/build_geo_postal.py` | Utilities | Postal DB generator tool. |
| **Review App** | | | |
| `review_app/main.py` | `review_app/main.py` | Web UI | FastAPI server and API routes. |
| `review_app/static/*` | `review_app/static/*` | Web UI | Static assets (HTML, CSS, JS). |
| **Runtime Persistence** | | | |
| `storage/perm/episodic_memory.db` | `storage/perm/episodic_memory.db` | Runtime Storage | Unchanged. SQLite store. |
| `storage/perm/chroma/*` | `storage/perm/chroma/*` | Runtime Storage | Unchanged. Vector store. |
| `storage/perm/handoff/*` | `storage/perm/handoff/*` | Runtime Storage | Unchanged. Outbox files. |
| `storage/tmp/logs/*` | `storage/tmp/logs/*` | Runtime Storage | Unchanged. Run logs. |

---

## Detailed Focus on the Three Core Pillars

### 1. Where the Orchestrator Lives (`orchestrator/`)

In the existing architecture, the execution flow is fragmented across `orchestrator/runner.py`, `orchestrator/graph.py`, `orchestrator/cache_runner.py`, and `orchestrator/job_manager.py`.

In the proposed layout:
- **`orchestrator/graph.py`**: The definitive LangGraph StateGraph engine. It orchestrates the lifecycle for profiling each table:
  1. `plan_batch` (Structured LLM check proposals)
  2. `execute_all` (Multiprocessing sandbox execution)
  3. `repair_batch` (Conditional self-repair cycle for failed checks)
  4. `reflect_batch` (Structured LLM severity/confidence judgment)
- **`orchestrator/runner.py`** (renamed from `main.py`): The top-level CLI and batch exploration runner. Coordinates table discovery, column concept mapping, deterministic engine execution, and invokes the LangGraph orchestrator.
- **`orchestrator/job_manager.py`**: The async process supervisor that decouples web app triggers from orchestrator runs, streaming logs and managing execution state.
- **`orchestrator/check_executor.py`, `preflight.py`, `repair.py`, `sandbox.py`**: Form the orchestrator execution subsystem, ensuring generated checks are validated via AST, sandboxed safely, and repaired automatically without polluting agent domain logic.

### 2. Where the Local Configuration Rules (.json) Sit (`rules/`)

Previously, rule JSON files were buried inside runtime storage (`rules/`), making them invisible on a fresh clone and creating confusion over whether they were code, configuration, or disposable cache.

Under the new layout, all configuration rules sit under a dedicated top-level `rules/` directory:
- **`rules/local/clients/<client_id>/`**:
  - `duplicate_rules.json`: Exact JSON specification of matching fields, fuzzy thresholds, and blocking rules generated for this client's unique schemas.
  - `column_mappings.json`: Human-editable JSON mappings binding source columns to business concepts (`KEY`, `POSTAL_CODE`, `TAX_ID`, etc.). Reviewers modify this file directly to adjust profiling behavior without code changes.
  - `duplicate_decisions.json`: Saved deduplication decisions for persistent clustering.
  - `client.json`: Client metadata, SAP version, and industry classification.
- **`rules/local/industries/<industry>/industry.json`**: Industry-wide rule defaults (e.g. Pharma batch rules, Retail barcode formats).
- **`rules/procedural/`**:
  - `duplicate_rules.json`: Baseline cross-client rules.
  - `skill_registry.json`: Canonical JSON catalog of promoted data quality checks.
- **`rules/packs/sap_master_data.yaml`**: The standard declarative SAP rule pack.
- **`rules/schemas/*.json`**: Strict JSON Schemas defining contracts for field value mappings, pipeline events, structural profiles, and target domains.

### 3. Where Datasets Belong (`data/`)

Previously, datasets were nested under `data/clients/`, which was gitignored alongside databases and logs. Crucially, ground-truth answer keys sat next to client data folders, creating risk of ingestion leaks if paths were misconfigured.

Under the new layout, all data assets belong under `data/`:
- **`data/clients/<client_id>/`**: Active working datasets for profiling (`LFA1.csv`, `MARA.csv`, `Data_Dictionary.csv`, `workspace.json`).
- **`data/synthetic/`**: Ready-to-run synthetic SAP data samples so any new engineer can clone the repository and run profiling immediately.
- **`data/answer_keys/`**: Answer keys (`acme-retail_ANSWER_KEY.csv`) reside in a dedicated folder strictly outside the table discovery path (`data_loader.discover_table_files`), guaranteeing zero risk of answer leakage into LLM prompts.
- **`data/reference/`**: Offline reference lookup tables and spatial databases (`geo_postal.db`).

---

## Migration Path & Compatibility Strategy

To adopt this refactored structure without disrupting ongoing development:

1. **Path Configuration Updates (`config.yaml` & `config.py`)**:
   Update `Config.PROJECT_ROOT` relative paths to reference the new directories:
   ```yaml
   data:
     client_data_dir: "data/clients"
     answer_key_dir: "data/answer_keys"
     reference_dir: "data/reference"
   memory:
     rules_dir: "rules"
     local_rules_dir: "rules/local/clients"
     procedural_dir: "rules/procedural"
     schema_dir: "rules/schemas"
   storage:
     perm_dir: "storage/perm"
     tmp_dir: "storage/tmp"
   ```
2. **Backward-Compatible Path Aliases**:
   Maintain a fallback in `src/agents/config.py` that checks `data/clients` first, falling back to legacy `data/clients` if legacy folders exist.
3. **Packaging & Imports**:
   Maintain `src/agents` as the primary import root, with `orchestrator` as a top-level package or sub-package (`src.agents.orchestrator`).
4. **Git Tracking Strategy**:
   - Track `rules/procedural/`, `rules/packs/`, `rules/schemas/`, and sample client configs in `rules/local/`.
   - Track `data/synthetic/` and `data/answer_keys/`.
   - Keep `data/clients/` and `storage/` gitignored to prevent client PII leakage.
