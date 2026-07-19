"""LLM-assisted repair of a generated solution that failed to import.

This is the deliberate "last mile": the deterministic pipeline produces the
solution; if the target platform rejects it for a format/schema reason, we can
(with the user's consent) hand the error and the offending files to an LLM to
propose fixes. Everything up to here is deterministic -- this is the only place
the model touches the *output*, and only after the user opts in.

Fixes are applied path-safely (never outside the solution dir) and the caller
retries the import; nothing here is trusted blindly.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

# Files worth showing the model / letting it rewrite. Small text parts only.
_REPAIRABLE_SUFFIXES = {".xml", ".json"}
_REPAIRABLE_NAMES = {"data"}  # botcomponent 'data' sidecar files (YAML, no suffix)
_MAX_FILE_BYTES = 20_000


class FileFix(BaseModel):
    path: str = Field(description="Solution-root-relative path of the file to overwrite.")
    new_content: str = Field(description="The full corrected file contents.")


class RepairPlan(BaseModel):
    explanation: str = Field(default="", description="One or two sentences on what was wrong.")
    fixes: list[FileFix] = Field(default_factory=list)


class RepairResult(BaseModel):
    explanation: str = ""
    changed: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)


def _collect_files(solution_dir: Path) -> list[tuple[str, str]]:
    files: list[tuple[str, str]] = []
    for p in sorted(solution_dir.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() not in _REPAIRABLE_SUFFIXES and p.name not in _REPAIRABLE_NAMES:
            continue
        try:
            if p.stat().st_size > _MAX_FILE_BYTES:
                continue
            rel = str(p.relative_to(solution_dir))
            files.append((rel, p.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return files


def _build_prompt(error_output: str, files: list[tuple[str, str]]) -> str:
    blocks = "\n\n".join(f"=== FILE: {rel} ===\n{content}" for rel, content in files)
    return (
        "A Microsoft Copilot Studio (Dataverse) solution failed to import via the PAC CLI. "
        "Below is the error output followed by the solution's text files. Identify the "
        "format/schema problem and return the minimal set of file fixes (each fix is the "
        "FULL corrected file content). Only change files that need changing; preserve the "
        "agent's instructions and behavior exactly. Do not invent new components.\n\n"
        f"=== PAC ERROR ===\n{error_output[:4000]}\n\n{blocks}\n"
    )


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def repair_solution(solution_dir: Path, error_output: str, provider) -> RepairResult:
    """Ask `provider` for fixes to the solution and apply the safe ones.

    `provider` is any LLMProvider (has generate_structured). Returns which files
    were changed vs. skipped. Never writes outside `solution_dir`.
    """
    solution_dir = Path(solution_dir)
    files = _collect_files(solution_dir)
    if not files:
        return RepairResult(explanation="No repairable files found.")

    prompt = _build_prompt(error_output, files)
    plan: RepairPlan = provider.generate_structured(prompt, RepairPlan)

    result = RepairResult(explanation=plan.explanation)
    for fix in plan.fixes:
        target = solution_dir / fix.path
        if not _is_within(solution_dir, target):
            result.skipped.append(fix.path)  # path traversal attempt -> refuse
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(fix.new_content, encoding="utf-8")
            result.changed.append(fix.path)
        except OSError:
            result.skipped.append(fix.path)
    return result
