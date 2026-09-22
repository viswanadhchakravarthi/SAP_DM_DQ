# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project context and working role

This repo is a **POC** for an SAP Data Migration & Data Quality platform. The current focus is the **Data Profiling Agent**, meaning the explorer pipeline and its review loop. Cleansing, transformation, load and reconciliation are not built yet, so don't scaffold them unless asked.

Work on it as a **Senior AI/ML Engineer (Agentic AI, Data Engineering, Enterprise Data Quality)**:
- Judge changes against production concerns: reliability, security, data privacy, observability, evaluation, maintainability and deployment. Say so when a change weakens one of them, including changes the user asked for.
- **Don't over-engineer the POC.** Build what the task needs at POC grade and name the production gap in one line, citing the relevant row of "POC vs production" below, instead of building the production version. Add no Kubernetes, auth providers, queues, ORMs or new infrastructure unless asked.
- Keep the design invariants that make the POC credible (see "Invariants" below). They are cheap now and expensive to add back later.
- Verify behavior by running the pipeline on the synthetic clients in `client_data/`, then compare findings against that client's answer key (see "Data in this repo"). Don't just reason about the code.

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

# Run the explorer agent (profiles tables, generates findings); --client is required
python -m explorer_agent.main --client "<client name>" --data-dir <path> [--tables LFA1 LFB1] [--no-cache] [--llm-provider google|groq|local] [--fallback-providers groq ...]

# Only duplicate detection. Free for tables whose matching rules are already saved for this
# client; a schema never seen before costs one LLM call to draft them (identical rows only
# when no API key is configured).
python -m explorer_agent.main --client "<client name>" --data-dir <path> --duplicates-only

# Rebuild the semantic (vector) index from the procedural registry (source of truth)
python -m explorer_agent.memory.reindex

# Promote human-approved findings into the skill registry from the CLI
python -m explorer_agent.memory.promotion [--run-id <id>] [--dedup-threshold <float>]

# Run the human review web app (serves review_app/static/ + JSON API):
# page 1 (/) picks the client and uploads its data, page 2 (/review.html?client=<id>) reviews findings
uvicorn review_app.main:app --reload   # same as start_appl.bat
```

There is no test suite, linter, or `pyproject.toml` configured in this repo currently.

`clean_memory.bat` **wipes all agent state**: it deletes `logs/`, `memory_store/` (skills, vector index, per-client decisions and duplicate rules) and `episodic_memory.db`. Never run it, or anything equivalent, without explicit user confirmation. Its username/password prompt is a local speed bump, not access control.

On this machine `git` is not on PATH. Use GitHub Desktop's bundled `git.exe` (`%LOCALAPPDATA%\GitHubDesktop\app-*\resources\app\git\cmd\git.exe`).

### Data in this repo

- `client_data/<client_id>/` holds **synthetic** SAP vendor-master clients (`acme-retail`, `danawsiv`, `medhovis-therapeutics`, `tarvarka-pvt-ltd`). Each has `Data_Dictionary.csv`, table CSVs and `workspace.json`. Answer keys list the seeded defects (`Object,Table,Key_Field,Key,Org_Key,Field,Issue_Type,Issue_Description`). When you change a dataset, update its answer key in the same change. `sample_output/Answer_Key.csv` is the original reference key.
- **Answer keys live one level above the client folders**, as `client_data/<client_id>{_,-}{ANSWER_KEY,Answer_Key}.csv` (the naming is inconsistent). Never put one inside `client_data/<client_id>/`. `data_loader.discover_table_files` treats every CSV under `--data-dir` as a table, recursively, except the dictionary. A key inside the client folder would be profiled as table `ANSWER_KEY` and sent to the planner LLM, leaking the answers into the run. Runs are safe because the review app always passes `--data-dir client_data/<client_id>`. Pointing a CLI run at `client_data/` itself also fails, because every client has an `LFA1.csv` and discovery refuses two files with the same table name.
- `.gitignore` lists `client_data/` and (commented) `episodic_memory.db`, but both **are already tracked in git**, so ignore rules don't apply to them. That is acceptable for synthetic data. Real client data must never be committed. Production would untrack both and keep client data outside the repo entirely.
- There is **no evaluation harness yet**. Nothing scores findings against the answer keys automatically, so recall and precision are measured by hand today.

### Configuration

`explorer_agent/config.py`'s `Config` class is the single place every module reads settings from. It layers two sources:
- **`config.yaml`** (project root) — non-secret, shareable defaults: LLM provider/model, data settings (default dictionary file name, per-client upload folder — tables are discovered from files, not listed), duplicate-matching rules, sandbox limits, memory/log/DB paths, cache thresholds, etc. Committed to version control.
- **`.env`** (gitignored) — secrets (`GEMINI_API_KEY`, etc.) and machine-specific values (e.g. `EXPLORER_LOCAL_LLM_MODEL_PATH`). Any `config.yaml` value can also be overridden per-machine by the matching environment variable (see comments in both files for exact names).

All relative paths (`EPISODIC_DB_PATH`, `LOG_DIR`, `MEMORY_BASE_DIR`) are resolved against `Config.PROJECT_ROOT`, not the process's current working directory, so behavior doesn't depend on which directory a script is launched from.

### LLM providers and fallbacks (`explorer_agent/llm_providers.py`)

`Config.LLM_PROVIDER` (`llm.provider` / `--llm-provider`: `google`, `groq` or `local`) is the primary backend; `Config.LLM_FALLBACK_PROVIDERS` (`llm.fallback_providers` / `--fallback-providers`) are tried in order after it. `build_llms()` expands this into an ordered model chain (Groq contributes one candidate per entry in `llm.groq.models`) and returns an `LLMBundle` whose planner / reflector / duplicate-rule runnables are `first.with_fallbacks(rest)`. Transient errors are retried inside each provider SDK (`llm.max_retries`, kept low so overloads fail over fast); malformed/missing structured output is retried on the same model (`llm.structured_output_retries`); any remaining failure moves to the next model. When all fail, `LLMChainExhaustedError` is raised: `main.py` skips that table (exit code 1) and `cache_runner.py` falls back to its rule-based heuristic. `metrics.llm_call_failures` / `llm_fallback_calls` count this. Groq models are per-entry dicts in `llm.groq.models` (`structured_output_method`, `reasoning_effort`, `max_tokens`) — `json_schema` is far more reliable than tool-calling for the large `CheckPlan` schema. `Config.validate()` only requires credentials for the primary provider; a fallback missing its key is skipped with a warning. Callers (`graph.py`, `cache_runner.py`) just call `.invoke()` and are unaware of the chain.

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

This design intentionally costs exactly 2 LLM calls per table regardless of column count (plus, only the first time a client's table layout is seen, one call to draft its duplicate-matching rules — see below) (contrast with an earlier per-column tool-calling design referenced in the module docstring).

Each `ProposedCheck` (`explorer_agent/schemas.py`) has two code fields: `code` (must set an AGGREGATE `result` — this is what gets sent to the reflector LLM) and optional `detail_code` (sets `detail_rows`, a list of offending rows for local human review only — **never sent to any LLM**).

### 4-pillar classification and duplicate governance

Every `ProposedCheck` and `FindingJudgment` (`explorer_agent/schemas.py`) also carries a classification, produced by the same planner/reflector LLM calls (no extra calls):

- **`category`** — `ACTIVENESS` / `DUPLICATE` / `COMPLETENESS` / `CORRECTNESS`, the four profiling pillars.
- **`rule_scope`** — `UNIVERSAL` / `INDUSTRY_SPECIFIC` / `CLIENT_SPECIFIC`, a 3-tier scope for how broadly a check generalizes (invalid values fall back to `UNIVERSAL`/`CORRECTNESS` via `field_validator`s, never raise).
- **`fix_type`** (`AUTO_FIXABLE` / `MANUAL_FIX`) + `auto_fix_value` — mainly for `COMPLETENESS` findings.
- **`is_anomaly`** — statistical outlier flag.
- **`duplicate_fields`** — for `DUPLICATE`-category checks, the fields used for match logic (e.g. `["NAME1", "PSTLZ", "STCD1"]`).

**Duplicate matching is deterministic; the rules it executes are written by the LLM once per client and schema.** `explorer_agent/duplicate_detector.py` does the matching for **any** table with zero LLM involvement, but nothing in it decides what a column means. That spec comes from `explorer_agent/duplicate_rule_planner.py`: ONE structured call (`DuplicateRulePlan`, `schemas.py`) that sees only metadata — column names, the client's data dictionary text, aggregate statistics and masked example values (`mask_value`, last two characters) — and gives every column a role (`KEY` / `IDENTIFIER` / `NAME` / `LOCATION` / `CONTEXT` / `IGNORE`, plus `identifier_group` for composites like bank country + key + account, `key_unique`, `label`, `rule_scope` and a one-sentence `reason` per column). `compile_plan` turns that into the same rules dict the detector has always executed (`key`/`key_unique`/`name`/`identifiers`/`location`/`display`/`label`/`why`/`checks`), dropping columns the model invented and rejecting any single-column identifier whose filled values are less than `duplicates.rules.min_identifier_distinct` distinct (arithmetic, not semantics: such a value covers many records — it catches a composite identifier whose parts were left ungrouped). The reasons land in `rules["why"]`, shown by "View Matching Rules" (`describe_rules`), so the review UI now shows the model's own justification per column.

Why the LLM and not Python: a (TABLE, COLUMN) pair does not mean the same thing at every company — standard fields get repurposed, Z-fields mean whatever their owner decided, and uploads may not be SAP at all. The previous regex/keyword inference in `duplicate_rules.py` assumed otherwise and was removed; that module is now only the resolver.

**Resolution order** (`duplicate_rules.resolve_rules`): `config.yaml` → `duplicates.tables.<TABLE>` (manual pin) → rules saved for **this client** whose schema signature matches → shared `UNIVERSAL`/`INDUSTRY_SPECIFIC` rules (only with `duplicates.rules.reuse_across_clients`, exact signature) → one LLM call, then saved. `explorer_agent/memory/duplicate_rule_store.py` keeps them as human-readable JSON: `memory_store/clients/<client_id>/duplicate_rules.json` and `memory_store/procedural/duplicate_rules.json`, keyed by a **schema signature** (table name + hash of its column set, order/case independent). So the first run for a client costs one call per table and every later run costs none; a table whose columns changed is re-drafted alone (even if the columns its rules use all survive — a new column may itself be worth matching on), the rest stay free. Cross-client reuse is off by default for the meaning-divergence reason above. `metrics.duplicate_rule_llm_calls/hits/misses` report this per run. If rules can't be produced (no LLM configured for `--duplicates-only`, or every model failed), the table falls back to **identical rows only** — comparing whole rows needs no business knowledge, so it cannot be wrong, whereas guessing roles in Python would be. Every table is checked for identical rows anyway. `main.py` builds the planner lazily (`RulePlanner`) so a run served entirely from memory never constructs an LLM, passes the run's dictionary and client, and runs detection for every table before the planner (its findings are saved even if the LLM chain fails). The planner prompt forbids DUPLICATE checks and `graph.py` discards any it proposes; the finding's `category` always comes from the planner's check, never the reflector's default (that override once stored a KOINH completeness check as a DUPLICATE finding). Matching: a shared identifier → `EXACT`; same normalized name + ≥2/1 matching location fields → `EXACT`/`PROBABLE`; fuzzy name ≥ `fuzzy_name_threshold` with a matching location field → `PROBABLE` (≥90%) / `SIMILAR`; same name elsewhere → `SIMILAR`. Guards: candidate pairs come from identifier/name/location blocking (no row-order window), names whose numbers differ never fuzzy-match, placeholder identifiers (`INVALID…`, `N/A`, `0000…`) are ignored, and any identifier or name shared by more than `max_identifier_share` keys is treated as a placeholder. Rows with the same business key (e.g. two LFBK accounts of one vendor) are never linked. `profiler_primitives.cluster_duplicates` (still injected into the sandbox, with `fuzzy_token_similarity` and `detect_distribution_outliers`) is a thin wrapper over the detector for old check code and cached skills.

**Clients and remembered decisions.** Every run belongs to a client (company): `--client` / the Run dialog's required field → `runs.client_id` (slug, e.g. `acme-retail`) + `client_name`. `explorer_agent/client_knowledge.py` keeps per-client knowledge as human-readable JSON under `memory.clients_dir` (`memory_store/clients/<client_id>/client.json`, `duplicate_decisions.json`, and `duplicate_rules.json` written by `memory/duplicate_rule_store.py`) — durable knowledge next to procedural memory, not per-run episodic state. Duplicate decisions are stored per record keyed by `record_id` = `<key>|<sha1 fingerprint of record_data>` with the partner record_ids it was grouped with. The review app writes them the moment a row/group verdict is saved (`_remember_duplicate_groups` in `review_app/main.py`; last action wins, `PENDING` removes). On the next run for that client, `duplicate_detector` skips a pair only when **both** records were marked `UNIQUE` against each other — reviewers mark `DUPLICATE` + `UNIQUE` to mean "this is the duplicate, that is the original", so a single `UNIQUE` must not hide a confirmed duplicate — and pre-fills any remembered verdict (`finding_items.decision_source = 'REMEMBERED'`, shown as ↺ in the UI) when a record is grouped with a former partner again. Because the fingerprint is part of the id, any change to a record's display fields makes its old decisions stop applying. Runs created before clients existed can be linked once via `POST /api/runs/{id}/client`, which also saves decisions already made in them; `/api/findings` and `/api/runs` accept `client_id`, `/api/clients` lists clients. The review dashboard is always scoped to exactly one client (there is no "all clients" view): page 2's `?client=` drives the runs list, findings, tab counts and status chips, and the client can only be changed on page 1 (see "Review app" below); the last client is kept in `localStorage` to preselect it on page 1.

Each duplicate row in `finding_items` carries `duplicate_group_id`, `similarity_score`, `match_type` (`EXACT`/`PROBABLE`/`SIMILAR`), `match_reasons`, and `record_data` (JSON of the table's `display` columns, used for the side-by-side comparison). Each row also tracks its own human review state, separate from the finding-level `PENDING`/`APPROVED`/`REJECTED` status: `review_verdict` (`PENDING`/`DUPLICATE`/`UNIQUE`/`TO_BE_CONFIRMED`). `is_golden_record`/`suggested_action` are legacy columns from a removed golden-record workflow, kept readable for old data.

The review app exposes this on top: `/api/findings/{id}/duplicate-groups` groups a finding's rows by `duplicate_group_id` (with parsed `record` data and per-group verdict counts); `/api/finding-items/{id}/verdict` records the human's `DUPLICATE`/`UNIQUE`/`TO_BE_CONFIRMED` call on a row (`PENDING` clears it); `/api/findings/{id}/duplicate-groups/{group_id}/verdict` applies one verdict to a whole group. The detail modal loads duplicate groups immediately (no click) as side-by-side tables with shared values highlighted, and finding cards show row review progress (`item_count`/`reviewed_count`/`group_count` from `get_findings_light`). `/api/findings` and `/api/stats` accept `category`/`rule_scope` filters for the classification breakdown, and `/api/finding-items/{id}/autofill` applies `auto_fix_value` for `AUTO_FIXABLE` `COMPLETENESS` rows.

### Cache fast path (`explorer_agent/cache_runner.py`)

Before invoking the planner for a column, `explore_table()` checks for existing approved skills for that exact table+column (`skill_registry.get_skills_for_table_column`). If found, the cached check code re-runs against fresh data and the planner LLM call is skipped entirely for that column (only the reflector may still run, unless `SKIP_REFLECTION_ON_CACHE_HIT` is set, in which case a rule-based heuristic replaces it). `explorer_agent/metrics.py` tracks LLM call counts and cache hit/miss counts per run to make these savings concrete.

### Privacy boundary

`explorer_agent/privacy_guard.py` (`sanitize_result_for_llm`) is the **last line of defense** before any locally-computed check result reaches an LLM: it withholds raw DataFrames/Series, masks string values (`profiler_primitives.mask_value`), and truncates long lists/strings. It is explicitly a best-effort heuristic, not a certified PII scanner. `explorer_agent/table_profiler.py` similarly uses an **allowlist** extraction from `ydata-profiling`'s raw JSON output — only explicitly named fields are pulled into the distilled profile sent to the planner LLM; anything not listed is dropped by default, since `value_counts_without_nan` etc. can contain raw values as dict keys even with `sensitive=True`.

`detail_code`/`detail_rows` output (row-level PII-bearing detail for confirmed findings) deliberately bypasses `privacy_guard` — it goes only to the local SQLite store (`finding_items` table) and the review UI, never to an LLM.

### Sandbox (`explorer_agent/sandbox.py`)

LLM-generated pandas code executes in a separate `multiprocessing` process (spawn context), with restricted builtins, a blocked-imports list (`os`, `sys`, `subprocess`, `socket`, etc.), and (Unix-only) CPU/memory rlimits plus a wall-clock timeout with forced termination. Documented as POC-grade isolation — "good enough to let an LLM experiment safely," not hardened against a determined adversary; harden further (containers/microVMs) before touching real client data.

### Review app (`review_app/`)

Two pages. **Page 1** (`static/index.html` + `setup.js`): pick a client from a typeahead of existing clients or "+ Add client" (created on its first upload), then upload that client's data dictionary and table CSVs. **Page 2** (`static/review.html?client=<client_id>` + `app.js`): the findings dashboard, fixed to that client — client, dictionary and tables are shown read-only in the header and the Run dialog; changing them means going back to page 1 ("Change client / data"). Missing/unknown `client` redirects to page 1.

**Client workspaces and dynamic tables.** Uploaded files live in `explorer_agent/client_workspace.py`'s per-client folder, `data.client_data_dir` (`client_data/<client_id>/`, gitignored raw client data, separate from `memory_store/` knowledge): the dictionary under its uploaded name, each table as `<TABLE>.csv`, and `workspace.json` metadata (rows, columns, upload time). Tables are not configured anywhere: the table name comes from the file name (`data_loader.table_name_from_filename`, `lfa1.csv` → `LFA1`) and `main.py` profiles every CSV under `--data-dir`, **searched recursively**, except the dictionary (`data_loader.discover_table_files`). A client can group tables by business object (`vendor-master/LFA1.csv`). The folder is presentation only: the table is still `LFA1`, and two files that map to the same table name abort the run. `client_workspace.save_table(..., subdir=)` and `seed_from_folder` preserve that layout. The loader reads every column as text and restores numeric measures using the dictionary's SAP data type (`apply_column_types`), because pandas inference strips the zero-padding from keys like LIFNR/MATNR. Uploads are the raw request body (`PUT /api/clients/{id}/dictionary?filename=…`, `PUT /api/clients/{id}/tables?filename=…`, no `python-multipart` dependency), streamed to a temp file, size-capped by `data.max_upload_mb`, then validated (CSV with rows; dictionary needs `Table`/`Field`/`Description`). Other endpoints: `POST /api/clients`, `GET /api/clients/{id}/workspace`, `DELETE /api/clients/{id}/tables/{table}`, `GET /api/dictionary?client_id=` (tooltips from that client's dictionary). `POST /api/jobs/run-explorer` takes only `client_id` plus LLM options — the server builds `--client/--data-dir/--dictionary-file` from the workspace and refuses clients without a dictionary and at least one table. Duplicate detection works for any uploaded table because its matching rules are drafted by the LLM from that client's dictionary and column statistics on first sight of a schema, then reused from that client's memory (see "4-pillar classification and duplicate governance").

FastAPI app (`review_app/main.py`) serving a JSON API over `episodic_store` plus the promotion pipeline, with `review_app/static/` (vanilla HTML/CSS/JS) mounted last so `/api/*` routes take precedence. Key endpoints: `/api/runs`, `/api/findings` (filterable by `category`/`rule_scope`), `/api/findings/{id}/decision` (human approve/reject — the gate that `promotion.py` reads from), `/api/promote`, `/api/skills`, `/api/stats` (counts by category/rule_scope), plus per-row endpoints for reviewing individual `detail_rows`: `/api/findings/{id}/items`, `/api/finding-items/{id}/decision`, `/api/finding-items/{id}/verdict` and `/api/finding-items/{id}/autofill` (the `COMPLETENESS` auto-fix workflow), and duplicate governance (`/api/findings/{id}/duplicate-groups`, `/api/findings/{id}/duplicate-groups/{group_id}/verdict`), plus the background run endpoints (`/api/jobs/run-explorer` — accepts `duplicates_only` — `/api/jobs/current`, `/api/jobs/{id}`, `/api/jobs/{id}/stop`) — see "4-pillar classification and duplicate governance" above.

### Local LLM support (`explorer_agent/local_llms.py`)

`QwenCoderGGUFChatModel` wraps a local GGUF model (via `llama-cpp-python`) as a LangChain `BaseChatModel`, emulating structured output/tool-calling through llama.cpp's grammar-constrained JSON decoding rather than prompt-based coaxing. It supports exactly one bound tool/schema at a time. Used when `local` appears in the provider chain built by `llm_providers.build_llms()`, alongside `ChatGoogleGenerativeAI` (Gemini) and `ChatGroq`. This module deliberately does NOT import `llama_cpp` at module level (it's imported unconditionally by `llm_providers.py`) — the `Llama` instance is constructed only inside `llm_providers._get_local_llm()`.

`review_app/job_manager.py` runs `python -m explorer_agent.main` as a child process and keeps a 1000-line log tail in memory. It allows **one run at a time**, because everything shares one SQLite file and possibly the local LLM singleton. Job state is lost when uvicorn restarts. (`review_app/_init__.py` is misnamed and has no effect. The package imports as a namespace package.)

## Invariants (do not break without an explicit decision)

1. **No raw data reaches an LLM.** Planner input is the allowlisted profile (`table_profiler.py`). Reflector input passes through `privacy_guard.sanitize_result_for_llm`. Duplicate-rule drafting sees only metadata plus `mask_value` samples. `detail_code`/`detail_rows` stay local. Any new LLM call site needs the same treatment. Adding a field to a prompt means adding it to an allowlist, never removing a filter.
2. **The LLM budget is bounded and independent of column count.** Each table costs 2 calls (planner + reflector), plus 1 duplicate-rule call the first time a client's schema is seen. Cache hits cost less. A feature that adds per-column or per-row LLM calls needs a stated reason and must show up in `metrics.py`.
3. **Deterministic where business meaning isn't required.** Matching, execution and extraction are plain Python. The LLM decides only *what a column means* and *what to check*, and those decisions are persisted (duplicate rules, skills) so later runs are free and auditable.
4. **Human gate before reuse.** Nothing becomes a skill without `APPROVED` status plus `reusable` plus `check_code` (`promotion.py`). Remembered duplicate decisions are per client, keyed by record fingerprint.
5. **Procedural JSON is the source of truth; Chroma is derived.** Only `chroma_store.py` imports `chromadb`.
6. **LLM-generated code runs only in `sandbox.py`**, never through `exec` in the main process.
7. **Configuration goes through `Config`.** Non-secrets live in `config.yaml` with an env override. Secrets live only in `.env`. Never hard-code paths or keys.

## POC vs production

This lists what is POC-grade today and what production would need. Use it to name gaps precisely. Don't close them unprompted.

| Area | Today (POC, verified in code) | Required before real client data / production |
|---|---|---|
| **Sandbox** | `multiprocessing` spawn, restricted builtins, import blocklist, wall-clock timeout. `resource` rlimits are Unix-only, so **on Windows (the dev machine) only the timeout applies**, with no memory or CPU cap. | Container or microVM isolation (gVisor/Firecracker), no network, read-only FS, enforced memory/CPU limits. Consider replacing free-form code with a check DSL or a vetted primitive library. |
| **Privacy** | Heuristic masking plus allowlisted profiles. Masked samples and the client dictionary are sent to hosted LLMs (Gemini/Groq). | A DPA and data-residency review per provider (or a local/VPC-hosted model), a real PII classifier, per-client opt-in to hosted LLMs, prompt/response audit logging, retention policy. |
| **Data at rest** | Raw client CSVs in `client_data/`, row-level PII in `finding_items` (SQLite). Both are **committed to git** (synthetic only). | Untrack from git. Encrypted storage outside the repo, per-client access control, retention and deletion (right-to-erasure) for `finding_items`/`record_data`. |
| **Review app security** | No authentication or authorization, CORS `*`, uploads capped by size only. Reviewer identity is not recorded on decisions. | SSO/OIDC, role-based access per client, reviewer identity plus an audit trail on every approve/reject/verdict (this is the governance gate), CSRF/CORS tightening, upload content scanning. |
| **Reliability** | Provider fallback chain, structured-output retries, failed tables skipped with exit code 1. One run at a time. Job state is in memory. | A durable job queue (worker plus retries plus idempotent runs), resumable per-table progress, Postgres instead of SQLite for concurrent writers, pinned model versions. |
| **Evaluation** | Answer keys exist per synthetic client, but there is **no scoring harness**. Keys are kept out of runs only by where they are stored (see "Data in this repo"). | A harness that scores findings and duplicate groups against answer keys (precision/recall per pillar and issue type), run on every prompt, model or rule change. Regression gate in CI. Track reviewer-approval rate as an online quality signal. |
| **Observability** | Python logging to console plus a rotating `logs/` file. `RunMetrics` counts LLM calls and cache hits per process. `runs.model` records the primary model only. | Structured logs with run/table/finding correlation IDs, LLM tracing (prompt/version/model/tokens/latency/cost per call), recording which fallback model actually answered, dashboards for failure and fallback rates. |
| **Maintainability** | No tests, linter or type checking. Additive `ALTER TABLE` migrations in `episodic_store._migrate`. Legacy fields remain (`max_iterations_per_column`, golden-record columns). | pytest around deterministic parts first (duplicate detector, rule compiler, loader typing, privacy guard). Ruff plus mypy, versioned migrations (Alembic), prompt versioning. |
| **Deployment** | Local `uvicorn --reload`, `.venv`, Windows-specific llama-cpp wheel. | A container image, config per environment, a secrets manager instead of `.env`, CI/CD, and a separate worker from the API. |

When a task touches one of these rows, prefer the POC-sized step that doesn't block the production path. Example: add a `reviewer` column now even without SSO, rather than building SSO.
