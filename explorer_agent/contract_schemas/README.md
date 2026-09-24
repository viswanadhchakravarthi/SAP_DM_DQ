# Handoff contracts: Data Profiling Agent ⇄ Mapping Agent / Metadata Repository

**Status: v1.0 draft, revised on 2026-09-24 after the review answers below.** No consumer existed yet, so the
changes (e.g. `targets[]` replacing a single target) are made in place instead of bumping the major version.

```
Extract ─► structural profile ─► Mapping Agent ─► field/value mapping ─► DQ rules (profiling) ─► Cleansing
            (profiling → mapping)                  (mapping → profiling)       ▲
                                                   Metadata Repository ─► target domains (check tables)
            profiling.completed event ─► orchestrator / next agent
```

Structural profiling needs no knowledge of what a column means, so it runs before mapping and is the input
the Mapping Agent maps from. The data-quality rules need that meaning, so they run after mapping and read
the Mapping Agent's output and the target check tables.

| Contract | Direction | Schema | Example |
|---|---|---|---|
| `sap-dm.structural-profile` | Profiling → Mapping / Value Mapping | [schema](sap-dm.structural-profile.v1.schema.json) | [example](examples/structural_profile.example.json) |
| `sap-dm.field-value-mapping` | Mapping → Profiling | [schema](sap-dm.field-value-mapping.v1.schema.json) | [example](examples/field_value_mapping.example.json) |
| `sap-dm.target-domains` | Metadata Repository → Profiling | [schema](sap-dm.target-domains.v1.schema.json) | [example](examples/target_domains.example.json) |
| `sap-dm.pipeline-event` | Profiling → orchestrator / next agent | [schema](sap-dm.pipeline-event.v1.schema.json) | [example](examples/pipeline_event.example.json) |

The pydantic models in [`explorer_agent/contracts.py`](../contracts.py) are the source of truth.
`python -m explorer_agent.contracts` regenerates the schema files.

**Versioning:** `version` is `MAJOR.MINOR`. A consumer **rejects an unknown MAJOR** and **ignores fields it does
not know**. A new MINOR version only adds optional fields.

## Review decisions (2026-09-24)

| # | Question | Decision | Implemented as |
|---|---|---|---|
| 1 | Multiple targets per source column? | **Yes, 1-to-many.** Denormalized legacy files feed LFA1 + LFB1 + LFM1 | `field_mappings[].targets[]` with `is_primary_key` / `is_foreign_key` |
| 2 | Receive SAP check-table values? | **Yes**, from the Metadata Repository. The check table overrides statistical rarity. Unmapped values go to value mapping | `sap-dm.target-domains`; `INVALID_CODE` findings; `domain.values[].target_status` |
| 3 | Who applies the confidence threshold? | **This side.** The Mapping Agent sends **all** candidates with a 0–100 score and an explanation | `confidence_score`, `confidence_tier`, `explanation`; `handoff.min_confidence` (default 90) |
| 4 | Transport | **REST with async job IDs** for the MVP; a queue in production | `POST /api/jobs/run-profiling` → `job_id`; `profiling.completed` events in an outbox |

## 1. `sap-dm.structural-profile` (profiling → mapping)

One document per profiling run, covering every uploaded table and column.

- **Where:** `GET /api/clients/{client_id}/handoff/structural-profile` (latest) or `?run_id=` for a specific run.
  The link is also in the job's `outputs` and in the `profiling.completed` event.
- **Per table:** `row_count`, `key_candidates` (columns that are filled and unique in every row), and
  `declared_key` with `declared_key_unique`.
- **Per column:**

| Field | Use for |
|---|---|
| `dictionary` | The client's description and declared type (e.g. `CHAR10`) |
| `detected_type` | `string` / `integer` / `decimal` / `date` / `empty`. Zero-padded numbers are `string` with `stats.leading_zeros=true` because they are codes |
| `stats.max_length`, `min_length`, `avg_length`, `null_pct`, `distinct(_pct)`, `unique`, `min`/`max` | Length fit, mandatory and key checks, ranges |
| `patterns[]`, `regex`, `regex_coverage_pct` | Top 5 value shapes (`A` upper-case, `a` lower-case, `9` digit, `L` other letter) and one regex covering ≥95% of values. A confidence signal for field mapping |
| `value_class`, `sensitivity` | `CATEGORICAL` · `FLAG` · `IDENTIFIER` · `FREE_TEXT` · `NUMERIC` · `DATE` · `EMPTY`; `PERSONAL` or `NONE` |
| `domain` | Every distinct value with its count (non-personal code columns only). When the SAP target domain is known, each value carries a **`target_status`**: `VALID` (in the check table), `MAPPED` (a value mapping converts it, see `target_value`) or `UNMAPPED`, plus the `target`, `check_table` and `unmapped_count`. **`UNMAPPED` values are the Value Mapping Agent's worklist** |
| `semantic_hint` | Optional. This agent's reading of the column (`concept`, `sap_field`, all `sap_fields` for 1-to-many, `source`) |

**Privacy.** Distinct values are listed only for `CATEGORICAL` / `FLAG` columns with `sensitivity=NONE`, such as
country, language, currency, payment terms, incoterms, account groups, reconciliation accounts, company codes
and flags. The following carry shapes and statistics only:

- names, addresses, e-mail, phone, tax numbers;
- identifiers (≥90% distinct: vendor numbers, bank accounts, IBANs);
- free text, numbers and dates.

The same applies to any column with more than `handoff.max_domain_values` (200) distinct values. This file may be
sent to an LLM downstream, which is why the rules are this strict.

## 2. `sap-dm.field-value-mapping` (mapping → profiling)

- **Where:** `PUT /api/clients/{client_id}/handoff/field-mapping` (body = the JSON). It is validated and
  stored as `client_data/<client_id>/field_mapping.json`, and the next run uses it (CLI: `--mapping-file`).
- **`field_mappings[]`:** one entry per **source column** with **all** its candidate SAP `targets[]`
  (`table`, `field`, `is_primary_key`, `is_foreign_key`, and an optional per-target `confidence_score` and
  `explanation`). Each entry also has a `confidence_score` (0–100), a `confidence_tier` (derived when
  omitted: 90–100 `HIGH`, 70–89 `MEDIUM`, 40–69 `LOW`, 0–39 `VERY_LOW`), an `explanation` and a `status`.
- **Send everything.** Low-confidence candidates must not be dropped upstream: a consultant who never sees
  them assumes the column was missed. This agent applies the threshold. `APPROVED` always counts,
  `REJECTED` never does, and `PROPOSED` counts from `handoff.min_confidence` (default 90 = `HIGH`). Candidates
  below it are listed in the run log and not used by the rules.
- **Multiple targets.** The primary-key target, else the first, decides what the column means for the rules.
  A column is mandatory if any of its targets is. A foreign-key target that points back into the same source
  file (`LFB1-LIFNR → LFA1-LIFNR`, both from `VENDOR_ID`) is the same record, not a link, so no orphan check
  runs against itself.
- **`value_mappings[]`:** `source_value → target_value` for a `target_table.target_field` (optionally one
  source column). The rules check values after mapping: `UK` → `GB` is a valid country, and a legacy `Y`
  flag → `X` counts as set.

## 3. `sap-dm.target-domains` (Metadata Repository → profiling)

- **Where:** `PUT /api/clients/{client_id}/handoff/target-domains`, stored as `target_domains.json` next to
  the client's data (CLI: `--target-domains-file`).
- **`domains[]`:** `target_table`, `target_field`, `check_table` (e.g. `T052`), and every allowed `values[]`
  entry (`value`, optional `description`).
- **Effect:**
  - A column mapped to a field with a domain gets an **`INVALID_CODE`** finding for every value outside it,
    checked after value mapping.
  - For that column the domain **replaces** the statistical rare-code check: a valid `Z090` used twice is fine,
    and a bogus `XXXX` used often is still caught.
  - Without a domain, the rare-code check remains the fallback.
- **Measured on the synthetic acme client:** with a T052 payment-terms domain, all 14 seeded "Invalid Payment
  Terms" were found. The rare-code check alone found none of them.

## 4. `sap-dm.pipeline-event` and the job API (profiling → orchestrator)

- **Start a run:** `POST /api/jobs/run-profiling` (same body as the UI's `/run-explorer`) returns a `job_id`
  at once.
- **Follow it:** `GET /api/jobs/{job_id}` returns `status` (`RUNNING`/`COMPLETED`/`FAILED`/`STOPPED`), `run_id`
  and `outputs` (`structural_profile_url`, `events_url`).
- **Completion event:** every run appends a `profiling.completed` event to the outbox. Its `status` is
  `COMPLETED`, or `PARTIAL` when some tables' LLM steps failed, and its `payload` holds the tables, finding
  count, failed tables, profile path/URL, duplicate-decision export URL and which inputs were used. Poll with
  `GET /api/handoff/events?client_id=&type=profiling.completed&after=<last event_id>`.
- **Production:** keep the same event documents and the same job API, and put Celery + Redis / RabbitMQ /
  Kafka behind `events.publish` and the job runner. Long profiling runs then never hold an HTTP request
  open, and agents stay decoupled.
