"""Where the shipped platform assets live.

One module answers this so the two catalogue readers -- one from each corridor
-- cannot drift onto different copies of the same file. They previously held
byte-identical 883 KB snapshots at two paths, which is one refresh away from
two corridors disagreeing about what the target platform offers.

Laid out by platform because that is how a person looks for them:

    engine/assets/orchestrate/catalog-snapshot.json
    engine/assets/copilot-studio/adapters/...
"""

from __future__ import annotations

from pathlib import Path

# engine/agent_liftoff/assets.py -> engine/assets
ASSETS = Path(__file__).resolve().parents[1] / "assets"


def platform_dir(platform: str) -> Path:
    return ASSETS / platform


def asset(platform: str, *parts: str) -> Path:
    return platform_dir(platform).joinpath(*parts)
