"""watsonx Orchestrate implementation of TargetModelSource.

Shells out to the `orchestrate` ADK CLI (`models list --raw`) rather than
hitting a raw REST endpoint directly -- same convention `deployer.py` already
uses for imports, and the CLI already owns the auth/env-activation dance
(IAM token exchange, workspace resolution) that would otherwise have to be
duplicated here. See docs/model-matrix-research.md for how the --raw output
shape was confirmed against a live tenant.

Live-checked against a real dev tenant (2026-07-26): this tenant's admin has
restricted the allowed list down to two entries. That's the entire reason
this module exists instead of just trusting a static table -- see the
`~*review note*~` in `wheatear/model_map.py`.
"""

from __future__ import annotations

import re
import shutil
import subprocess

from wheatear.errors import WheatearError

_LINE_RE = re.compile(r"^-\s*(?P<symbols>[✔★◆✖\s]*)(?P<id>\S+):\s*(?P<description>.*)$")
_DISALLOWED_SYMBOL = "✖"


class OrchestrateCliUnavailableError(WheatearError):
    """The `orchestrate` ADK CLI isn't installed/on PATH."""


class OrchestrateModelSource:
    """TargetModelSource backed by a live `orchestrate` CLI session.

    Assumes the caller has already run `orchestrate env activate <name>
    --api-key ...` (or has a valid cached token) for the environment they
    want models from -- this class doesn't manage credentials itself, same
    division of responsibility as `rest_client.py`'s IAM token exchange.
    """

    def list_available_models(self, *, include_non_preferred: bool = False) -> list[str]:
        if shutil.which("orchestrate") is None:
            raise OrchestrateCliUnavailableError(
                "The 'orchestrate' ADK CLI was not found on PATH. Install it with "
                "'pip install ibm-watsonx-orchestrate' and activate an environment "
                "('orchestrate env activate <name> --api-key ...') before resolving "
                "target models."
            )

        cmd = ["orchestrate", "models", "list", "--raw"]
        if include_non_preferred:
            cmd.append("--all")

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return _parse_model_list(result.stdout)


def _parse_model_list(raw_output: str) -> list[str]:
    """Parse the ADK CLI's `--raw` bullet-list output into raw model ids.

    Models marked disallowed by the tenant admin (✖) are excluded -- they
    show up in the list but can't actually be used, so returning them would
    let the scorer recommend a model the target tenant will then reject.
    """
    ids: list[str] = []
    for line in raw_output.splitlines():
        match = _LINE_RE.match(line.strip())
        if not match:
            continue
        if _DISALLOWED_SYMBOL in match.group("symbols"):
            continue
        ids.append(match.group("id"))
    return ids
