"""Resolves an export location -- a local path or a GitHub repo/tree URL --
into a local directory Wheatear can read.

Export bundles often live in a repo (someone's `pac copilot clone` output
committed somewhere, or a solution export checked in for review), not on
the machine running the wizard, so pointing Wheatear at a URL directly is
worth supporting alongside a plain local path.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from wheatear.connectors.copilot_studio.importer import detect_format

GITHUB_TREE_RE = re.compile(
    r"^https://github\.com/(?P<org>[^/]+)/(?P<repo>[^/]+)/tree/(?P<branch>[^/]+)(?:/(?P<subpath>.*))?$"
)

CLONE_TIMEOUT_SECONDS = 60


class SourceFetchError(Exception):
    pass


def looks_like_url(raw: str) -> bool:
    return raw.startswith(("http://", "https://", "git@"))


def parse_github_url(raw: str) -> tuple[str, str | None, str]:
    """Returns (repo_clone_url, branch_or_None, subpath_within_repo)."""
    match = GITHUB_TREE_RE.match(raw)
    if match:
        repo_url = f"https://github.com/{match['org']}/{match['repo']}.git"
        return repo_url, match["branch"], match["subpath"] or ""

    repo_url = raw
    if "github.com" in raw and not raw.endswith(".git"):
        repo_url = raw.rstrip("/") + ".git"
    return repo_url, None, ""


def clone_repo(repo_url: str, branch: str | None) -> Path:
    if shutil.which("git") is None:
        raise SourceFetchError("git is required to fetch from a repository URL, but wasn't found on PATH.")

    dest = Path(tempfile.mkdtemp(prefix="wheatear-export-"))
    cmd = ["git", "clone", "--depth", "1"]
    if branch:
        cmd += ["--branch", branch]
    cmd += [repo_url, str(dest)]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CLONE_TIMEOUT_SECONDS,
            # Fail fast instead of hanging on a credential prompt for a
            # private repo the wizard has no way to authenticate against.
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired as exc:
        raise SourceFetchError(f"Cloning {repo_url} timed out after {CLONE_TIMEOUT_SECONDS}s.") from exc

    if result.returncode != 0:
        raise SourceFetchError(f"git clone failed for {repo_url}: {result.stderr.strip()}")
    return dest


def resolve_export_source(raw: str) -> Path:
    """Returns a local directory. If `raw` is a URL, clones it (or the
    relevant subpath, for a GitHub tree URL) to a temp directory first.
    Raises SourceFetchError with a clean message on anything that goes
    wrong; never leaves a half-cloned mess silently unreported.
    """
    if not looks_like_url(raw):
        return Path(raw).expanduser()

    repo_url, branch, subpath = parse_github_url(raw)
    clone_root = clone_repo(repo_url, branch)
    candidate = (clone_root / subpath) if subpath else clone_root

    if detect_format(candidate) is not None:
        return candidate

    if candidate.is_dir():
        matches = [d for d in sorted(candidate.iterdir()) if d.is_dir() and detect_format(d) is not None]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            options = ", ".join(str(m.relative_to(clone_root)) for m in matches)
            raise SourceFetchError(
                f"Found multiple recognizable exports under {repo_url}: {options}. "
                "Point the URL at the specific folder (a GitHub 'tree' URL) instead."
            )

    raise SourceFetchError(f"Cloned {repo_url} but couldn't find a recognized export under it.")
