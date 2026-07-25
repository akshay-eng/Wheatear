"""Most of this screen is intentionally not unit tested -- same convention
as wizard.py (see its module docstring): driving a real interactive prompt
isn't worth the harness complexity, and it's covered by live pty runs
instead. The hard_gate menu-construction logic is pure enough to assert on
directly by capturing what gets passed to questionary.select.
"""

import questionary

from wheatear import onboarding
from wheatear.config import WheatearConfig
from wheatear.onboarding import needs_onboarding
from wheatear.syscheck import DependencyStatus


def test_needs_onboarding_true_when_no_config():
    assert needs_onboarding(None) is True


def test_needs_onboarding_true_when_never_completed():
    assert needs_onboarding(WheatearConfig(onboarding_completed=False)) is True


def test_needs_onboarding_false_once_completed():
    assert needs_onboarding(WheatearConfig(onboarding_completed=True)) is False


def test_hard_gate_removes_continue_bypass_while_required_item_missing(monkeypatch):
    # Corridor tools (dotnet/PAC/Orchestrate CLI) have no graceful
    # degradation -- unlike git/keyring, there's no "continue anyway" for a
    # migration that will just fail without them.
    missing_required = [DependencyStatus("dotnet", installed=False, detail="x", required=True)]
    captured_choices = []

    class FakeAsk:
        def ask(self):
            return "back"

    def fake_select(message, choices):
        captured_choices.extend(choices)
        return FakeAsk()

    monkeypatch.setattr(onboarding.questionary, "select", fake_select)

    went_back, _ = onboarding._run_checklist_loop(
        lambda on_check=None: missing_required,
        title="t", intro_text="t", allow_back=True, back_label="back", hard_gate=True,
    )

    assert went_back is True
    values = [c.value for c in captured_choices if isinstance(c, questionary.Choice)]
    assert "continue" not in values
    assert "back" in values


def test_hard_gate_allows_continue_once_everything_installed(monkeypatch):
    all_installed = [DependencyStatus("dotnet", installed=True, detail="x", required=True)]
    captured_choices = []

    class FakeAsk:
        def ask(self):
            return "continue"

    def fake_select(message, choices):
        captured_choices.extend(choices)
        return FakeAsk()

    monkeypatch.setattr(onboarding.questionary, "select", fake_select)

    went_back, _ = onboarding._run_checklist_loop(
        lambda on_check=None: all_installed,
        title="t", intro_text="t", allow_back=True, back_label="back", hard_gate=True,
    )

    assert went_back is False
    values = [c.value for c in captured_choices if isinstance(c, questionary.Choice)]
    assert "continue" in values


def test_soft_gate_keeps_continue_anyway_even_when_missing(monkeypatch):
    # run_onboarding's base checks (git/keyring) degrade gracefully, so
    # hard_gate=False must still offer a bypass.
    missing_optional = [DependencyStatus("git", installed=False, detail="x", required=False)]
    captured_choices = []

    class FakeAsk:
        def ask(self):
            return "continue"

    def fake_select(message, choices):
        captured_choices.extend(choices)
        return FakeAsk()

    monkeypatch.setattr(onboarding.questionary, "select", fake_select)

    went_back, _ = onboarding._run_checklist_loop(
        lambda on_check=None: missing_optional,
        title="t", intro_text="t", allow_back=False, back_label="", hard_gate=False,
    )

    assert went_back is False
    values = [c.value for c in captured_choices if isinstance(c, questionary.Choice)]
    assert "continue" in values