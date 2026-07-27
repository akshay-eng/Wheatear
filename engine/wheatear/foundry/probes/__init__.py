"""Probe sources: the ways the inspector can find out what a platform's
records look like.

Each source answers the same question from a different vantage point, and none
of them answers it completely:

  `export_scan`     the export archive. Authoritative about structure, free,
                    offline, and silent about anything the vendor strips on
                    export -- endpoints, connection targets, secrets.
  `orchestrate`     a live watsonx Orchestrate instance: installed tools and
                    agents over the IAM-authenticated instance API, plus the
                    global catalog over a console session.
  `copilot_studio`  a live Power Platform environment over Dataverse.

The inspector runs the structural source first and the live ones second, and
merges. What no source could reach becomes a `ProbeGap` -- recorded, with a
remedy, rather than inferred.
"""

from wheatear.foundry.probes.base import ProbeContext, ProbeResult, ProbeSource, observe

__all__ = ["ProbeContext", "ProbeResult", "ProbeSource", "observe"]
