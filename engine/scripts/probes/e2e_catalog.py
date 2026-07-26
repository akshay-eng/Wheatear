"""Drive `wheatear match-tools` end to end against the real Copilot export,
with both HTTP layers replaced: installed tools by a small stand-in, the
catalog by the actual response bodies captured from the live console.

Proves the wiring and the ranking. Everything except the sockets is real code.
"""
import json
import os
import sys

import requests
from click.testing import CliRunner

HAR = os.environ["WXO_HAR"]  # path to the console HAR capture
EXPORT = os.environ["WXO_EXPORT_DIR"]  # unpacked Copilot managed solution

# Real catalog records, pulled straight out of the browser capture.
har = json.load(open(HAR))
real_records = []
for e in har["log"]["entries"]:
    if "artifacts/list" not in e["request"]["url"]:
        continue
    body = json.loads(e["response"]["content"]["text"])
    if "artifacts" in body:
        real_records = body["artifacts"]
    elif "tools" in body:
        real_records += body["tools"]["data"]
for r in real_records:
    r.pop("icon", None)
print(f"catalog records replayed from the capture: {len(real_records)}")

INSTALLED = [
    {"name": "SNOWMCPALL:get_record",
     "description": "Get a specific record by sys_id\n\nArgs:\n  table: Table to query",
     "input_schema": {"properties": {"table": {}, "sys_id": {}}},
     "binding": {"mcp": {}}, "toolkit_id": "tk-1"},
    {"name": "SNOWMCPALL:perform_query",
     "description": "Perform a query against ServiceNow",
     "input_schema": {"properties": {"table": {}, "query": {}}},
     "binding": {"mcp": {}}, "toolkit_id": "tk-1"},
    {"name": "githubtools:list_pull_requests",
     "description": "List open pull requests",
     "input_schema": {"properties": {"repo": {}}},
     "binding": {"mcp": {}}, "toolkit_id": "tk-2"},
]


class Resp:
    def __init__(self, payload):
        self.status_code, self._p, self.text = 200, payload, ""

    def json(self):
        return self._p


class FakeSession:
    def __init__(self):
        self.headers = {}
        self.sent = []

    def get(self, url, params=None, timeout=None):
        return Resp(INSTALLED if url.endswith("/tools") else [])

    def post(self, url, json=None, timeout=None):
        self.sent.append(json)
        offset = json["offset"]
        cat = json["search_criteria"][0]["filter_groups"][0]["filters"][0]["value"]
        page = real_records if cat == ["tool"] else []
        return Resp({"artifacts": page[offset:offset + json["limit"]],
                     "total": len(page), "offset": offset, "limit": json["limit"]})


requests.Session = FakeSession
sys.modules["wheatear.connectors.orchestrate.rest_client"].get_iam_token = lambda k: "tok"

os.environ["IBMUrl"] = "https://api.us-south.watson-orchestrate.cloud.ibm.com/instances/00000000-0000-0000-0000-000000000000"
os.environ["IBMKey"] = "fake"

from wheatear.cli import main  # noqa: E402

result = CliRunner().invoke(
    main, ["match-tools", EXPORT, "--bot", "crd07_Candidateagent", "--no-llm", "--top", "4"]
)
print(result.output)
if result.exception:
    import traceback
    traceback.print_exception(result.exception)
print("exit code:", result.exit_code)
