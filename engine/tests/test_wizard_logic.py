from pathlib import Path

import questionary

from wheatear import wizard
from wheatear.config import WheatearConfig
from wheatear.ir.schema import Agent
from wheatear.tui import flush_input
from wheatear.wizard import (
    BACK,
    _build_final_config,
    _export_for_target,
    _is_back,
    _match_filter,
    _multiselect_menu,
    _translate_stage,
    config_changed,
    resolve_key_env_for_provider,
    suggest_output_path,
)


def test_is_back_matches_plain_sentinel_on_an_empty_field():
    assert _is_back(":back") is True
    assert _is_back(":b") is True
    assert _is_back("  :BACK  ") is True


def test_is_back_matches_sentinel_appended_after_a_prefilled_default():
    # Regression: questionary.text pre-fills a default with the cursor at
    # the end, so typing ":back" over a remembered/suggested value appends
    # rather than replaces the text -- confirmed live (a suggested output
    # path plus a typed ":back" produced "...sample_agent-orchestrate:back",
    # which an exact-match check would silently accept as a literal path).
    assert _is_back("/home/user/export-orchestrate:back") is True
    assert _is_back("https://example.com/instance:b") is True


def test_is_back_false_for_a_real_path_or_url():
    assert _is_back("/home/user/my-export") is False
    assert _is_back("https://example.com/instance") is False
    assert _is_back("") is False


def test_suggest_output_path_is_a_sibling_directory():
    export_path = Path("/home/user/exports/my-agent")
    assert suggest_output_path(export_path) == Path("/home/user/exports/my-agent-orchestrate")


def test_resolve_key_env_keeps_saved_value_for_same_provider():
    existing = WheatearConfig(llm_provider="anthropic", llm_key_env="MY_CUSTOM_KEY_VAR")
    assert resolve_key_env_for_provider("anthropic", existing) == "MY_CUSTOM_KEY_VAR"


def test_resolve_key_env_falls_back_to_default_when_provider_changes():
    existing = WheatearConfig(llm_provider="anthropic", llm_key_env="MY_CUSTOM_KEY_VAR")
    assert resolve_key_env_for_provider("openai", existing) == "OPENAI_API_KEY"


def test_resolve_key_env_falls_back_to_default_when_no_existing_config():
    assert resolve_key_env_for_provider("anthropic", None) == "ANTHROPIC_API_KEY"


def test_config_changed_true_when_no_saved_config():
    assert config_changed(WheatearConfig(), None) is True


def test_config_changed_false_when_identical():
    cfg = WheatearConfig(llm_provider="anthropic", llm_key_env="ANTHROPIC_API_KEY")
    assert config_changed(cfg, WheatearConfig(llm_provider="anthropic", llm_key_env="ANTHROPIC_API_KEY")) is False


def test_config_changed_true_when_provider_differs():
    cfg = WheatearConfig(llm_provider="openai", llm_key_env="OPENAI_API_KEY")
    old = WheatearConfig(llm_provider="anthropic", llm_key_env="ANTHROPIC_API_KEY")
    assert config_changed(cfg, old) is True


def test_resolve_key_env_deterministic_provider_needs_no_default_key():
    # "none" (deterministic) must not blow up looking for a default key env.
    assert resolve_key_env_for_provider("none", None) == ""
    existing = WheatearConfig(llm_provider="anthropic", llm_key_env="MY_KEY")
    assert resolve_key_env_for_provider("none", existing) == "MY_KEY"


def test_translate_stage_deterministic_when_no_provider():
    agent = Agent(name="a", source_platform="orchestrate", existing_instructions="Be helpful.")
    _translate_stage(agent, None)  # provider None -> deterministic carry-over
    assert agent.instructions == "Be helpful."
    assert agent.translation_confidence == 1.0


def test_build_final_config_preserves_completed_onboarding_flag():
    # Regression guard: every wizard path re-saves config after collecting LLM
    # settings, via this function. If it dropped onboarding_completed, the
    # onboarding screen would resurface on every single future launch.
    llm_config = WheatearConfig(llm_provider="anthropic", llm_key_env="ANTHROPIC_API_KEY")
    saved = WheatearConfig(onboarding_completed=True)

    result = _build_final_config(llm_config, None, saved)

    assert result.onboarding_completed is True


def test_build_final_config_defaults_onboarding_false_with_no_saved_config():
    llm_config = WheatearConfig(llm_provider="anthropic", llm_key_env="ANTHROPIC_API_KEY")

    result = _build_final_config(llm_config, None, None)

    assert result.onboarding_completed is False


def test_match_filter_requires_every_term():
    assert _match_filter("hr onboarding assistant", ["hr", "assistant"]) is True
    assert _match_filter("hr onboarding assistant", ["hr", "payroll"]) is False
    assert _match_filter("anything", []) is True  # blank filter shows all


# ---------------------------------------------------------------------------
# _multiselect_menu -- driven through a scripted questionary, since the
# interesting behaviour (selection surviving paging/filtering, "select all"
# being scoped) only shows up across several prompt rounds.
# ---------------------------------------------------------------------------

class _Answer:
    def __init__(self, value):
        self.value = value

    def ask(self):
        return self.value


def _script_menu(monkeypatch, picks, texts=()):
    """Replay `picks` as successive menu selections and `texts` as successive
    filter entries. Returns the list of `default=` values questionary was
    handed, so a test can assert the cursor was never restored to a row that
    had been paged or filtered away."""
    picks = list(picks)
    texts = list(texts)
    defaults: list = []

    def fake_select(message, choices, default=None):
        selectable = {
            c.value for c in choices if isinstance(c, questionary.Choice) and not c.disabled
        }
        assert default in selectable, f"default {default!r} is not a selectable row"
        defaults.append(default)
        want = picks.pop(0)
        assert want in selectable, f"scripted pick {want!r} is not on screen"
        return _Answer(want)

    def fake_text(message, default=""):
        return _Answer(texts.pop(0))

    monkeypatch.setattr(wizard.questionary, "select", fake_select)
    monkeypatch.setattr(wizard.questionary, "text", fake_text)
    return defaults


ITEMS = [f"agent-{i:02d}" for i in range(30)]
LEGAL = [f"legal-{i:02d}" for i in range(4)]


def test_select_all_is_scoped_to_the_active_filter(monkeypatch):
    # "Select all" while filtered must not quietly rope in the 30 agents the
    # user filtered out -- at enterprise scale that's an unrecoverable misclick.
    _script_menu(monkeypatch, ["__search__", "__toggle_all__", "__confirm__"], texts=["legal"])

    picked = _multiselect_menu("pick", ITEMS + LEGAL, lambda s: s, page_size=5)

    assert picked == LEGAL


def test_selection_survives_paging_and_filtering(monkeypatch):
    # A pick made on page 1 has to still be there after the user pages away,
    # filters, clears the filter and comes back.
    defaults = _script_menu(
        monkeypatch,
        [0, "__next__", 7, "__search__", "__clear_search__", "__confirm__"],
        texts=["agent-2"],
    )

    picked = _multiselect_menu("pick", ITEMS, lambda s: s, page_size=5)

    assert picked == ["agent-00", "agent-07"]
    # Regression: questionary raises ValueError on a default that isn't a
    # selectable row, and paging/filtering retires rows under the cursor.
    assert all(d is not None for d in defaults)


def test_preselected_items_come_back_checked(monkeypatch):
    _script_menu(monkeypatch, ["__confirm__"])

    picked = _multiselect_menu(
        "pick", ITEMS, lambda s: s, preselected={"agent-03"}, page_size=5
    )

    assert picked == ["agent-03"]


def test_preselected_keys_that_no_longer_exist_are_dropped(monkeypatch):
    # A remembered selection from an earlier discovery run may name agents
    # that a re-discovery no longer returns; those must not be reported as
    # "selected" (nor make Confirm think something is picked).
    _script_menu(monkeypatch, [1, "__confirm__"])

    picked = _multiselect_menu(
        "pick", ITEMS, lambda s: s, preselected={"deleted-agent"}, page_size=5
    )

    assert picked == ["agent-01"]


def test_back_choice_returns_the_back_sentinel(monkeypatch):
    _script_menu(monkeypatch, [BACK])

    assert _multiselect_menu("pick", ITEMS, lambda s: s, page_size=5) is BACK


def test_confirm_is_disabled_until_something_is_picked(monkeypatch):
    captured: list = []

    def fake_select(message, choices, default=None):
        captured.extend(choices)
        return _Answer(BACK)

    monkeypatch.setattr(wizard.questionary, "select", fake_select)
    _multiselect_menu("pick", ITEMS, lambda s: s, page_size=5)

    confirm = next(
        c for c in captured if isinstance(c, questionary.Choice) and c.value == "__confirm__"
    )
    assert confirm.disabled


def test_flush_input_is_a_no_op_without_a_tty():
    # pytest replaces stdin with a non-tty; flushing must stay silent there
    # rather than raising and taking a prompt down with it.
    flush_input()


def test_export_for_target_dispatches_to_the_right_exporter(tmp_path):
    agent = Agent(name="Helper", source_platform="orchestrate", instructions="hi")

    orch = _export_for_target(agent, "orchestrate", tmp_path / "orch")
    assert (orch.agent_path).name == "agent.yaml"

    cp = _export_for_target(agent, "copilot-studio", tmp_path / "cp")
    assert (cp.agent_path / "solution.xml").exists()
