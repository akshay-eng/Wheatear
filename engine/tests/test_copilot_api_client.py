from wheatear.connectors.copilot_studio.api_client import (
    CopilotStudioClient,
    Environment,
)


class _Tokens:
    def token_for(self, _resource_url):
        return "token"


class _Response:
    status_code = 200
    text = ""

    def json(self):
        return {
            "value": [
                {
                    "botid": "bot-1",
                    "name": "Support",
                    "schemaname": "contoso_Support",
                }
            ]
        }


class _Requests:
    def __init__(self, payload=None):
        self.url = None
        self.params = None
        self.payload = payload

    def get(self, url, *, params, **_kwargs):
        self.url = url
        self.params = params
        if self.payload is None:
            return _Response()
        response = _Response()
        response.json = lambda: self.payload
        return response


def test_list_environments_uses_a_supported_management_api_version():
    client = CopilotStudioClient(_Tokens())
    requests = _Requests(
        {
            "value": [
                {
                    "name": "environment-guid",
                    "properties": {
                        "displayName": "Contoso Production",
                        "linkedEnvironmentMetadata": {
                            "instanceUrl": "https://contoso.crm.dynamics.com/",
                        },
                    },
                }
            ]
        }
    )
    client._requests = requests

    environments = client.list_environments()

    assert requests.url == (
        "https://api.bap.microsoft.com/providers/"
        "Microsoft.BusinessAppPlatform/environments"
    )
    assert requests.params == {
        "api-version": "2023-06-01",
        "$expand": "properties/linkedEnvironmentMetadata",
    }
    assert environments == [
        Environment(
            id="environment-guid",
            display_name="Contoso Production",
            instance_url="https://contoso.crm.dynamics.com",
        )
    ]


def test_list_bots_uses_fields_available_across_dataverse_tenants():
    client = CopilotStudioClient(_Tokens())
    requests = _Requests()
    client._requests = requests

    bots = client.list_bots(
        Environment(
            id="direct",
            display_name="Test",
            instance_url="https://example.crm.dynamics.com",
        )
    )

    assert requests.params == {"$select": "botid,name,schemaname"}
    assert bots[0].id == "bot-1"
    assert bots[0].description == ""


def test_list_solutions_returns_only_stable_solution_metadata():
    client = CopilotStudioClient(_Tokens())
    requests = _Requests(
        {
            "value": [
                {
                    "solutionid": "solution-1",
                    "uniquename": "contoso_agents",
                    "friendlyname": "Contoso Agents",
                    "version": "2.4.0.0",
                    "ismanaged": False,
                }
            ]
        }
    )
    client._requests = requests

    solutions = client.list_solutions(
        Environment(
            id="direct",
            display_name="Test",
            instance_url="https://example.crm.dynamics.com",
        )
    )

    assert requests.params["$filter"] == "ismanaged eq false"
    assert solutions[0].unique_name == "contoso_agents"
    assert solutions[0].friendly_name == "Contoso Agents"
    assert solutions[0].version == "2.4.0.0"
