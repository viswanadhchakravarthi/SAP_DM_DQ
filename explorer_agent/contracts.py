"""Handoff contracts between the Data Profiling Agent and the agents around it.

    Extract -> STRUCTURAL PROFILE -> Mapping Agent -> FIELD/VALUE MAPPING -> semantic profiling -> Cleansing
               (this agent, out)                     (this agent, in)

Two versioned JSON documents, defined here as pydantic models so both sides can
validate them. ``python -m explorer_agent.contracts`` writes the JSON Schema
files to ``explorer_agent/contract_schemas/`` for the other team.

1. ``sap-dm.structural-profile`` (OUT, to the Mapping / Value Mapping Agent):
   per source column, what the data looks like - detected type, lengths, value
   patterns with a regex, null/distinct statistics, and for categorical code
   columns the complete list of distinct values with counts. Needs no knowledge
   of what a column means. Values of personal or identifying columns are never
   listed, only their shapes and statistics (see ``Privacy``).

2. ``sap-dm.field-value-mapping`` (IN, from the Mapping Agent): source column ->
   one or MORE SAP target table.fields (a denormalized legacy vendor file feeds
   LFA1 + LFB1 + LFM1), each candidate with a 0-100 confidence score, tier and
   explanation, and source value -> target value per target field. The Mapping
   Agent sends ALL candidates; this agent applies ``handoff.min_confidence``.
   When present, the profiling rules use it instead of guessing what a column
   means (column_mapping.py), and check values after value mapping.

3. ``sap-dm.target-domains`` (IN, from the Metadata Repository): the allowed
   values of SAP target fields (check tables: T052 payment terms, T077K account
   groups, ...). A value outside its target domain is an INVALID_CODE finding,
   and check-table validity overrides the statistical rare-code check. Domain
   values in the structural profile are marked VALID / MAPPED / UNMAPPED, so the
   Value Mapping Agent sees exactly what still needs a conversion.

4. ``sap-dm.pipeline-event`` (OUT): ``profiling.completed`` when a run ends,
   appended to an outbox (``handoff/events.jsonl``) and served over REST. A
   message queue can consume the same events in production without changing them.

Versioning: ``version`` is MAJOR.MINOR. A consumer must reject an unknown MAJOR
and ignore fields it does not know (new MINOR versions only add optional fields).
"""

import json
from pathlib import Path
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

STRUCTURAL_PROFILE = "sap-dm.structural-profile"
FIELD_VALUE_MAPPING = "sap-dm.field-value-mapping"
TARGET_DOMAINS = "sap-dm.target-domains"
PIPELINE_EVENT = "sap-dm.pipeline-event"
CONTRACT_VERSION = "1.0"   # draft - revised in place after the 2026-09-24 review, no consumer yet

ValueClass = Literal["CATEGORICAL", "FLAG", "IDENTIFIER", "FREE_TEXT", "NUMERIC", "DATE", "EMPTY"]
Sensitivity = Literal["NONE", "PERSONAL"]
DetectedType = Literal["string", "integer", "decimal", "date", "empty"]
MappingStatus = Literal["APPROVED", "PROPOSED", "REJECTED"]
# Scoping document section 14: 90-100 bulk-approvable, 70-89 functional review,
# 40-69 business confirmation, 0-39 manual analysis.
ConfidenceTier = Literal["HIGH", "MEDIUM", "LOW", "VERY_LOW"]
TargetStatus = Literal["VALID", "MAPPED", "UNMAPPED"]


def confidence_tier(score: Optional[float]) -> Optional[str]:
    if score is None:
        return None
    return "HIGH" if score >= 90 else "MEDIUM" if score >= 70 else "LOW" if score >= 40 else "VERY_LOW"


class _Open(BaseModel):
    """Consumers ignore fields they don't know (forward-compatible MINOR versions)."""
    model_config = ConfigDict(extra="allow")


# ---------------------------------------------------------------------------
# 1. Structural profile (OUT)
# ---------------------------------------------------------------------------

class Producer(_Open):
    agent: str = Field(description="Producing agent, e.g. 'data-profiling-agent'")
    run_id: Optional[str] = Field(default=None, description="Producer's run id (traceability)")
    client_id: str = Field(description="Client (company) slug, e.g. 'acme-retail'")
    client_name: Optional[str] = None


class Privacy(_Open):
    domain_policy: str = Field(description="Which columns list their distinct values, in words")
    max_domain_values: int = Field(description="A column with more distinct values than this lists none")
    withheld_classes: List[str] = Field(description="Value classes / concepts whose values are never listed")


class DictionaryEntry(_Open):
    description: Optional[str] = None
    data_type: Optional[str] = Field(default=None, description="As declared in the client's data dictionary, e.g. CHAR10")
    notes: Optional[str] = None


class ColumnStats(_Open):
    rows: int
    filled: int = Field(description="Non-blank values")
    null_pct: float = Field(description="Blank or missing, % of rows")
    distinct: int = Field(description="Distinct non-blank values")
    distinct_pct: float = Field(description="Distinct as % of filled values (100 = every value different)")
    unique: bool = Field(description="Filled in every row and no value repeats - a single-column key candidate")
    min_length: Optional[int] = None
    max_length: Optional[int] = Field(default=None, description="Longest value in characters (for target field length checks)")
    avg_length: Optional[float] = None
    leading_zeros: bool = Field(default=False, description="Some numeric-looking values have leading zeros (a code, not a number)")
    min: Optional[str] = Field(default=None, description="NUMERIC / DATE columns only, as text")
    max: Optional[str] = Field(default=None, description="NUMERIC / DATE columns only, as text")
    decimals: Optional[int] = Field(default=None, description="NUMERIC: most decimal places seen")


class Pattern(_Open):
    shape: str = Field(description="Value shape: A = upper-case letter, a = lower-case, 9 = digit, other characters "
                                   "literal, runs compressed, e.g. 'A{2}9{9}' for 'DE123456789'")
    regex: str = Field(description="Anchored regex for the shape")
    count: int
    pct: float = Field(description="% of filled values with this shape")


class DomainValue(_Open):
    value: str
    count: int
    pct: float = Field(description="% of filled values")
    target_status: Optional[TargetStatus] = Field(default=None, description=(
        "Only when the column's SAP target domain is known (sap-dm.target-domains): VALID = the value exists "
        "in the target check table; MAPPED = a value mapping converts it to a valid target value; UNMAPPED = "
        "neither - the Value Mapping Agent still has to convert it"))
    target_value: Optional[str] = Field(default=None, description="MAPPED: the target value it converts to")


class Domain(_Open):
    listed: bool = Field(description="True when `values` holds every distinct value of the column")
    values: List[DomainValue] = []
    withheld_reason: Optional[str] = Field(default=None, description="Why the values are not listed, when listed=false")
    target: Optional[str] = Field(default=None, description="SAP TABLE.FIELD whose domain the values were checked against")
    check_table: Optional[str] = Field(default=None, description="SAP check table of that domain, e.g. T052")
    unmapped_count: Optional[int] = Field(default=None, description="Distinct values with target_status UNMAPPED")


class SemanticHint(_Open):
    """This agent's own reading of the column, if it has one - a hint, not a mapping."""
    concept: str = Field(description="Business concept, e.g. COUNTRY, TAX_ID (schemas.ColumnConcept)")
    sap_field: Optional[str] = Field(default=None, description="TABLE.FIELD when the column is SAP-standard or mapped")
    source: str = Field(description="sap-standard | mapping-agent | llm | client-memory | ...")


class ProfiledColumn(_Open):
    column: str
    position: int = Field(description="1-based column order in the source file")
    dictionary: Optional[DictionaryEntry] = None
    detected_type: DetectedType
    value_class: ValueClass = Field(description="CATEGORICAL: a small repeating set of codes; FLAG: 1-3 short values; "
                                                "IDENTIFIER: (nearly) every value different; FREE_TEXT: names, "
                                                "descriptions, addresses; NUMERIC; DATE; EMPTY")
    sensitivity: Sensitivity = Field(description="PERSONAL: names, addresses, contact data, tax or bank numbers - "
                                                 "never listed, only shapes and statistics")
    stats: ColumnStats
    patterns: List[Pattern] = Field(description="Most frequent value shapes, at most 5")
    regex: Optional[str] = Field(default=None, description="One regex covering >= 95% of filled values (alternation of "
                                                           "the top shapes), or null when the column has no dominant format")
    regex_coverage_pct: Optional[float] = None
    domain: Domain
    semantic_hint: Optional[SemanticHint] = None


class ProfiledTable(_Open):
    table: str = Field(description="Table name as uploaded (file name without .csv, upper-cased)")
    source_file: str
    row_count: int
    column_count: int
    key_candidates: List[List[str]] = Field(description="Single columns that are filled and unique in every row")
    declared_key: Optional[List[str]] = Field(default=None, description="The row key from this agent's column mapping")
    declared_key_unique: Optional[bool] = None
    columns: List[ProfiledColumn]


class StructuralProfile(_Open):
    contract: Literal["sap-dm.structural-profile"] = STRUCTURAL_PROFILE
    version: str = CONTRACT_VERSION
    generated_at: str = Field(description="ISO-8601 UTC")
    producer: Producer
    privacy: Privacy
    tables: List[ProfiledTable]


# ---------------------------------------------------------------------------
# 2. Field / value mapping (IN)
# ---------------------------------------------------------------------------

class FieldTarget(_Open):
    table: str = Field(description="SAP table, e.g. LFA1")
    field: str = Field(description="SAP field, e.g. LIFNR")
    is_primary_key: bool = Field(default=False, description="The field identifies the target table's record "
                                                            "(e.g. LFA1-LIFNR for a vendor id)")
    is_foreign_key: bool = Field(default=False, description="The field points to another target table's record "
                                                            "(e.g. LFB1-LIFNR -> LFA1)")
    confidence_score: Optional[float] = Field(default=None, ge=0, le=100, description="Overrides the mapping's "
                                                                                      "score for this target")
    explanation: Optional[str] = None


class FieldMapping(_Open):
    """One source column and ALL its candidate SAP targets (1-to-many for denormalized legacy files)."""
    source_table: str
    source_column: str
    targets: List[FieldTarget] = Field(min_length=1, description=(
        "Every SAP field this source column feeds, e.g. VENDOR_ID -> LFA1-LIFNR (primary key), LFB1-LIFNR and "
        "LFM1-LIFNR (foreign keys). The primary-key target, else the first, decides the column's meaning here."))
    confidence_score: Optional[float] = Field(default=None, ge=0, le=100, description="0-100 (section 14)")
    confidence_tier: Optional[ConfidenceTier] = Field(default=None, description="Derived from the score when omitted")
    explanation: Optional[str] = Field(default=None, description="Why the Mapping Agent proposes this mapping")
    status: MappingStatus = "PROPOSED"


class ValueMapping(_Open):
    target_table: str
    target_field: str
    source_value: str
    target_value: str = Field(description="Value in the SAP target domain, e.g. GB for source value UK")
    source_table: Optional[str] = Field(default=None, description="Restrict to one source table (optional)")
    source_column: Optional[str] = Field(default=None, description="Restrict to one source column (optional)")
    status: MappingStatus = "PROPOSED"


class FieldValueMapping(_Open):
    """The Mapping Agent sends ALL candidates (low confidence included, with explanations);
    this agent decides what to use with ``handoff.min_confidence`` - dropping them upstream
    would hide them from the consultant (scoping document sections 13, 14, 26)."""
    contract: Literal["sap-dm.field-value-mapping"] = FIELD_VALUE_MAPPING
    version: str = CONTRACT_VERSION
    generated_at: Optional[str] = None
    producer: Producer
    field_mappings: List[FieldMapping] = []
    value_mappings: List[ValueMapping] = []


# ---------------------------------------------------------------------------
# 3. Target domains (IN, from the Metadata Repository)
# ---------------------------------------------------------------------------

class AllowedValue(_Open):
    value: str
    description: Optional[str] = None


class TargetDomain(_Open):
    target_table: str = Field(description="SAP table, e.g. LFB1")
    target_field: str = Field(description="SAP field, e.g. ZTERM")
    check_table: Optional[str] = Field(default=None, description="SAP check table, e.g. T052")
    values: List[AllowedValue] = Field(description="Every allowed value in the client's target system")


class TargetDomains(_Open):
    contract: Literal["sap-dm.target-domains"] = TARGET_DOMAINS
    version: str = CONTRACT_VERSION
    generated_at: Optional[str] = None
    producer: Producer
    domains: List[TargetDomain]


# ---------------------------------------------------------------------------
# 4. Pipeline events (OUT)
# ---------------------------------------------------------------------------

class PipelineEvent(_Open):
    contract: Literal["sap-dm.pipeline-event"] = PIPELINE_EVENT
    version: str = CONTRACT_VERSION
    event_id: str
    type: str = Field(description="e.g. profiling.completed")
    occurred_at: str
    producer: Producer
    status: Literal["COMPLETED", "PARTIAL", "FAILED"] = Field(description="PARTIAL: some tables' LLM steps failed")
    payload: dict = Field(default_factory=dict, description="Type-specific: run summary and links to the outputs")


def check_version(version: str, contract: str) -> None:
    major = str(version).split(".")[0]
    if major != CONTRACT_VERSION.split(".")[0]:
        raise ValueError(f"{contract} version {version} is not supported (this agent speaks {CONTRACT_VERSION})")


def export_schemas(out_dir: Optional[Path] = None) -> List[Path]:
    out_dir = out_dir or Path(__file__).with_name("contract_schemas")
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, model in ((f"{STRUCTURAL_PROFILE}.v1.schema.json", StructuralProfile),
                        (f"{FIELD_VALUE_MAPPING}.v1.schema.json", FieldValueMapping),
                        (f"{TARGET_DOMAINS}.v1.schema.json", TargetDomains),
                        (f"{PIPELINE_EVENT}.v1.schema.json", PipelineEvent)):
        path = out_dir / name
        path.write_text(json.dumps(model.model_json_schema(), indent=2), encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    for p in export_schemas():
        print(p)
