"""Push a generated Copilot Studio solution into the user's environment.

The exporter writes a solution in the *raw* Dataverse package layout (root
`solution.xml` + `customizations.xml` + `[Content_Types].xml` + component
folders) -- the same shape a Dataverse solution export unzips to. That is
directly importable, so we zip the directory ourselves and run
`pac solution import`.

We deliberately do NOT use `pac solution pack`: that command expects the
*unpacked source* layout (`Other/Solution.xml`, ...) produced by
`pac solution unpack`, not the raw package layout, and fails with
"Cannot find required file" on ours.

PAC auth/env selection is handled by the wizard's connection step. Output is
captured (not raised) so a format failure can be shown to the user and
optionally handed to the AI-repair step, rather than crashing the run.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

# Files the exporter writes into the solution dir that are NOT part of the
# Dataverse package and must be excluded before packing.
_NON_SOLUTION_FILES = {"review-manifest.yaml"}


@dataclass
class DeployResult:
    success: bool
    stage: str  # "pack" | "import" | "done"
    output: str
    command: str
    zip_path: Path | None = None


def _run(cmd: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _staged_copy(solution_dir: Path) -> Path:
    """Copy the solution into a temp dir minus non-package files, so the zip we
    import is a clean Dataverse package.
    """
    staged = Path(tempfile.mkdtemp(prefix="wheatear-pack-")) / solution_dir.name
    shutil.copytree(
        solution_dir,
        staged,
        ignore=lambda _d, names: [n for n in names if n in _NON_SOLUTION_FILES],
    )
    return staged


def _zip_solution(src_dir: Path, zip_path: Path) -> None:
    """Zip a raw solution directory so its files sit at the archive root
    (solution.xml, [Content_Types].xml, botcomponents/..., etc.) -- the shape
    `pac solution import` expects.
    """
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(src_dir.rglob("*")):
            if p.is_file():
                zf.write(p, arcname=str(p.relative_to(src_dir)))


def deploy_solution(solution_dir: Path) -> DeployResult:
    """Zip `solution_dir` and import it into the active PAC environment."""
    solution_dir = Path(solution_dir)
    if shutil.which("pac") is None:
        return DeployResult(
            success=False, stage="import",
            output="The 'pac' CLI was not found on PATH.", command="pac",
        )

    staged = _staged_copy(solution_dir)
    zip_path = staged.parent / f"{solution_dir.name}.zip"
    try:
        _zip_solution(staged, zip_path)
    except OSError as exc:
        return DeployResult(
            success=False, stage="zip", output=f"Failed to zip solution: {exc}", command="zip",
        )

    import_cmd = ["pac", "solution", "import", "--path", str(zip_path), "--force-overwrite"]
    imp = _run(import_cmd, timeout=300)
    return DeployResult(
        success=imp.returncode == 0,
        stage="import" if imp.returncode != 0 else "done",
        output=(imp.stderr + imp.stdout).strip()[:2000] or "(no output)",
        command=" ".join(import_cmd),
        zip_path=zip_path,
    )
