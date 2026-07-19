"""Auto-deploy a generated agent to watsonx Orchestrate via the ADK CLI.

After the export stage writes agent.yaml (and companion tool/knowledge YAML
files), this module shells out to `orchestrate agents import` to push the
agent directly into the user's Orchestrate instance.

The credentials (instance URL + API key) are passed as environment variables
to the subprocess. The exact variable names may vary by ADK version; we set
several common aliases so the CLI finds them regardless of which version the
user has installed.

Requires the `orchestrate` CLI to be available on PATH. The user should have
run `orchestrate env activate ...` or `orchestrate auth login ...` previously
OR rely on the env vars set by the wizard session.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class DeployError(Exception):
    pass


@dataclass
class DeployResult:
    agent_name: str
    agent_path: Path
    success: bool
    output: str
    command: str


def deploy_agent(
    agent_path: Path,
    instance_url: str,
    api_key_env: str,
) -> DeployResult:
    """Import agent.yaml into watsonx Orchestrate using the ADK CLI.

    agent_path   -- path to the generated agent.yaml file
    instance_url -- the Orchestrate service instance URL
    api_key_env  -- name of the env var holding the API key (value already
                    set in os.environ by the wizard credential step)
    """
    if shutil.which("orchestrate") is None:
        raise DeployError(
            "The 'orchestrate' CLI was not found on PATH. "
            "Install the watsonx Orchestrate ADK and run 'orchestrate env activate ...' first, "
            "then re-run Wheatear."
        )

    api_key = os.environ.get(api_key_env, "")

    cmd = ["orchestrate", "agents", "import", "-f", str(agent_path)]

    # Pass credentials in the subprocess environment using multiple alias env
    # var names to cover different ADK versions.
    env = {
        **os.environ,
        "ORCHESTRATE_INSTANCE_URL": instance_url,
        "ORCHESTRATE_SERVICE_INSTANCE_URL": instance_url,
        "WATSONX_ORCHESTRATE_INSTANCE_URL": instance_url,
        "ORCHESTRATE_API_KEY": api_key,
        "WATSONX_ORCHESTRATE_API_KEY": api_key,
    }

    result = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    combined = (result.stdout + result.stderr).strip()
    success = result.returncode == 0
    agent_name = agent_path.parent.name

    return DeployResult(
        agent_name=agent_name,
        agent_path=agent_path,
        success=success,
        output=combined,
        command=" ".join(cmd),
    )
