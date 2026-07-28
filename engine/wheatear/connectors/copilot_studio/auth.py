"""Microsoft authentication for Power Platform / Copilot Studio API access.

Two flows are supported:

  device_code  -- The wizard shows a short code and a URL; the user visits
                  microsoft.com/devicelogin in any browser and enters it.
                  No Azure AD app registration is required from the user.
                  Works with any work/school account that has Power Platform
                  access. Tokens are cached in memory; nothing is written to
                  disk by Wheatear.

  service_principal -- Uses a registered Azure AD application (client ID +
                  client secret). Better for team or CI scenarios where no
                  human is at the keyboard. The service principal must also be
                  added as an Application User inside the target Dataverse
                  environment (Power Platform admin center → Settings →
                  Users + permissions → Application users).

Requires the 'copilot-studio' extra:
    pip install wheatear[copilot-studio]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# The Azure PowerShell app is pre-approved in the vast majority of enterprise
# Azure AD tenants and carries the Power Platform delegated permissions needed
# for device code flows without requiring users to register their own app.
_PUBLIC_CLIENT_ID = "1950a258-227b-4e31-a9cf-717495945fc2"

# Scopes needed for the Power Platform management API (environment listing,
# solution export). Dataverse-org-scoped tokens are requested on demand via
# token_for(resource) using the cached refresh token.
_MANAGEMENT_SCOPES = ["https://service.powerapps.com/.default"]


class AuthError(Exception):
    pass


@dataclass
class DeviceCodeLogin:
    """An initiated device-code flow waiting for Microsoft sign-in."""

    _app: object
    _flow: dict

    @property
    def user_code(self) -> str:
        return str(self._flow.get("user_code", ""))

    @property
    def verification_uri(self) -> str:
        return str(
            self._flow.get("verification_uri")
            or self._flow.get("verification_url")
            or "https://microsoft.com/devicelogin"
        )

    @property
    def message(self) -> str:
        return str(self._flow.get("message", ""))

    @property
    def expires_in(self) -> int:
        return int(self._flow.get("expires_in", 900))


@dataclass
class TokenProvider:
    """Holds an authenticated MSAL session and can issue tokens for any
    resource scope without re-prompting the user.

    Internally keeps the MSAL app + account so that
    acquire_token_silent() (device code path) or acquire_token_for_client()
    (service principal path) can be called for Dataverse org URLs that are
    only known after the environment is selected.
    """

    _app: object
    _account: object | None  # None for service principal flow
    _is_confidential: bool

    @property
    def account_name(self) -> str:
        if not isinstance(self._account, dict):
            return ""
        return str(
            self._account.get("username")
            or self._account.get("name")
            or self._account.get("home_account_id")
            or ""
        )

    def token_for(self, resource_url: str) -> str:
        """Return an access token scoped to resource_url/.default.

        resource_url is the root URL of the resource, e.g.:
          "https://service.powerapps.com"
          "https://org12345.crm.dynamics.com"
        """
        scope = resource_url.rstrip("/") + "/.default"
        if self._is_confidential:
            result = self._app.acquire_token_for_client(scopes=[scope])
        else:
            result = self._app.acquire_token_silent(
                scopes=[scope],
                account=self._account,
            )
            # acquire_token_silent can return None if the cache misses; in
            # practice this shouldn't happen after a successful device code
            # auth with a refresh token in the cache.
            if result is None:
                raise AuthError(
                    f"Cached token for {resource_url} has expired. Re-run the wizard to re-authenticate."
                )

        if "error" in result:
            raise AuthError(
                f"Could not get token for {resource_url}: "
                f"{result.get('error_description') or result.get('error')}"
            )
        return result["access_token"]


def begin_device_code(tenant_id: str = "organizations") -> DeviceCodeLogin:
    """Initiate Microsoft sign-in without waiting for the user to finish."""
    try:
        import msal
    except ImportError as exc:
        raise ImportError(
            "Auto-discovery mode requires the 'copilot-studio' extra: "
            "pip install wheatear[copilot-studio]"
        ) from exc

    app = msal.PublicClientApplication(
        client_id=_PUBLIC_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
    )

    flow = app.initiate_device_flow(scopes=_MANAGEMENT_SCOPES)
    if "error" in flow:
        raise AuthError(
            f"Could not start device code flow: "
            f"{flow.get('error_description') or flow.get('error')}"
        )
    return DeviceCodeLogin(_app=app, _flow=flow)


def complete_device_code(login: DeviceCodeLogin) -> TokenProvider:
    """Wait for an initiated device-code flow and return its token provider."""
    result = login._app.acquire_token_by_device_flow(login._flow)
    if "error" in result:
        raise AuthError(
            f"Authentication failed: "
            f"{result.get('error_description') or result.get('error')}"
        )

    accounts = login._app.get_accounts()
    return TokenProvider(
        _app=login._app,
        _account=accounts[0] if accounts else None,
        _is_confidential=False,
    )


def authenticate_device_code(
    tenant_id: str,
    on_code: Callable[[str], None],
) -> TokenProvider:
    """Run the blocking device-code flow used by the terminal wizard."""
    login = begin_device_code(tenant_id)
    on_code(login.message)
    return complete_device_code(login)


def authenticate_service_principal(
    tenant_id: str,
    client_id: str,
    client_secret: str,
) -> TokenProvider:
    """Authenticate using Azure AD client credentials (service principal).

    The returned TokenProvider can get tokens for any resource without
    further user interaction.
    """
    try:
        import msal
    except ImportError as exc:
        raise ImportError(
            "Auto-discovery mode requires the 'copilot-studio' extra: "
            "pip install wheatear[copilot-studio]"
        ) from exc

    app = msal.ConfidentialClientApplication(
        client_id=client_id,
        client_credential=client_secret,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
    )

    # Do an initial token acquisition to validate credentials eagerly.
    result = app.acquire_token_for_client(scopes=_MANAGEMENT_SCOPES)
    if "error" in result:
        raise AuthError(
            f"Service principal authentication failed: "
            f"{result.get('error_description') or result.get('error')}"
        )

    return TokenProvider(
        _app=app,
        _account=None,
        _is_confidential=True,
    )
