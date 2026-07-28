# Agent Liftoff

Agent Liftoff migrates Microsoft Copilot Studio agents and n8n workflows to IBM
watsonx Orchestrate. Its web console wraps the existing Foundry compiler in a
six-step wizard that follows the terminal workflow:

1. Choose Copilot Studio or n8n and a live or uploaded source.
2. Configure the Orchestrate target and deployment policy.
3. Sign in to the source and test both connections.
4. Select source content. Live Copilot migrations choose unmanaged solutions
   first, export and PAC-unpack them, then select agents grouped by solution.
5. Configure the translation model and review the migration manifest.
6. Run the migration, follow the live log, and download the generated bundle.

IBM, n8n, and translation credentials are kept in browser `sessionStorage`,
sent only for discovery or execution, redacted from job events, and removed
from the server-side request when the operation finishes. Microsoft sign-in is
handled by Microsoft; its token cache and Dataverse URLs remain only in
server memory behind an opaque browser-session ID. No credential is written to
an Agent Liftoff configuration file.

## Run locally

Build the UI and install the Python project:

```bash
cd UI
npm ci
npm run build
cd ../engine
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev,anthropic,google,copilot-studio]"
wheatear-web
```

The launcher tries port `8080`, then nearby ports, then an operating-system
assigned port. It prints the active URL:

```text
Agent Liftoff: http://localhost:8080
```

Set `PORT` to choose a preferred port and `WHEATEAR_HOST` to change the bind
address.

## Run with Docker

The helper builds the image, asks Docker for an unused host port, prints the
browser URL, and streams the container logs:

```bash
./scripts/run-docker.sh
```

The image uses port `8080` internally. To run it without the helper:

```bash
docker build -t agent-liftoff .
docker run --rm -p 127.0.0.1::8080 agent-liftoff
```

Use `docker port <container-id> 8080/tcp` to read the host port Docker selected.
A fixed internal port is intentional: Docker can dynamically allocate the host
port only when the container target remains known.

## Source inputs

For a live Copilot Studio source, choose **Sign in with Microsoft**, complete
the Microsoft-hosted authentication, then search and select one of the Power
Platform environments Agent Liftoff discovers. No Dataverse URL or bearer token
is required. Offline migration accepts an unpacked-solution ZIP containing
`solution.xml` and `bots/`.

n8n accepts a base URL plus API key, or one or more workflow JSON exports.
Selecting a supervisor automatically includes the agents or workflows it
delegates to.

The Orchestrate console cookie is optional. When present, the Copilot corridor
reads the live tool catalog; otherwise it uses the catalog snapshot shipped
with Agent Liftoff.

## Tests

```bash
cd engine
pytest

cd ../UI
npm test
npm run build
npm run test:e2e
```
