"""Creating connections and handing the target a credential.

The security property this file exists to hold is narrow and worth stating:
the secret a person types goes into a request body and nowhere else. Not onto a
command line, where `ps` and shell history would have it; not into a Wheatear
file; not into a review manifest. The ADK exposes these as CLI flags, and the
whole reason `provisioning` calls the controller in-process is to avoid them.
"""

from __future__ import annotations

import pytest

from wheatear.connectors.orchestrate import provisioning
from wheatear.connectors.orchestrate.provisioning import CredentialRequest
from wheatear.errors import WheatearError


class Controller:
    """Stands in for the ADK's connections controller."""

    def __init__(self):
        self.calls: list[tuple] = []

    def add_connection(self, app_id, resource=None):
        self.calls.append(("add", app_id))

    def add_configuration(self, config):
        self.calls.append(("configure", config.app_id, config.security_scheme, config.preference))

    def add_credentials(self, app_id, environment, use_app_credentials, credentials, payload=None):
        self.calls.append(("credentials", app_id, credentials))


@pytest.fixture
def controller(monkeypatch):
    c = Controller()
    monkeypatch.setattr(provisioning, "_controller", lambda: c)
    return c


def test_a_member_connection_is_never_given_a_stored_secret(controller):
    """Member means each user signs in themselves. Putting a shared credential
    on one defeats the setting that made the migration faithful."""
    request = CredentialRequest(app_id="servicenow_ibm", kind="basic_auth", preference="member")

    with pytest.raises(WheatearError, match="member credentials"):
        provisioning.set_credentials(request, {"username": "u", "password": "p"})

    assert not [c for c in controller.calls if c[0] == "credentials"]


def test_provision_creates_configures_and_says_what_it_did(controller, monkeypatch):
    monkeypatch.setattr(provisioning, "ensure_connection", lambda app_id: True)
    request = CredentialRequest(app_id="servicenow_ibm", kind="bearer_token", preference="member")

    done = provisioning.provision(request)

    assert any("created connection" in line for line in done)
    assert any("each user signs in themselves" in line for line in done)
    assert ("configure", "servicenow_ibm", "bearer_token", "member") in controller.calls
    # Member: no credential call at all.
    assert not [c for c in controller.calls if c[0] == "credentials"]


def test_a_team_connection_takes_the_secret_through_the_python_api(controller, monkeypatch):
    """Not through a CLI flag. `set-credentials --password X` would put the
    password in `ps` output and shell history on a shared machine."""
    monkeypatch.setattr(provisioning, "ensure_connection", lambda app_id: False)
    request = CredentialRequest(app_id="snow", kind="basic_auth", preference="team")

    done = provisioning.provision(request, {"username": "svc", "password": "hunter2"})

    creds = [c for c in controller.calls if c[0] == "credentials"]
    # One per environment: a deployed agent runs against `live`, so a secret
    # stored only in `draft` produces a tool that works in the builder's
    # preview and fails after deploy.
    assert len(creds) == len(provisioning.ENVIRONMENTS)
    assert {c[1] for c in creds} == {"snow"}
    assert all(c[2].password == "hunter2" for c in creds)
    assert any("stored the credential" in line for line in done)
    # The statement handed back to the operator must not repeat the secret.
    assert all("hunter2" not in line for line in done)


def test_both_environments_are_configured_not_just_draft(controller, monkeypatch):
    """The bug this guards against was found live: a migration reported success,
    and the deployed tool raised `KeyError: 'base_url'` because the connection
    existed only in `draft`."""
    monkeypatch.setattr(provisioning, "ensure_connection", lambda app_id: False)
    request = CredentialRequest(app_id="snow", kind="basic_auth", preference="team")

    provisioning.provision(request, {"username": "svc", "password": "hunter2"})

    configured = [c for c in controller.calls if c[0] == "configure"]
    assert len(configured) == 2
    assert "draft" in provisioning.ENVIRONMENTS and "live" in provisioning.ENVIRONMENTS


def test_an_unknown_auth_kind_is_refused_rather_than_guessed(controller):
    with pytest.raises(WheatearError, match="not an auth kind"):
        provisioning.configure(CredentialRequest(app_id="x", kind="telepathy"))


def test_empty_credentials_are_refused(controller):
    request = CredentialRequest(app_id="snow", kind="basic_auth", preference="team")

    with pytest.raises(WheatearError, match="No credential values"):
        provisioning.set_credentials(request, {})


def test_oauth_kinds_configure_under_the_oauth2_scheme(controller):
    """The API takes both a kind and a scheme and rejects a mismatched pair."""
    provisioning.configure(
        CredentialRequest(app_id="snow", kind="oauth2_auth_code", preference="member")
    )

    assert ("configure", "snow", "oauth2", "member") in controller.calls


def test_every_known_kind_declares_the_fields_it_needs():
    """An api_key connection handed a username fails at call time with a
    message about something else entirely."""
    for kind in provisioning.CREDENTIAL_MODELS:
        assert kind in provisioning.SCHEME_FOR_KIND
        assert kind in provisioning.CREDENTIAL_FIELDS

    assert [f[0] for f in provisioning.CREDENTIAL_FIELDS["basic_auth"]] == ["username", "password"]
    assert [f[0] for f in provisioning.CREDENTIAL_FIELDS["api_key_auth"]] == ["api_key"]
    assert [f[0] for f in provisioning.CREDENTIAL_FIELDS["bearer_token"]] == ["token"]


def test_secret_fields_are_marked_so_a_prompt_can_mask_them():
    masked = {name for name, _p, secret in provisioning.CREDENTIAL_FIELDS["basic_auth"] if secret}
    assert masked == {"password"}

    masked = {name for name, _p, secret in provisioning.CREDENTIAL_FIELDS["oauth2_password"] if secret}
    assert masked == {"client_secret", "password"}


def test_a_request_describes_itself_without_leaking_anything():
    request = CredentialRequest(app_id="snow", kind="basic_auth", preference="team")

    assert "snow" in request.summary()
    assert "shared credential" in request.summary()
    assert request.prompts_the_user is False


# ----------------------------------------------------------------------
# Not blanking what somebody configured by hand
# ----------------------------------------------------------------------


class Config:
    def __init__(self, server_url=None, security_scheme=None, preference=None):
        self.server_url = server_url
        self.security_scheme = security_scheme
        self.preference = preference


def test_an_existing_server_url_is_preserved_when_the_request_omits_it(controller, monkeypatch):
    """The destructive case. A configure call sends the whole configuration and
    the platform takes it literally, so a request that simply did not mention
    `server_url` would blank a working ServiceNow endpoint mid-migration.
    """
    monkeypatch.setattr(
        provisioning,
        "current_configuration",
        lambda app_id, environment="draft": Config(server_url="https://dev000000.service-now.com/"),
    )
    captured = {}
    controller.add_configuration = lambda config: captured.update(
        server_url=config.server_url, app_id=config.app_id
    )

    provisioning.configure(CredentialRequest(app_id="servicenow_ibm", kind="bearer_token"))

    assert captured["server_url"] == "https://dev000000.service-now.com/"


def test_a_supplied_server_url_replaces_the_old_one(controller, monkeypatch):
    monkeypatch.setattr(
        provisioning,
        "current_configuration",
        lambda app_id, environment="draft": Config(server_url="https://old.service-now.com/"),
    )
    captured = {}
    controller.add_configuration = lambda config: captured.update(server_url=config.server_url)

    provisioning.configure(
        CredentialRequest(
            app_id="servicenow_ibm", kind="bearer_token", server_url="https://new.service-now.com"
        )
    )

    assert captured["server_url"] == "https://new.service-now.com"


def test_a_connection_nobody_has_configured_yet_starts_with_nothing(controller, monkeypatch):
    monkeypatch.setattr(
        provisioning, "current_configuration", lambda app_id, environment="draft": None
    )
    captured = {}
    controller.add_configuration = lambda config: captured.update(server_url=config.server_url)

    provisioning.configure(CredentialRequest(app_id="brand_new", kind="api_key_auth"))

    assert captured["server_url"] is None


def test_reading_the_current_config_never_raises_into_a_migration(monkeypatch):
    """An unreachable tenant mid-configure must degrade to 'assume nothing',
    not abort a migration that has already put agents on the target."""
    import builtins

    real_import = builtins.__import__

    def explode(name, *args, **kwargs):
        if "connections" in name:
            raise ImportError("no client here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", explode)

    assert provisioning.current_configuration("anything") is None
    assert provisioning.existing_server_url("anything") is None


def test_an_adk_process_exit_becomes_an_error_not_a_dead_wizard(controller):
    """The ADK calls `sys.exit(1)` on an expired token rather than raising.

    `SystemExit` derives from BaseException, so `except Exception` never caught
    it -- and an expired session did not fail a connection step, it killed the
    whole wizard with agents already on the tenant and nothing said about why.
    """

    def exits(*args, **kwargs):
        raise SystemExit(1)

    controller.add_configuration = exits

    with pytest.raises(WheatearError, match="session has usually expired"):
        provisioning.configure(CredentialRequest(app_id="snow", kind="bearer_token"))


def test_a_credential_failure_never_repeats_the_secret(controller):
    """The payload that failed contains the password."""

    def exits(*args, **kwargs):
        raise SystemExit(1)

    controller.add_credentials = exits
    request = CredentialRequest(app_id="snow", kind="basic_auth", preference="team")

    with pytest.raises(WheatearError) as caught:
        provisioning.set_credentials(request, {"username": "svc", "password": "hunter2"})

    assert "hunter2" not in str(caught.value)


def test_reading_config_treats_a_process_exit_as_nothing_configured(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def exits(name, *args, **kwargs):
        if "connections" in name:
            raise SystemExit(1)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", exits)

    assert provisioning.current_configuration("anything") is None
