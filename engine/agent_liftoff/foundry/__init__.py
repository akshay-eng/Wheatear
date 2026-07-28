"""The adapter foundry: probe two platforms, infer their field mapping once,
and compile it into deterministic code that every later migration reuses.

Agent Liftoff's pipeline (`pipeline/`) migrates *one agent* through hand-written
connectors. The foundry answers a different question: what do you do when a
corridor has no hand-written connector yet, and the user has ten thousand
agents to move?

Doing it with a model per agent is the wrong shape twice over. It costs a
model call per record, and it is non-reproducible -- record 9,000 can be
mapped differently from record 3, with nothing to diff and nothing to review.
So the foundry uses the model exactly once per corridor, to answer "how do
these two schemas correspond?", and then compiles that answer into a pure
Python function. The function is what runs ten thousand times.

    probe both platforms  ->  infer the mapping  ->  compile + test it
                                                          |
                                              cache it, keyed by schema
                                                          |
                    every later migration of that corridor: cache hit, no model

Four agents, each with one job (see the module of the same name):

  `inspector`     probes a platform and returns a `SchemaCorpus` -- every
                  entity, field, type and enum it could reach, from the export
                  archive first and from live APIs second.
  `translator`    correlates two corpora into a `MappingSpec`: field-to-field
                  correspondences, and explicit flags for what has no
                  counterpart. It flags and moves on; it never invents a
                  target for a field that has none.
  `engineer`      compiles a `MappingSpec` into stdlib-only Python, generates
                  positive/negative/edge cases, runs them in a sandbox with no
                  network and no filesystem, and repairs the code until green.
  `orchestrator`  owns the cache. Checks the store first and, on a hit, skips
                  every model call and goes straight to executing the adapter.

Two design choices are worth stating up front because everything else follows
from them:

**Adapters are hub-and-spoke, not point-to-point.** The foundry never
generates a copilot-studio -> orchestrate mapper. It generates a
copilot-studio -> IR *import* adapter and an IR -> orchestrate *export*
adapter, and composes them. N platforms therefore cost 2N adapters rather
than N^2 mappers, a new platform reuses every existing adapter on the far
side, and the IR stays the single contract it is everywhere else in Agent Liftoff.

**The cache is keyed by schema, not by tenant.** A `SchemaCorpus` fingerprints
the *shape* it found -- paths, types, required-ness -- and deliberately not
the data. Two tenants running the same platform version fingerprint
identically, so the second customer to migrate pays no model cost at all, and
a genuine platform upgrade changes the fingerprint and correctly forces a
rebuild.
"""

from agent_liftoff.foundry.types import (
    AdapterArtifact,
    AdapterKey,
    CaseKind,
    Direction,
    EntityKind,
    EntitySchema,
    FieldMapping,
    FieldNode,
    FlagReason,
    MappingSpec,
    ProbeGap,
    ReviewFlag,
    SandboxResult,
    SchemaCorpus,
    TestCase,
    TransformKind,
)

__all__ = [
    "AdapterArtifact",
    "AdapterKey",
    "CaseKind",
    "Direction",
    "EntityKind",
    "EntitySchema",
    "FieldMapping",
    "FieldNode",
    "FlagReason",
    "MappingSpec",
    "ProbeGap",
    "ReviewFlag",
    "SandboxResult",
    "SchemaCorpus",
    "TestCase",
    "TransformKind",
]
