"""Platform connector registry: the seam that makes Wheatear IR-centric.

Each platform contributes an importer module (source -> IR) and an exporter
module (IR -> target). The CLI/pipeline pick these by the `--from`/`--to`
platform keys instead of hardcoding one direction, so every supported corridor
-- including the reverse of one already built -- routes through the same code.

Modules are imported lazily (by dotted path) so pulling in one platform's
optional dependencies never forces the others'.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from types import ModuleType

from wheatear.errors import UnsupportedCorridorError


@dataclass(frozen=True)
class PlatformSpec:
    key: str
    display_name: str
    importer_module: str
    exporter_module: str | None  # None until a reverse-direction exporter exists


PLATFORMS: dict[str, PlatformSpec] = {
    "copilot-studio": PlatformSpec(
        key="copilot-studio",
        display_name="Microsoft Copilot Studio",
        importer_module="wheatear.connectors.copilot_studio.importer",
        exporter_module="wheatear.connectors.copilot_studio.exporter",
    ),
    "orchestrate": PlatformSpec(
        key="orchestrate",
        display_name="IBM watsonx Orchestrate",
        importer_module="wheatear.connectors.orchestrate.importer",
        exporter_module="wheatear.connectors.orchestrate.exporter",
    ),
}


def _spec(platform: str) -> PlatformSpec:
    spec = PLATFORMS.get(platform)
    if spec is None:
        known = ", ".join(sorted(PLATFORMS))
        raise UnsupportedCorridorError(f"Unknown platform '{platform}'. Known platforms: {known}.")
    return spec


def load_importer(platform: str) -> ModuleType:
    """Return the importer module for `platform` (has detect_format, import_agent)."""
    return importlib.import_module(_spec(platform).importer_module)


def load_exporter(platform: str) -> ModuleType:
    """Return the exporter module for `platform` (has export_agent).

    Raises UnsupportedCorridorError if the platform has no exporter yet, so a
    half-supported direction fails with a clear message, not an AttributeError.
    """
    spec = _spec(platform)
    if spec.exporter_module is None:
        raise UnsupportedCorridorError(
            f"Migrating *to* {spec.display_name} isn't supported yet (no exporter)."
        )
    return importlib.import_module(spec.exporter_module)
