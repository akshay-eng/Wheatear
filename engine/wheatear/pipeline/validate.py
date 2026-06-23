"""Validate stage: schema-level checks before Export writes anything.

Catches structural gaps (missing instructions, empty name) early rather than
letting the exporter silently write an unusable agent.yaml. review_required
items on tools/connections become warnings here, not errors -- the exporter
already handles those by writing a review-manifest.yaml alongside the
agent, so they don't block producing output, only block "ready to import
without a human looking at it first."
"""

from __future__ import annotations

from dataclasses import dataclass, field

from wheatear.ir.schema import Agent

LOW_CONFIDENCE_THRESHOLD = 0.8


@dataclass
class ValidationIssue:
    field: str
    message: str
    severity: str  # "error" | "warning"


@dataclass
class ValidationResult:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def is_valid(self) -> bool:
        return not self.errors


def validate_agent(agent: Agent) -> ValidationResult:
    result = ValidationResult()

    if not agent.name.strip():
        result.issues.append(ValidationIssue("name", "Agent name is empty.", "error"))

    if not agent.instructions.strip():
        result.issues.append(
            ValidationIssue(
                "instructions",
                "No instructions were generated; run the Translate stage first.",
                "error",
            )
        )

    for tool in agent.tools:
        if tool.review_required:
            result.issues.append(
                ValidationIssue("tools", f"Tool '{tool.ref}' needs manual review before import.", "warning")
            )

    for conn in agent.connections:
        if conn.review_required:
            result.issues.append(
                ValidationIssue(
                    "connections",
                    f"Connection '{conn.ref}' needs credentials before this agent will run.",
                    "warning",
                )
            )

    if agent.translation_confidence < LOW_CONFIDENCE_THRESHOLD:
        result.issues.append(
            ValidationIssue(
                "instructions",
                f"Translation confidence is {agent.translation_confidence:.2f}; review before import.",
                "warning",
            )
        )

    return result
