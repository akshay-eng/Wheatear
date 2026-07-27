"""Reading and switching Power Platform accounts.

Which account is active decides which tenant's solutions are listed and which
environment every later `pac` call reaches, so getting it wrong is not a
cosmetic error -- it exports the wrong agents from the wrong place. The
parsing is tested against real captured output rather than only against a live
signed-in machine, and against layouts from more than one PAC release, because
the column order has moved between them and the wizard must survive that.
"""

from __future__ import annotations

import pytest

from wheatear.connectors.copilot_studio import pac_client

# `pac auth list` on 1.52.x with two profiles, the second active.
TWO_PROFILES = """
Index Active Kind      Name             User                           Cloud   Type
[1]          UNIVERSAL  contoso          admin@contoso.onmicrosoft.com  Public  ServicePrincipal
[2]   *      DATAVERSE  fabrikam         maker@fabrikam.com             Public  User
"""

# A single profile, and PAC omits the marker when there is nothing to choose.
ONE_PROFILE = """
Index Active Kind      Name    User                 Cloud   Type
[1]          DATAVERSE test    solo@contoso.com     Public  User
"""

WITH_ENVIRONMENT = """
Index Active Kind       Name  User               Environment
[1]   *      DATAVERSE  dev   maker@contoso.com  https://org1234.crm.dynamics.com/
"""

NOT_SIGNED_IN = "No profiles were found on this computer.\n"


def test_the_active_profile_is_the_one_marked_active():
    profiles = pac_client.parse_auth_list(TWO_PROFILES)

    assert [p.index for p in profiles] == [1, 2]
    assert [p.user for p in profiles] == [
        "admin@contoso.onmicrosoft.com",
        "maker@fabrikam.com",
    ]
    active = [p for p in profiles if p.active]
    assert len(active) == 1
    assert active[0].user == "maker@fabrikam.com"


def test_auth_status_reports_the_active_account_not_the_first(monkeypatch):
    """The bug this exists to prevent.

    Somebody with two tenants signed in has two emails in the output and only
    one of them is the account every later `pac` call uses. Naming the first
    tells them the migration is reading an environment it is not.
    """
    monkeypatch.setattr(
        pac_client, "list_auth_profiles", lambda: pac_client.parse_auth_list(TWO_PROFILES)
    )

    assert pac_client.auth_status() == (True, "maker@fabrikam.com")


def test_a_lone_profile_counts_as_active_even_without_a_marker():
    profiles = pac_client.parse_auth_list(ONE_PROFILE)

    assert len(profiles) == 1
    assert profiles[0].active is True
    assert profiles[0].user == "solo@contoso.com"


def test_an_environment_url_is_carried_into_the_label():
    profile = pac_client.parse_auth_list(WITH_ENVIRONMENT)[0]

    assert profile.environment == "https://org1234.crm.dynamics.com/"
    assert "maker@contoso.com" in profile.label()
    assert "org1234" in profile.label()


def test_nothing_signed_in_parses_to_no_profiles():
    assert pac_client.parse_auth_list(NOT_SIGNED_IN) == []
    assert pac_client.parse_auth_list("") == []


def test_auth_status_is_false_when_no_profile_exists(monkeypatch):
    monkeypatch.setattr(pac_client, "list_auth_profiles", list)

    assert pac_client.auth_status() == (False, "")


def test_listing_profiles_survives_pac_being_absent(monkeypatch):
    """A missing binary is an empty list, not a traceback out of the wizard."""

    def boom(*args, **kwargs):
        raise FileNotFoundError("pac")

    monkeypatch.setattr(pac_client.subprocess, "run", boom)

    assert pac_client.list_auth_profiles() == []


@pytest.mark.parametrize(
    "call,expected",
    [
        (lambda: pac_client.select_auth_profile(2), ["auth", "select", "--index", "2"]),
        (lambda: pac_client.clear_auth(), ["auth", "clear"]),
        (lambda: pac_client.delete_auth_profile(3), ["auth", "delete", "--index", "3"]),
    ],
)
def test_account_commands_invoke_pac_as_documented(monkeypatch, call, expected):
    """Arguments are asserted; argv[0] is whatever `find_pac` resolved to,
    which on a machine where ~/.dotnet/tools is off PATH is an absolute path
    rather than the bare name."""
    seen: list[list[str]] = []

    class Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kwargs):
        seen.append(cmd)
        return Ok()

    monkeypatch.setattr(pac_client.subprocess, "run", fake_run)
    call()

    assert len(seen) == 1
    assert seen[0][0].endswith(("pac", "pac.exe"))
    assert seen[0][1:] == expected


def test_a_failed_switch_is_raised_rather_than_ignored(monkeypatch):
    """Silently continuing would run the migration against the old tenant."""

    class Failed:
        returncode = 1
        stdout = ""
        stderr = "Error: profile 9 does not exist"

    monkeypatch.setattr(pac_client.subprocess, "run", lambda cmd, **kw: Failed())

    with pytest.raises(pac_client.PacError, match="does not exist"):
        pac_client.select_auth_profile(9)


# ----------------------------------------------------------------------
# Environments
# ----------------------------------------------------------------------
#
# One tenant holds many Dataverse environments and an agent lives in exactly
# one of them. `pac solution list` reports the selected environment's solutions
# and never says which that is, so choosing the wrong one shows an empty
# solution list -- or worse, a plausible one belonging to Dev.

ENV_LIST = """
Index Display Name          Environment Id                        Environment Url                       Type
[1]   Contoso (default)     8f3c2b1a-1111-2222-3333-444455556666  https://contoso.crm.dynamics.com/     Default
[2] * Contoso Dev           1a2b3c4d-5555-6666-7777-888899990000  https://contoso-dev.crm.dynamics.com/ Sandbox
[3]   Contoso Prod          9e8d7c6b-aaaa-bbbb-cccc-ddddeeeeffff  https://contoso-prod.crm.dynamics.com/ Production
"""

# A release that prints the type before the identifiers.
ENV_LIST_TYPE_FIRST = """
Index Name              Type       Environment Id                        Url
[1]   Maker Sandbox     Sandbox    1a2b3c4d-5555-6666-7777-888899990000  https://maker.crm.dynamics.com/
"""

ENV_WHO = """
Connected to...        Contoso Dev
Environment Url:       https://contoso-dev.crm.dynamics.com/
Environment Id:        1a2b3c4d-5555-6666-7777-888899990000
"""


def test_environments_are_parsed_with_their_identifiers():
    envs = pac_client.parse_env_list(ENV_LIST)

    assert [e.index for e in envs] == [1, 2, 3]
    assert [e.display_name for e in envs] == ["Contoso (default)", "Contoso Dev", "Contoso Prod"]
    assert envs[1].url == "https://contoso-dev.crm.dynamics.com/"
    assert envs[2].environment_id == "9e8d7c6b-aaaa-bbbb-cccc-ddddeeeeffff"
    assert [e.env_type for e in envs] == ["Default", "Sandbox", "Production"]


def test_the_selected_environment_is_the_one_marked():
    envs = pac_client.parse_env_list(ENV_LIST)

    active = [e for e in envs if e.active]
    assert [e.display_name for e in active] == ["Contoso Dev"]


def test_a_type_column_before_the_ids_is_not_glued_onto_the_name():
    """The column order has moved between PAC releases; a layout change should
    cost a column, not the ability to tell environments apart."""
    env = pac_client.parse_env_list(ENV_LIST_TYPE_FIRST)[0]

    assert env.display_name == "Maker Sandbox"
    assert env.env_type == "Sandbox"
    assert env.url == "https://maker.crm.dynamics.com/"


def test_an_environment_is_selected_by_url_rather_than_name():
    """Display names repeat across tenants; a Dataverse org URL does not."""
    envs = pac_client.parse_env_list(ENV_LIST)

    assert envs[0].selector == "https://contoso.crm.dynamics.com/"
    assert pac_client.EnvironmentInfo(1, "Nameless", "the-guid").selector == "the-guid"
    assert pac_client.EnvironmentInfo(1, "Only a name").selector == "Only a name"


def test_listing_environments_degrades_to_empty_rather_than_raising(monkeypatch):
    """Losing the ability to *choose* an environment must not cost the ability
    to migrate from the one already selected."""

    class Failed:
        returncode = 1
        stdout = ""
        stderr = "Error: no auth profile"

    monkeypatch.setattr(pac_client.subprocess, "run", lambda cmd, **kw: Failed())
    assert pac_client.list_environments() == []

    def boom(*args, **kwargs):
        raise FileNotFoundError("pac")

    monkeypatch.setattr(pac_client.subprocess, "run", boom)
    assert pac_client.list_environments() == []
    assert pac_client.current_environment() == ""


def test_the_current_environment_is_read_back_from_pac(monkeypatch):
    class Ok:
        returncode = 0
        stdout = ENV_WHO
        stderr = ""

    monkeypatch.setattr(pac_client.subprocess, "run", lambda cmd, **kw: Ok())

    assert pac_client.current_environment() == "https://contoso-dev.crm.dynamics.com/"


def test_selecting_an_environment_invokes_pac_as_documented(monkeypatch):
    seen: list[list[str]] = []

    class Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(pac_client.subprocess, "run", lambda cmd, **kw: (seen.append(cmd), Ok())[1])
    pac_client.select_environment("https://contoso-prod.crm.dynamics.com/")

    assert len(seen) == 1
    assert seen[0][0].endswith(("pac", "pac.exe"))
    # Verified against pac 1.52.1: `pac env select [--environment]`, which takes
    # an ID, url, unique name or partial name.
    assert seen[0][1:] == [
        "env", "select", "--environment", "https://contoso-prod.crm.dynamics.com/"
    ]


def test_a_failed_environment_switch_is_raised(monkeypatch):
    """Silently continuing would export from the previous environment."""

    class Failed:
        returncode = 1
        stdout = ""
        stderr = "Error: environment not found"

    monkeypatch.setattr(pac_client.subprocess, "run", lambda cmd, **kw: Failed())

    with pytest.raises(pac_client.PacError, match="not found"):
        pac_client.select_environment("https://nope.crm.dynamics.com/")


# ----------------------------------------------------------------------
# Finding the binary at all
# ----------------------------------------------------------------------


def test_pac_is_found_in_the_dotnet_tools_dir_when_it_is_not_on_path(monkeypatch, tmp_path):
    """The regression behind "it asks me to install PAC every time".

    `dotnet tool install --global` puts `pac` in ~/.dotnet/tools, and that
    directory is on PATH only if the dotnet installer edited a shell profile.
    Looking at PATH alone made an installed pac read as missing: the wizard
    offered to install it, dotnet said it already was, the process patched its
    own PATH, and the next launch asked again.
    """
    tools = tmp_path / ".dotnet" / "tools"
    tools.mkdir(parents=True)
    (tools / "pac").write_text("#!/bin/sh\n")

    monkeypatch.setattr(pac_client.shutil, "which", lambda _name: None)
    monkeypatch.setattr(pac_client, "dotnet_tools_path", lambda: str(tools))

    assert pac_client.find_pac() == str(tools / "pac")


def test_pac_on_path_wins_over_the_tools_directory(monkeypatch):
    monkeypatch.setattr(pac_client.shutil, "which", lambda _name: "/usr/local/bin/pac")

    assert pac_client.find_pac() == "/usr/local/bin/pac"


def test_a_genuinely_missing_pac_is_still_reported_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(pac_client.shutil, "which", lambda _name: None)
    monkeypatch.setattr(pac_client, "dotnet_tools_path", lambda: str(tmp_path / "nowhere"))

    assert pac_client.find_pac() is None
    assert pac_client.check() == (False, "")


# ----------------------------------------------------------------------
# The layout `pac env list` actually prints
# ----------------------------------------------------------------------

# Captured verbatim from pac 1.52.1. There is no index column at all -- an
# earlier parser required one, matched nothing, and reported that a tenant with
# three environments had none.
ENV_LIST_REAL = """Connected as user@example.com
Active Display Name     Environment ID                       Environment URL                            Unique Name
       copilotstudio    11111111-1111-1111-1111-111111111111 https://orgexample02.crm8.dynamics.com/     unq00000000000000000000000000a1
*      Contoso Main     22222222-2222-2222-2222-222222222222 https://orgexample01.crm8.dynamics.com/     orgexample01
       Contoso Consult  33333333-3333-3333-3333-333333333333 https://contosoconsult.crm8.dynamics.com/  unq00000000000000000000000000a2
"""


def test_the_real_pac_layout_yields_every_environment():
    envs = pac_client.parse_env_list(ENV_LIST_REAL)

    assert [e.display_name for e in envs] == ["copilotstudio", "Contoso Main", "Contoso Consult"]
    assert [e.active for e in envs] == [False, True, False]
    assert envs[2].url == "https://contosoconsult.crm8.dynamics.com/"
    assert envs[1].unique_name == "orgexample01"
    # No index column, so position is the index.
    assert [e.index for e in envs] == [1, 2, 3]


def test_the_connected_as_preamble_and_header_are_not_environments():
    """Neither carries a GUID or a URL, which is exactly how they are skipped."""
    envs = pac_client.parse_env_list(ENV_LIST_REAL)

    assert len(envs) == 3
    assert all("Connected as" not in e.display_name for e in envs)
    assert all(e.environment_id for e in envs)
