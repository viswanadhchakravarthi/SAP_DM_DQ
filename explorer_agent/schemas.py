from typing import List, Literal, Optional
from pydantic import BaseModel, Field, field_validator

# Shared by Reflection (single-check judgment) and FindingJudgment (one
# judgment within a batch) below, so the severity/confidence vocabulary
# only has to be defined once.
Severity = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
Confidence = Literal["HIGH", "MEDIUM", "LOW"]
Category = Literal["ACTIVENESS", "DUPLICATE", "COMPLETENESS", "CORRECTNESS"]
RuleScope = Literal["UNIVERSAL", "INDUSTRY_SPECIFIC", "CLIENT_SPECIFIC"]
FixType = Literal["AUTO_FIXABLE", "MANUAL_FIX"]
# For CORRECTNESS checks only: a single wrong field value vs. a cross-table/
# referential-integrity mismatch - the review app shows different correction
# controls for each (a "corrected value" field makes no sense for the latter).
SubType = Literal["VALUE_ERROR", "RELATIONSHIP_INTEGRITY"]

class Reflection(BaseModel):
    """Structured judgment of a single data-quality check's result."""
    is_issue: bool = Field(description="True if this reveals a genuine data quality issue")
    severity: Severity
    confidence: Confidence
    summary: str = Field(description="One-line human-readable description of the finding")
    reusable: bool = Field(description="True if this check would likely generalize to similar datasets/clients")
    category: Optional[Category] = Field(default="CORRECTNESS", description="One of: ACTIVENESS, DUPLICATE, COMPLETENESS, CORRECTNESS")
    rule_scope: Optional[RuleScope] = Field(default="UNIVERSAL", description="One of: UNIVERSAL, INDUSTRY_SPECIFIC, CLIENT_SPECIFIC")
    industry: Optional[str] = Field(default=None, description="Industry name if rule_scope is INDUSTRY_SPECIFIC")
    fix_type: Optional[FixType] = Field(default=None, description="AUTO_FIXABLE or MANUAL_FIX (especially for completeness)")
    auto_fix_value: Optional[str] = Field(default=None, description="Recommended default value if AUTO_FIXABLE")
    is_anomaly: Optional[bool] = Field(default=False, description="True if this finding is a statistical outlier/anomaly")
    sub_type: Optional[SubType] = Field(default=None, description="For CORRECTNESS only: VALUE_ERROR (single bad value) or RELATIONSHIP_INTEGRITY (cross-table/referential mismatch)")

class ProposedCheck(BaseModel):
    column: str = Field(description="Primary column this check targets")
    hypothesis: str = Field(description="What is being tested and why")
    category: Category = Field(default="CORRECTNESS", description="One of: ACTIVENESS, DUPLICATE, COMPLETENESS, CORRECTNESS")
    rule_scope: RuleScope = Field(default="UNIVERSAL", description="One of: UNIVERSAL, INDUSTRY_SPECIFIC, CLIENT_SPECIFIC")
    industry: Optional[str] = Field(default=None, description="Industry name if rule_scope is INDUSTRY_SPECIFIC (e.g. Banking, Healthcare, Manufacturing)")
    fix_type: Optional[FixType] = Field(default=None, description="AUTO_FIXABLE or MANUAL_FIX (mainly for completeness)")
    auto_fix_value: Optional[str] = Field(default=None, description="Recommended default replacement value if AUTO_FIXABLE")
    is_anomaly: bool = Field(default=False, description="True if this check detects statistical distribution outliers")
    duplicate_fields: Optional[List[str]] = Field(default=None, description="Fields evaluated for duplicate matching (e.g. ['NAME1', 'PSTLZ', 'STCD1'])")
    sub_type: Optional[SubType] = Field(default=None, description="For CORRECTNESS checks only: VALUE_ERROR (single bad value) or RELATIONSHIP_INTEGRITY (cross-table/referential mismatch, e.g. a vendor missing from the general vendor master)")
    code: str = Field(description="Pandas code; must set `result` to an AGGREGATE value "
                                  "(count/pct/bool/small dict) - sent to LLM for reflection.")
    detail_code: Optional[str] = Field(default=None, description=
        "OPTIONAL pandas code that sets `result` to a list of dicts, one per offending "
        "row, each with keys: row_index (int), key_field (str, name of a natural key column "
        "like LIFNR), key_value (that row's key value), issue_detail (str, what's wrong with "
        "THIS row). This is for LOCAL HUMAN REVIEW ONLY - it is NEVER sent back to the LLM. "
        "Keep detail_rows to a reasonable number of rows (e.g. cap at ~50) if many rows match.")
    compare_table: Optional[str] = Field(default=None,
        description="Optional; another registered table name if this is a cross-table check")

    @field_validator("compare_table", "detail_code", "industry", "auto_fix_value")
    @classmethod
    def normalize_none_string(cls, v):
        if v in (None, "None", "null", ""):
            return None
        return v

    @field_validator("category", mode="before")
    @classmethod
    def normalize_category(cls, v):
        if not v or v not in ("ACTIVENESS", "DUPLICATE", "COMPLETENESS", "CORRECTNESS"):
            return "CORRECTNESS"
        return v

    @field_validator("rule_scope", mode="before")
    @classmethod
    def normalize_rule_scope(cls, v):
        if not v or v not in ("UNIVERSAL", "INDUSTRY_SPECIFIC", "CLIENT_SPECIFIC"):
            return "UNIVERSAL"
        return v

    @field_validator("sub_type", mode="before")
    @classmethod
    def normalize_sub_type(cls, v):
        if v not in ("VALUE_ERROR", "RELATIONSHIP_INTEGRITY"):
            return None
        return v

    # Runs after normalize_none_string. Rejecting uncompilable code here turns
    # it into a ValidationError, which llm_providers retries on the same model
    # (then falls back) - instead of the plan being accepted and the code
    # failing later in the sandbox. Typical cause: a small model emitting an
    # unescaped `"` under grammar-constrained JSON, which ends the string early.
    @field_validator("code", "detail_code")
    @classmethod
    def must_compile(cls, v, info):
        if v is None:
            return v
        try:
            compile(v, f"<{info.field_name}>", "exec")
        except SyntaxError as e:
            raise ValueError(f"{info.field_name} is not valid Python: {e.msg} (line {e.lineno})") from e
        return v

class CheckPlan(BaseModel):
    checks: List[ProposedCheck]

class FindingJudgment(BaseModel):
    check_index: int = Field(description="Index (0-based) matching the input checks list order")
    is_issue: bool
    severity: Severity
    confidence: Confidence
    summary: str
    reusable: bool
    category: Optional[Category] = Field(default="CORRECTNESS")
    rule_scope: Optional[RuleScope] = Field(default="UNIVERSAL")
    industry: Optional[str] = Field(default=None)
    fix_type: Optional[FixType] = Field(default=None)
    auto_fix_value: Optional[str] = Field(default=None)
    is_anomaly: Optional[bool] = Field(default=False)
    sub_type: Optional[SubType] = Field(default=None)

    @field_validator("category", mode="before")
    @classmethod
    def normalize_category(cls, v):
        if not v or v not in ("ACTIVENESS", "DUPLICATE", "COMPLETENESS", "CORRECTNESS"):
            return "CORRECTNESS"
        return v

    @field_validator("rule_scope", mode="before")
    @classmethod
    def normalize_rule_scope(cls, v):
        if not v or v not in ("UNIVERSAL", "INDUSTRY_SPECIFIC", "CLIENT_SPECIFIC"):
            return "UNIVERSAL"
        return v

    @field_validator("sub_type", mode="before")
    @classmethod
    def normalize_sub_type(cls, v):
        if v not in ("VALUE_ERROR", "RELATIONSHIP_INTEGRITY"):
            return None
        return v

class ReflectionBatch(BaseModel):
    judgments: List[FindingJudgment]


# ---------------------------------------------------------------------------
# Duplicate matching rules (see duplicate_rule_planner.py)
# ---------------------------------------------------------------------------
# The LLM plays the analyst ONCE per client+schema: it reads the client's data
# dictionary and privacy-sanitized column statistics and assigns a role to each
# column. The deterministic engine (duplicate_detector.py) then executes that
# spec on every later run with no LLM call at all. Roles are business concepts,
# not SAP field names, so the same schema works for any uploaded table.
DuplicateRole = Literal["KEY", "IDENTIFIER", "NAME", "LOCATION", "CONTEXT", "IGNORE"]


class ColumnRule(BaseModel):
    column: str = Field(description="Column name exactly as given in the table metadata")
    role: DuplicateRole = Field(description=(
        "KEY: the column(s) identifying the business object this table is about (rows sharing it are "
        "the same object and are never compared with each other). "
        "IDENTIFIER: a value that identifies a real-world entity on its own (tax/registration number, "
        "e-mail, phone, IBAN, ...) - two records sharing one are the same entity. "
        "NAME: the entity's name/description used for exact and fuzzy name matching. "
        "LOCATION: a place-like field (street, city, postal code, ...) that corroborates a name match. "
        "CONTEXT: not matched on, but worth showing to the human reviewer. "
        "IGNORE: irrelevant for duplicate detection."))
    identifier_group: Optional[str] = Field(default=None, description=(
        "IDENTIFIER columns only. Give the SAME group name to columns that are only meaningful "
        "together (e.g. a bank country + bank key + account number are one identifier). "
        "Leave empty when the column identifies the entity on its own."))
    show_in_review: bool = Field(default=True, description=(
        "Show this column in the reviewer's side-by-side record comparison."))
    reason: str = Field(description=(
        "One sentence, for a human auditor, on why this column got this role - cite the data "
        "dictionary description or the statistics you based it on."))

    @field_validator("identifier_group")
    @classmethod
    def normalize_none_string(cls, v):
        return None if v in (None, "None", "null", "") else v

    @field_validator("role", mode="before")
    @classmethod
    def normalize_role(cls, v):
        role = str(v or "").strip().upper()
        return role if role in ("KEY", "IDENTIFIER", "NAME", "LOCATION", "CONTEXT", "IGNORE") else "IGNORE"


class DuplicateRulePlan(BaseModel):
    """How duplicates should be matched in ONE table."""
    label: str = Field(default="records", description=(
        "Plural business noun for one row, used in reviewer text, e.g. 'vendors', 'bank accounts', "
        "'customers', 'materials'."))
    key_unique: bool = Field(default=False, description=(
        "True when the KEY must be unique in this table, so a repeated key value is ITSELF a "
        "duplicate (a master table keyed by its own id). False when one key legitimately owns many "
        "rows (e.g. several bank accounts per vendor)."))
    rule_scope: RuleScope = Field(default="CLIENT_SPECIFIC", description=(
        "UNIVERSAL if these role assignments hold for any company using this standard table layout; "
        "INDUSTRY_SPECIFIC if they depend on the industry; CLIENT_SPECIFIC if any column is custom "
        "or repurposed for this client."))
    industry: Optional[str] = Field(default=None, description="Industry name if rule_scope is INDUSTRY_SPECIFIC")
    notes: Optional[str] = Field(default=None, description=(
        "Optional caveat for the human reviewer, e.g. a column you were unsure about."))
    columns: List[ColumnRule] = Field(description="One entry per column of the table")

    @field_validator("label", mode="before")
    @classmethod
    def normalize_label(cls, v):
        return str(v).strip() if v and str(v).strip() else "records"

    @field_validator("industry", "notes")
    @classmethod
    def normalize_none_string(cls, v):
        return None if v in (None, "None", "null", "") else v

    @field_validator("rule_scope", mode="before")
    @classmethod
    def normalize_rule_scope(cls, v):
        if not v or v not in ("UNIVERSAL", "INDUSTRY_SPECIFIC", "CLIENT_SPECIFIC"):
            return "CLIENT_SPECIFIC"
        return v
