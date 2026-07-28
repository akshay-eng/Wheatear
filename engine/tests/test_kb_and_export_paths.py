"""Resolving paths that were written on a different machine.

Both of these were found by a migration that reported success and produced a
wrong answer, which is the failure mode worth the most tests:

  * an HR agent deployed with no knowledge base, because its source path was
    written inside a Docker container. It then answered a leave-policy question
    from the model's memory -- fluently, and with the wrong number.
  * a Copilot solution export that could not be migrated at all unless the
    operator had live Power Platform credentials.
"""

from __future__ import annotations

import zipfile

from agent_liftoff.connectors.orchestrate.provisioner import resolve_kb_files
from agent_liftoff.wizard import _find_solution_root, _unpack_solution


# --------------------------------------------------------------------------- #
# Knowledge-base files
# --------------------------------------------------------------------------- #

def test_a_literal_path_that_exists_is_used_as_is(tmp_path):
    (tmp_path / "a.pdf").write_text("x")
    assert resolve_kb_files(str(tmp_path / "*.pdf")) == [str(tmp_path / "a.pdf")]


def test_a_container_home_path_resolves_under_this_users_home(monkeypatch, tmp_path):
    """`/home/node/.n8n-files/kb/*.pdf` is n8n-in-Docker's view, not ours."""
    home = tmp_path / "home"
    (home / ".n8n-files" / "kb").mkdir(parents=True)
    (home / ".n8n-files" / "kb" / "policy.pdf").write_text("x")
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))

    found = resolve_kb_files("/home/node/.n8n-files/kb/*.pdf")
    assert [p.rsplit("/", 1)[-1] for p in found] == ["policy.pdf"]


def test_a_mac_style_path_resolves_the_same_way(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "docs").mkdir(parents=True)
    (home / "docs" / "handbook.pdf").write_text("x")
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: home))

    assert resolve_kb_files("/Users/someone/docs/*.pdf")


def test_an_unresolvable_path_returns_nothing_rather_than_guessing(monkeypatch, tmp_path):
    """The caller turns [] into a manual step; a wrong guess would attach the
    wrong documents to an agent and be much harder to notice."""
    monkeypatch.setattr("pathlib.Path.home", classmethod(lambda cls: tmp_path))
    assert resolve_kb_files("/home/node/nowhere/*.pdf") == []


def test_no_selector_at_all_is_not_an_error():
    assert resolve_kb_files(None) == []
    assert resolve_kb_files("") == []


# --------------------------------------------------------------------------- #
# Copilot solution exports
# --------------------------------------------------------------------------- #

def _solution(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "solution.xml").write_text("<ImportExportXml/>")
    return root


def test_solution_root_is_found_when_the_exact_folder_is_given(tmp_path):
    sol = _solution(tmp_path / "MySolution")
    assert _find_solution_root(sol) == sol


def test_solution_root_is_found_from_a_parent_folder(tmp_path):
    """A TransferNow-style download wraps the export in another directory."""
    sol = _solution(tmp_path / "download" / "MySolution")
    assert _find_solution_root(tmp_path / "download") == sol


def test_a_folder_with_no_solution_xml_resolves_to_nothing(tmp_path):
    (tmp_path / "random").mkdir()
    assert _find_solution_root(tmp_path / "random") is None


def test_a_zip_is_unpacked_to_the_directory_holding_solution_xml(tmp_path):
    src = _solution(tmp_path / "src" / "MySolution")
    (src / "bots").mkdir()
    archive = tmp_path / "export.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(src / "solution.xml", "MySolution/solution.xml")
        zf.write(src / "solution.xml", "MySolution/bots/placeholder.xml")

    out = _unpack_solution(archive, tmp_path / "out")
    assert (out / "solution.xml").is_file()
    assert out.name == "MySolution"
