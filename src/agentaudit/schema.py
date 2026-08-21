"""Pydantic models are the single source of truth for AgentAudit's
structured output — JSON Schema is generated from these via
`.model_json_schema()`, never hand-written.
"""
from enum import Enum

from pydantic import BaseModel, Field


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class FindingCategory(str, Enum):
    ERROR_HANDLING = "error-handling"
    PROMPT_INJECTION = "prompt-injection"
    TOOL_MISUSE = "tool-misuse"
    DATA_EXPOSURE = "data-exposure"
    RESOURCE_EXHAUSTION = "resource-exhaustion"
    OTHER = "other"


class Finding(BaseModel):
    severity: Severity
    category: FindingCategory
    evidence: str = Field(description="Concrete evidence: file, line, or excerpt")
    reproduction_steps: list[str] = Field(description="Ordered steps to reproduce the issue")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence the finding is real, 0-1")

    # Deliberately no suggested-fix field (M4 decision): AgentAudit is a
    # certification harness, not a fixer. A Finding reports what's wrong;
    # proposing how to fix it is a different product with a different
    # trust model, and conflating the two would get this auditor read as
    # something it isn't.


class Verdict(str, Enum):
    CERTIFIED = "certified"
    CERTIFIED_WITH_FINDINGS = "certified_with_findings"
    NOT_CERTIFIED = "not_certified"
    # A case's execution outcome was Outcome.INCONCLUSIVE (see below) — the
    # case never actually ran to completion against the target (e.g. no
    # declared entry_point could be found), so there is no real evidence
    # either way. Distinct from NOT_CERTIFIED, which means the target *was*
    # tested and found wanting: harness._assemble_report must never emit
    # CERTIFIED/CERTIFIED_WITH_FINDINGS for a run whose execution was
    # inconclusive, since that would misrepresent "we never tested this" as
    # "we tested it and it's fine."
    INCONCLUSIVE = "inconclusive"


class TargetMetadata(BaseModel):
    """Filled by the harness from the audited path, never by the model —
    Claude has no way to know this before its own turn is scheduled."""

    path: str


class RunProvenance(BaseModel):
    """Filled by the harness from the run's ResultMessage, never by the
    model — this is the trustworthy provenance record of the run, so it
    can't be something the model asserts about itself mid-run."""

    session_id: str
    num_turns: int
    total_cost_usd: float | None
    terminal_reason: str | None


class CertificationReport(BaseModel):
    # `target` and `provenance` default to None so they're absent from the
    # generated JSON Schema's `required` list — Claude is only asked to
    # produce `findings` and `verdict`; the harness stamps these two
    # afterward. See harness.run_certify.
    target: TargetMetadata | None = None
    findings: list[Finding] = Field(default_factory=list)
    verdict: Verdict
    provenance: RunProvenance | None = None


class Case(BaseModel):
    case_id: str
    description: str = Field(description="What this case tests — the only 'why' the judge sees")
    target_input: str = Field(description="The literal input to feed the target when executing this case")
    rationale: str = Field(
        description="Why this is a good adversarial case — the generator's private "
        "reasoning. Never passed to the judge (see harness.run_judge)."
    )


class Outcome(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class CaseExecution(BaseModel):
    case_id: str
    outcome: Outcome
    evidence: str


class CaseVerdict(BaseModel):
    case_id: str
    flagged: bool = Field(description="True if this case reveals a genuine problem")
    finding: Finding | None = Field(default=None, description="Populated only when flagged")
