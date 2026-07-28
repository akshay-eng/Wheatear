"""Why did 988 of 1173 detail fetches fail? Cookie expiry, rate limiting, or
artifacts that genuinely have no detail record are three different problems.
"""
import os
import sys
import time

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from agent_liftoff.connectors.orchestrate.catalog_client import (  # noqa: E402
    OrchestrateCatalogClient,
    to_artifacts,
)

client = OrchestrateCatalogClient(os.environ["IBMUrl"])
arts = to_artifacts(client.list_installable())
print(f"catalog list still works: {len(arts)} artifacts  <- so the cookie is alive")

print("\nfetching 12 details back to back, no pacing:")
codes = []
for artifact in arts[:12]:
    r = requests.get(
        f"{client.base}/artifacts/{artifact.id}",
        headers=dict(client._session.headers),
        timeout=30,
    )
    codes.append(r.status_code)
    body = "" if r.status_code < 400 else " ".join(r.text.split())[:80]
    print(f"  {r.status_code}  {artifact.install_ref:<40} {body}")

print("\nsame 12 again, 400ms apart:")
ok = 0
for artifact in arts[:12]:
    time.sleep(0.4)
    r = requests.get(
        f"{client.base}/artifacts/{artifact.id}",
        headers=dict(client._session.headers),
        timeout=30,
    )
    ok += r.status_code < 400
print(f"  {ok}/12 succeeded when paced")
