from wheatear import syscheck


def test_check_all_returns_three_base_checks():
    statuses = syscheck.check_all()
    names = [s.name for s in statuses]
    assert len(statuses) == 3
    assert any("Python" in n for n in names)
    assert "git" in names
    assert any("keyring" in n.lower() for n in names)


def test_check_python_version_passes_under_the_test_runner():
    status = syscheck._check_python_version()
    assert status.installed is True
    assert status.required is True


def test_git_install_command_macos_uses_brew(monkeypatch):
    monkeypatch.setattr(syscheck.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(syscheck.shutil, "which", lambda name: "/usr/bin/brew" if name == "brew" else None)
    assert syscheck._git_install_command() == ["brew", "install", "git"]


def test_git_install_command_macos_none_without_brew(monkeypatch):
    monkeypatch.setattr(syscheck.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(syscheck.shutil, "which", lambda name: None)
    assert syscheck._git_install_command() is None


def test_git_install_command_windows_prefers_winget(monkeypatch):
    monkeypatch.setattr(syscheck.platform, "system", lambda: "Windows")
    monkeypatch.setattr(syscheck.shutil, "which", lambda name: "C:\\winget.exe" if name == "winget" else None)
    assert syscheck._git_install_command() == ["winget", "install", "--id", "Git.Git", "-e", "--source", "winget"]


def test_git_install_command_windows_falls_back_to_choco(monkeypatch):
    monkeypatch.setattr(syscheck.platform, "system", lambda: "Windows")
    monkeypatch.setattr(syscheck.shutil, "which", lambda name: "choco" if name == "choco" else None)
    assert syscheck._git_install_command() == ["choco", "install", "git", "-y"]


def test_git_install_command_linux_picks_first_available_manager(monkeypatch):
    monkeypatch.setattr(syscheck.platform, "system", lambda: "Linux")
    monkeypatch.setattr(syscheck.shutil, "which", lambda name: "/usr/bin/dnf" if name == "dnf" else None)
    assert syscheck._git_install_command() == ["sudo", "dnf", "install", "-y", "git"]


def test_git_install_command_linux_apt_get_refreshes_index_first(monkeypatch):
    # Regression: a bare `apt-get install -y git` fails with "Unable to
    # locate package" on a fresh/minimal image whose package index was never
    # fetched -- confirmed against a real fresh container, not a guess.
    monkeypatch.setattr(syscheck.platform, "system", lambda: "Linux")
    monkeypatch.setattr(syscheck.shutil, "which", lambda name: "/usr/bin/apt-get" if name == "apt-get" else None)
    cmd = syscheck._git_install_command()
    assert cmd[:3] == ["sudo", "sh", "-c"]
    assert "apt-get update" in cmd[3]
    assert "apt-get install -y git" in cmd[3]


def test_git_install_command_linux_none_when_no_manager_found(monkeypatch):
    monkeypatch.setattr(syscheck.platform, "system", lambda: "Linux")
    monkeypatch.setattr(syscheck.shutil, "which", lambda name: None)
    assert syscheck._git_install_command() is None


def test_check_git_missing_has_no_fix_when_no_manager_available(monkeypatch):
    monkeypatch.setattr(syscheck.shutil, "which", lambda name: None)
    monkeypatch.setattr(syscheck.platform, "system", lambda: "Linux")
    status = syscheck._check_git()
    assert status.installed is False
    assert status.required is False
    assert status.fix is None
    assert status.fix_command is None
    # Manual suggestions must still be offered even with no auto-fix path --
    # that's the whole point of the manual option existing separately.
    assert status.manual_hint is not None


def test_git_manual_hint_lists_apt_dnf_and_pacman_on_linux(monkeypatch):
    monkeypatch.setattr(syscheck.platform, "system", lambda: "Linux")
    monkeypatch.setattr(syscheck.shutil, "which", lambda name: "/usr/bin/pacman" if name == "pacman" else None)
    hint = syscheck._git_manual_hint()
    assert "apt-get install -y git" in hint
    assert "dnf install -y git" in hint
    assert "pacman -S --noconfirm git" in hint
    assert "detected on this system" in hint  # only pacman should be flagged


def test_git_manual_hint_macos_mentions_brew(monkeypatch):
    monkeypatch.setattr(syscheck.platform, "system", lambda: "Darwin")
    hint = syscheck._git_manual_hint()
    assert "brew install git" in hint


def test_git_manual_hint_windows_mentions_winget(monkeypatch):
    monkeypatch.setattr(syscheck.platform, "system", lambda: "Windows")
    hint = syscheck._git_manual_hint()
    assert "winget install" in hint


def test_check_keyring_backend_reports_manual_hint_when_broken(monkeypatch):
    import keyring as real_keyring

    def _raise(*a, **k):
        raise RuntimeError("no backend available")

    monkeypatch.setattr(real_keyring, "set_password", _raise)
    status = syscheck._check_keyring_backend()
    assert status.installed is False
    assert status.manual_hint is not None
    assert "keyrings.alt" in status.manual_hint


def test_pip_install_reports_failure_without_raising(monkeypatch):
    class FakeCompleted:
        returncode = 1
        stdout = ""
        stderr = "no such package"

    monkeypatch.setattr(syscheck.subprocess, "run", lambda *a, **k: FakeCompleted())
    outcome = syscheck._pip_install(["not-a-real-package"])
    assert outcome.success is False
    assert "no such package" in outcome.detail


def test_pip_install_reports_success(monkeypatch):
    class FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(syscheck.subprocess, "run", lambda *a, **k: FakeCompleted())
    outcome = syscheck._pip_install(["keyrings.alt"])
    assert outcome.success is True
    assert "keyrings.alt" in outcome.detail


def test_run_system_install_missing_binary_reports_failure(monkeypatch):
    def _raise(*a, **k):
        raise FileNotFoundError("no such file: fake-pkg-mgr")

    monkeypatch.setattr(syscheck.subprocess, "run", _raise)
    outcome = syscheck._run_system_install(["fake-pkg-mgr", "install", "git"])
    assert outcome.success is False
    assert "fake-pkg-mgr" in outcome.detail


def test_fix_keyring_backend_warns_a_restart_is_needed_on_success(monkeypatch):
    # Regression: confirmed against a real container that keyring caches its
    # backend-discovery result for the process lifetime, so installing
    # keyrings.alt genuinely fixes things -- but only from the *next* launch.
    # A refresh in the same session must not look like the install failed.
    monkeypatch.setattr(
        syscheck, "_pip_install", lambda packages: syscheck.InstallOutcome(True, "installed keyrings.alt")
    )
    outcome = syscheck._fix_keyring_backend()
    assert outcome.success is True
    assert outcome.restart_required is True


def test_fix_keyring_backend_passes_through_failure(monkeypatch):
    monkeypatch.setattr(
        syscheck, "_pip_install", lambda packages: syscheck.InstallOutcome(False, "network error")
    )
    outcome = syscheck._fix_keyring_backend()
    assert outcome.success is False
    assert outcome.detail == "network error"


# ---------------------------------------------------------------------------
# Corridor-specific tooling: dotnet, PAC CLI, Orchestrate CLI
# ---------------------------------------------------------------------------

def test_dotnet_install_command_windows_uses_winget(monkeypatch):
    monkeypatch.setattr(syscheck.platform, "system", lambda: "Windows")
    monkeypatch.setattr(syscheck.shutil, "which", lambda name: "winget" if name == "winget" else None)
    cmd = syscheck._dotnet_install_command()
    assert cmd == ["winget", "install", "-e", "--id", "Microsoft.DotNet.SDK.8"]


def test_dotnet_install_command_windows_none_without_winget(monkeypatch):
    monkeypatch.setattr(syscheck.platform, "system", lambda: "Windows")
    monkeypatch.setattr(syscheck.shutil, "which", lambda name: None)
    assert syscheck._dotnet_install_command() is None


def test_dotnet_install_command_linux_uses_official_script(monkeypatch):
    monkeypatch.setattr(syscheck.platform, "system", lambda: "Linux")
    monkeypatch.setattr(syscheck.shutil, "which", lambda name: f"/usr/bin/{name}")
    cmd = syscheck._dotnet_install_command()
    assert cmd[:2] == ["bash", "-c"]
    assert "dotnet-install.sh" in cmd[2]
    assert "sudo" not in cmd[2]  # installs to ~/.dotnet, no elevation needed
    assert "curl" in cmd[2]


def test_dotnet_install_command_linux_falls_back_to_wget(monkeypatch):
    # Regression: confirmed against a real minimal container (python:3.12-slim)
    # that has neither curl nor wget by default -- and separately that a
    # wget-only environment is a real possibility worth covering.
    monkeypatch.setattr(syscheck.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        syscheck.shutil, "which",
        lambda name: "/usr/bin/wget" if name in ("bash", "wget") else None,
    )
    cmd = syscheck._dotnet_install_command()
    assert cmd[:2] == ["bash", "-c"]
    assert "wget" in cmd[2]
    assert "curl" not in cmd[2]


def test_dotnet_install_command_linux_none_without_bash_curl_or_wget(monkeypatch):
    # Regression: this is the exact state of a fresh python:3.12-slim image.
    monkeypatch.setattr(syscheck.platform, "system", lambda: "Linux")
    monkeypatch.setattr(syscheck.shutil, "which", lambda name: "/usr/bin/bash" if name == "bash" else None)
    assert syscheck._dotnet_install_command() is None


def test_check_dotnet_missing_reports_no_fix_without_bash_or_curl(monkeypatch):
    monkeypatch.setattr(syscheck.platform, "system", lambda: "Linux")
    monkeypatch.setattr(syscheck.shutil, "which", lambda name: None)
    status = syscheck._check_dotnet()
    assert status.installed is False
    assert status.required is True
    assert status.fix is None
    assert status.manual_hint is not None


def test_check_pac_reports_dotnet_prerequisite_when_dotnet_missing(monkeypatch):
    from wheatear.connectors.copilot_studio import pac_client

    monkeypatch.setattr(pac_client, "check", lambda: (False, ""))
    monkeypatch.setattr(syscheck.shutil, "which", lambda name: None)
    status = syscheck._check_pac()
    assert status.installed is False
    assert status.fix is None  # don't offer a fix guaranteed to fail
    assert ".net sdk" in status.detail.lower()


def test_check_pac_offers_fix_when_dotnet_present(monkeypatch):
    from wheatear.connectors.copilot_studio import pac_client

    monkeypatch.setattr(pac_client, "check", lambda: (False, ""))
    monkeypatch.setattr(syscheck.shutil, "which", lambda name: "/usr/bin/dotnet" if name == "dotnet" else None)
    status = syscheck._check_pac()
    assert status.installed is False
    assert status.fix is not None
    assert status.fix_command == pac_client.install_guide()


def test_check_orchestrate_cli_missing_offers_pip_fix(monkeypatch):
    monkeypatch.setattr(syscheck.shutil, "which", lambda name: None)
    status = syscheck._check_orchestrate_cli()
    assert status.installed is False
    assert status.required is True
    assert "ibm-watsonx-orchestrate" in status.fix_command
    assert status.fix is not None


def test_check_orchestrate_cli_found(monkeypatch):
    monkeypatch.setattr(syscheck.shutil, "which", lambda name: "/usr/bin/orchestrate" if name == "orchestrate" else None)
    status = syscheck._check_orchestrate_cli()
    assert status.installed is True


def test_check_corridor_needs_nothing_for_pure_deterministic_pair():
    # Neither platform is copilot-studio and we're not deploying -- e.g. a
    # hypothetical future orchestrate-to-orchestrate corridor.
    checks = syscheck.check_corridor("orchestrate", "orchestrate", deploy_to_orchestrate=False)
    assert checks == []


def test_check_corridor_includes_dotnet_and_pac_when_copilot_studio_is_source():
    checks = syscheck.check_corridor("copilot-studio", "orchestrate", deploy_to_orchestrate=False)
    names = [c.name for c in checks]
    assert ".NET SDK" in names
    assert "PAC CLI (Copilot Studio)" in names
    assert "Orchestrate CLI (deploy)" not in names


def test_check_corridor_includes_dotnet_and_pac_when_copilot_studio_is_target():
    checks = syscheck.check_corridor("orchestrate", "copilot-studio", deploy_to_orchestrate=False)
    names = [c.name for c in checks]
    assert ".NET SDK" in names
    assert "PAC CLI (Copilot Studio)" in names


def test_check_corridor_includes_orchestrate_cli_only_when_deploying():
    checks = syscheck.check_corridor("copilot-studio", "orchestrate", deploy_to_orchestrate=True)
    names = [c.name for c in checks]
    assert "Orchestrate CLI (deploy)" in names


def test_corridor_needs_checks_answers_without_running_any_probe(monkeypatch):
    # The whole point of this predicate is that ensure_corridor_tools can ask
    # "is there anything to check?" for free. check_corridor() spends several
    # seconds in dotnet/pac cold starts, so answering by calling it would
    # double the wait the user sits through.
    def _explode():
        raise AssertionError("a probe ran while only the corridor shape was asked about")

    for name in ("_check_dotnet", "_check_pac", "_check_orchestrate_cli"):
        monkeypatch.setattr(syscheck, name, _explode)

    assert syscheck.corridor_needs_checks("orchestrate", "copilot-studio") is True
    assert syscheck.corridor_needs_checks("copilot-studio", "orchestrate") is True
    assert syscheck.corridor_needs_checks("orchestrate", "orchestrate", True) is True
    assert syscheck.corridor_needs_checks("orchestrate", "orchestrate") is False


def test_check_corridor_announces_each_probe_before_running_it(monkeypatch):
    # Drives the spinner label, so the user is told *which* tool is being
    # probed rather than watching an unexplained multi-second pause.
    announced: list[str] = []
    monkeypatch.setattr(
        syscheck, "_check_dotnet",
        lambda: syscheck.DependencyStatus(".NET SDK", True, "9.0", True),
    )
    monkeypatch.setattr(
        syscheck, "_check_pac",
        lambda: syscheck.DependencyStatus("PAC CLI (Copilot Studio)", True, "1.5", True),
    )

    syscheck.check_corridor("orchestrate", "copilot-studio", on_check=announced.append)

    assert announced == [".NET SDK", "PAC CLI"]


def test_check_all_announces_each_probe():
    announced: list[str] = []
    syscheck.check_all(on_check=announced.append)
    assert announced == ["Python", "git", "credential store"]