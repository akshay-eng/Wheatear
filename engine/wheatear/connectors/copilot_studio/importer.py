"""Format dispatcher for Copilot Studio imports.

Copilot Studio agents reach Wheatear via one of two real export shapes:
a `pac copilot clone` workspace, or a Dataverse solution export. This module
detects which one it's looking at and routes to the matching importer, so
the rest of the pipeline (Map, Translate, Validate, CLI) only ever has to
call one `import_agent`.
"""

from __future__ import annotations

from pathlib import Path

from wheatear.connectors.copilot_studio import mcs_yaml_importer, solution_importer
from wheatear.connectors.copilot_studio.common import ImportResult  # re-exported for existing call sites

__all__ = ["ImportResult", "detect_format", "import_agent"]


def detect_format(path: Path) -> str | None:
    """Return "mcs_yaml", "solution", or None if neither is recognized."""
    path = Path(path)
    if list(path.glob("*.mcs.yaml")):
        return "mcs_yaml"
    if (path / "solution.xml").exists() and (path / "bots").is_dir():
        return "solution"
    return None


def import_agent(path: Path) -> ImportResult:
    path = Path(path)
    fmt = detect_format(path)

    if fmt == "mcs_yaml":
        return mcs_yaml_importer.import_agent(path)
    if fmt == "solution":
        return solution_importer.import_agent(path)

    raise FileNotFoundError(
        f"{path} doesn't look like a Copilot Studio export Wheatear recognizes "
        "(expected either a `pac copilot clone` workspace with a *.mcs.yaml root file, "
        "or a solution export with solution.xml + a bots/ directory)."
    )
