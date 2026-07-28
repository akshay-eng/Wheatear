"""Create and configure the connections a migrated tool needs to authenticate.

A migrated tool is half a migration. `get_records` lands on the agent and then
fails at runtime unless something on the target can authenticate to ServiceNow,
and a solution export never contains credentials -- so somebody has to supply
them on the target. This module is what turns that from a documented manual
step into a question and an answer.

Three things happen here, in order, and all three are idempotent:

    add_connection(app_id)            the connection exists
    configure(...)                    it knows its auth kind and server
    set_credentials(...)              it holds a secret (or expects each user
                                      to bring their own)

**Secrets never touch a command line.** The ADK exposes these as CLI flags --
`connections set-credentials --password X` -- and on a shared machine that
lands the password in `ps` output and in shell history. So this calls the ADK's
Python controller in-process instead: the secret exists as a local variable and
a request body, and nowhere else.

**Nothing is written to disk by this module.** What the user types goes to the
target platform and, if they ask for it, to the OS keychain via `creds.py` --
their machine, their keyring. Wheatear stores no credential of its own, carries
none from the source, and puts none in a review manifest or a log.

`member` versus `team` is the one choice that matters and it is not a detail:
member means each user signs in themselves and the agent calls as them, which
is the only setting under which a migrated agent reproduces a source platform
that enforced per-user permissions. A member connection needs no stored secret
at all -- which is why `set_credentials` is skipped for one.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from wheatear.errors import WheatearError

# Which secret fields each auth kind actually needs, and what to call them when
# asking a person. Taken from the ADK's own credential models rather than
# invented: an api_key connection handed a username is a connection that fails
# on its first call with a message about something else entirely.
CREDENTIAL_FIELDS: dict[str, tuple[tuple[str, str, bool], ...]] = {
    # kind: ((field, prompt, secret?), ...)
    "basic_auth": (("username", "Username", False), ("password", "Password", True)),
    "bearer_token": (("token", "Bearer token", True),),
    "api_key_auth": (("api_key", "API key", True),),
    "key_value_creds": (),  # free-form; collected as key/value pairs
    "oauth2_auth_code": (
        ("client_id", "OAuth client id", False),
        ("client_secret", "OAuth client secret", True),
    ),
    "oauth2_client_creds": (
        ("client_id", "OAuth client id", False),
        ("client_secret", "OAuth client secret", True),
    ),
    "oauth2_password": (
        ("client_id", "OAuth client id", False),
        ("client_secret", "OAuth client secret", True),
        ("username", "Username", False),
        ("password", "Password", True),
    ),
}

# The security scheme each auth kind belongs to. The API takes both and rejects
# a mismatched pair.
SCHEME_FOR_KIND = {
    "basic_auth": "basic_auth",
    "bearer_token": "bearer_token",
    "api_key_auth": "api_key_auth",
    "key_value_creds": "key_value_creds",
    "oauth2_auth_code": "oauth2",
    "oauth2_client_creds": "oauth2",
    "oauth2_password": "oauth2",
    "oauth_on_behalf_of_flow": "oauth2",
    "oauth2_token_exchange": "oauth2",
    "oauth2_direct_accesstoken": "oauth2",
}

CREDENTIAL_MODELS = {
    "basic_auth": "BasicAuthCredentials",
    "bearer_token": "BearerTokenAuthCredentials",
    "api_key_auth": "APIKeyAuthCredentials",
    "key_value_creds": "KeyValueConnectionCredentials",
    "oauth2_auth_code": "OAuth2AuthCodeCredentials",
    "oauth2_client_creds": "OAuth2ClientCredentials",
    "oauth2_password": "OAuth2PasswordCredentials",
}


@dataclass
class CredentialRequest:
    """What one connection needs before a tool bound to it will work."""

    app_id: str
    kind: str  # a key of CREDENTIAL_FIELDS
    environment: str = "draft"
    preference: str = "member"
    server_url: str | None = None
    # Tools that stop working until this is answered. Shown when asking, so a
    # person can tell what they are being asked for and why.
    tools: list[str] = field(default_factory=list)

    @property
    def prompts_the_user(self) -> bool:
        """Member connections are answered by whoever chats to the agent."""
        return self.preference == "member"

    @property
    def fields(self) -> tuple[tuple[str, str, bool], ...]:
        return CREDENTIAL_FIELDS.get(self.kind, ())

    def summary(self) -> str:
        who = "each user signs in themselves" if self.prompts_the_user else "one shared credential"
        return f"{self.app_id} ({self.kind}, {self.environment}, {who})"


def _controller():
    """The ADK's connection controller, imported late.

    Late because the ADK is a heavy import and a migration that never touches
    connections should not pay for it, and because a missing ADK should read as
    a clear error here rather than an ImportError at module load.
    """
    try:
        from ibm_watsonx_orchestrate.cli.commands.connections import (  # noqa: PLC0415
            connections_controller,
        )
    except ImportError as exc:  # pragma: no cover - the ADK is a declared dep
        raise WheatearError(
            "The watsonx Orchestrate ADK is not installed, so connections cannot be "
            "configured. Install it, or configure the connection in the console."
        ) from exc
    return connections_controller


# The ADK's controllers call `sys.exit(1)` on an expired token rather than
# raising. `SystemExit` derives from BaseException, so `except Exception` does
# not catch it -- which meant an expired session did not fail a connection
# step, it killed the whole wizard mid-migration with agents already on the
# tenant and nothing said about why.
_ADK_FAILURES = (Exception, SystemExit)


def _adk_exit_message(exc: BaseException) -> str:
    return (
        "the ADK exited -- its session has usually expired; "
        "run `orchestrate env activate <env> --api-key ...`"
        if isinstance(exc, SystemExit)
        else f"{type(exc).__name__}: {exc}"
    )


def _types():
    from ibm_watsonx_orchestrate_core.types.connections import configuration, credentials  # noqa: PLC0415

    return configuration, credentials


def ensure_connection(app_id: str) -> bool:
    """Create the connection if the tenant does not have it. True if created.

    Idempotent because a migration is re-run: the second pass must not fail on
    a connection the first pass created.
    """
    controller = _controller()
    try:
        from ibm_watsonx_orchestrate.client.connections import (  # noqa: PLC0415
            get_connections_client,
        )

        if get_connections_client().get(app_id=app_id):
            return False
    except _ADK_FAILURES:  # noqa: BLE001 - unknown means "try to create and see"
        pass
    try:
        controller.add_connection(app_id=app_id)
    except SystemExit as exc:
        raise WheatearError(
            f"Could not create connection `{app_id}`: {_adk_exit_message(exc)}"
        ) from exc
    return True


def current_configuration(app_id: str, environment: str = "draft") -> Any | None:
    """What the tenant already has configured for this connection, if anything.

    Read before writing. Verified against a live tenant: a configure call that
    sends `server_url=None` leaves the stored value alone, while `""` clears
    it -- so the danger is narrower than it looks, but relying on an undocumented
    null-is-ignored rule to protect a working ServiceNow endpoint is not a plan.
    Reading first also means a person is only asked for a URL the tenant does
    not already know, which is the difference between one question and one
    question per migration.
    """
    try:
        from ibm_watsonx_orchestrate.client.connections import (  # noqa: PLC0415
            get_connections_client,
        )

        return get_connections_client().get_config(app_id=app_id, env=environment)
    except _ADK_FAILURES:  # noqa: BLE001 - unknown means "assume nothing configured"
        return None


def configure(request: CredentialRequest) -> None:
    """Tell the connection its auth kind, who it belongs to, and where it points.

    Anything the request leaves unset is taken from what is already there
    rather than sent as null. See `current_configuration` for why.
    """
    controller = _controller()
    configuration, _ = _types()

    scheme = SCHEME_FOR_KIND.get(request.kind)
    if scheme is None:
        raise WheatearError(
            f"'{request.kind}' is not an auth kind this target accepts. "
            f"Known kinds: {', '.join(sorted(SCHEME_FOR_KIND))}."
        )

    existing = current_configuration(request.app_id, request.environment)
    server_url = request.server_url or getattr(existing, "server_url", None)

    config = configuration.ConnectionConfiguration(
        app_id=request.app_id,
        environment=request.environment,
        preference=request.preference,
        security_scheme=scheme,
        auth_type=request.kind if scheme == "oauth2" else None,
        server_url=server_url,
    )
    try:
        controller.add_configuration(config)
    except SystemExit as exc:
        raise WheatearError(
            f"Could not configure `{request.app_id}`: {_adk_exit_message(exc)}"
        ) from exc


def existing_server_url(app_id: str, environment: str = "draft") -> str | None:
    """The server a connection already points at, for a caller about to ask."""
    return getattr(current_configuration(app_id, environment), "server_url", None)


def looks_like_a_url(value: str) -> bool:
    """Whether a typed value is plausibly a server URL.

    Cheap, and it earns its place. A prompt for a server URL sitting next to a
    prompt for a bearer token is an easy place to paste the wrong thing, and
    the platform's answer to a token in that field is a tenant outbound-call
    policy error naming the token -- which reads like a network problem and
    puts the secret in an error message.
    """
    value = (value or "").strip()
    if not value or " " in value:
        return False
    if value.startswith(("http://", "https://")):
        rest = value.split("//", 1)[1]
        return bool(rest) and "." in rest.split("/")[0]
    # A bare host is accepted; anything with no dot at all is not a server.
    return "." in value.split("/")[0] and "/" not in value.split(".")[0]


def set_credentials(request: CredentialRequest, secrets: dict[str, str]) -> None:
    """Hand the target the secret a person just typed.

    In-process on purpose. The equivalent CLI call puts the password in `ps`
    output and shell history; this keeps it in a local variable and a request
    body. Nothing is logged and nothing is written to disk here.

    A member-preference connection is not given a stored secret at all -- each
    user supplies their own when the agent prompts them -- so calling this for
    one is a mistake worth naming rather than a no-op.
    """
    if request.prompts_the_user:
        raise WheatearError(
            f"`{request.app_id}` is set to member credentials, where each user signs in "
            "themselves. Storing a shared secret on it would defeat that; switch it to "
            "team first if a shared credential is really what you want."
        )
    if not secrets:
        raise WheatearError(f"No credential values supplied for `{request.app_id}`.")

    controller = _controller()
    configuration, credentials = _types()

    model_name = CREDENTIAL_MODELS.get(request.kind)
    if model_name is None:
        raise WheatearError(f"No credential model for auth kind '{request.kind}'.")
    model = getattr(credentials, model_name)

    if request.kind == "key_value_creds":
        payload = model(keys=dict(secrets))
    else:
        payload = model(**secrets)

    try:
        controller.add_credentials(
            app_id=request.app_id,
            environment=configuration.ConnectionEnvironment(request.environment),
            use_app_credentials=False,
            credentials=payload,
        )
    except SystemExit as exc:
        # Deliberately does not repeat the payload: the secret is in it.
        raise WheatearError(
            f"Could not store the credential on `{request.app_id}`: {_adk_exit_message(exc)}"
        ) from exc


# Orchestrate keeps a connection's configuration per environment, and an agent
# runs against `live` once deployed while the builder's preview uses `draft`.
# Configuring one and not the other produces the worst possible split: the tool
# works while you are testing it and fails the moment it is deployed, with a
# KeyError naming a credential field rather than the environment that has none.
ENVIRONMENTS: tuple[str, ...] = ("draft", "live")


def provision(
    request: CredentialRequest,
    secrets: dict[str, str] | None = None,
    environments: tuple[str, ...] = ENVIRONMENTS,
) -> list[str]:
    """Do all of it, in every environment, and say what was done.

    Returns a list of past-tense statements for the caller to show. Deliberately
    not a boolean: "created the connection, configured it for per-user sign-in"
    is what somebody needs to read back, and a caller cannot reconstruct that
    from True.

    Both environments by default. A connection configured only in `draft` is
    invisible to the deployed agent that needs it -- observed live as
    `KeyError: 'base_url'` from inside a migrated tool, hours after a migration
    that reported success -- and nothing about a migration implies "for the
    preview only".
    """
    done: list[str] = []
    if ensure_connection(request.app_id):
        done.append(f"created connection `{request.app_id}`")

    configured: list[str] = []
    credentialled: list[str] = []
    for environment in environments:
        # The request carries one environment; each pass targets its own so a
        # failure in `live` cannot be mistaken for a failure in `draft`.
        scoped = replace(request, environment=environment)
        configure(scoped)
        configured.append(environment)
        if secrets and not scoped.prompts_the_user:
            set_credentials(scoped, secrets)
            credentialled.append(environment)

    who = "each user signs in themselves" if request.prompts_the_user else "a shared credential"
    done.append(
        f"configured `{request.app_id}` for {request.kind} ({who}) "
        f"in {' and '.join(configured)}"
    )
    if credentialled:
        done.append(
            f"stored the credential on `{request.app_id}` in {' and '.join(credentialled)}"
        )
    return done


def requests_for(tools: list[Any], connections: list[Any]) -> list[CredentialRequest]:
    """What still needs answering, given the tools that landed.

    Only tools that actually landed on an agent produce a request. A tool that
    could not be installed has nothing to authenticate yet, and asking somebody
    for a ServiceNow password to satisfy a tool that is not on the tenant is a
    question with no useful answer.
    """
    usable = {c.app_id for c in connections if getattr(c, "usable", False)}
    wanted: dict[str, CredentialRequest] = {}
    for tool in tools:
        for app_id in getattr(tool, "app_ids", ()) or ():
            if app_id in usable or app_id in wanted:
                continue
            wanted[app_id] = CredentialRequest(app_id=app_id, kind="basic_auth")
    return list(wanted.values())
