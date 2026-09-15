from typing import List, Literal
from pydantic import BaseModel, Field, field_validator

# Shared by Reflection (single-check judgment) and FindingJudgment (one
# judgment within a batch) below, so the severity/confidence vocabulary
# only has to be defined once.
Severity = Literal["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
Confidence = Literal["HIGH", "MEDIUM", "LOW"]

class Reflection(BaseModel):
    """Structured judgment of a single data-quality check's result."""
    is_issue: bool = Field(description="True if this reveals a genuine data quality issue")
    severity: Severity
    confidence: Confidence
    summary: str = Field(description="One-line human-readable description of the finding")
    reusable: bool = Field(description="True if this check would likely generalize to similar datasets/clients")

from typing import Optional

class ProposedCheck(BaseModel):
    column: str = Field(description="Primary column this check targets")
    hypothesis: str = Field(description="What is being tested and why")
    code: str = Field(description="Pandas code; must set `result` to an AGGREGATE value "
                                  "(count/pct/bool/small dict) - sent to LLM for reflection.")
    detail_code: Optional[str] = Field(default=None, description=
        "OPTIONAL pandas code that sets `detail_rows` - a list of dicts, one per offending "
        "row, each with keys: row_index (int), key_field (str, name of a natural key column "
        "like LIFNR), key_value (that row's key value), issue_detail (str, what's wrong with "
        "THIS row). This is for LOCAL HUMAN REVIEW ONLY - it is NEVER sent back to the LLM. "
        "Keep detail_rows to a reasonable number of rows (e.g. cap at ~50) if many rows match.")
    compare_table: Optional[str] = Field(default=None,
        description="Optional; another registered table name if this is a cross-table check")

    @field_validator("compare_table", "detail_code")
    @classmethod
    def normalize_none_string(cls, v):
        if v in (None, "None", "null", ""):
            return None
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

class ReflectionBatch(BaseModel):
    judgments: List[FindingJudgment]
