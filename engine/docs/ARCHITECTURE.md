# Wheatear architecture — the IR hub

Wheatear is a **hub-and-spoke** migrator, not a set of point-to-point
translators. Adding a platform costs one importer + one exporter, and every
corridor (including the reverse of an existing one) routes through the same
canonical IR. N platforms ⇒ 2N connectors, not N² translators.

```
  Copilot Studio ─importer─┐                        ┌─exporter─▶ Copilot Studio
                           ├──▶  IR (ir/schema.py) ──┤
  watsonx Orchestrate ─importer─┘   Agent / Workflow  └─exporter─▶ watsonx Orchestrate
```

## Pipeline stages (all deterministic except Translate)

| Stage | Module | LLM? | Job |
|---|---|---|---|
| Normalize | `connectors/<platform>/importer.py` | no | source export → `ImportResult` (IR `Agent` + raw refs) |
| Map | `pipeline/map.py` | no | raw refs → target tools/knowledge/connections; **target-aware** |
| Translate | `pipeline/translate.py` | **yes (optional)** | instructions synthesis; deterministic fallback in the CLI |
| Validate | `pipeline/validate.py` | no | structural checks before export |
| Export | `connectors/<platform>/exporter.py` | no | IR → target artifact + `review-manifest.yaml` |

**Determinism first.** Only Translate uses an LLM, and it's optional: with
`--no-llm` (or no API key) the CLI carries the source system prompt over
verbatim (lossless for any generative agent) or assembles instructions from
topics. The AI is the *last mile* for gap-filling, never load-bearing.

## The hub seams

- **`ir/schema.py`** — the one contract. `Agent` (single agent) and `Workflow`
  (a bundle + delegation edges, with leaf-first `migration_order()`).
  `AgentRef` models collaborators without nesting whole agents (cycles OK).
- **`connectors/base.py`** — platform-neutral `ImportResult` / `RawKnowledgeRef`.
  A hub type lives at the hub, not inside one platform package.
- **`connectors/registry.py`** — platform key → importer/exporter modules,
  lazily loaded. The CLI picks by `--from`/`--to`; this is where bidirectionality
  actually lives. A target with no exporter yet fails with a clear message.
- **`pipeline/map.py`** — the *only* place directionality lives: resolution onto
  Orchestrate is a different problem from onto Copilot, chosen by target platform.
- **`errors.py`** — one typed hierarchy (`WheatearError` + subtypes) so every
  expected failure is an actionable message, never a raw traceback. The CLI
  converts them to clean `ClickException`s.

## Bidirectional status (verified end-to-end)

- `copilot-studio → orchestrate` ✅
- `orchestrate → copilot-studio` ✅ (emits a Dataverse solution export that
  re-imports cleanly; round-trip preserves name + instructions)
- Multi-agent: collaborators survive Orchestrate export/import; `Workflow`
  orders leaf-first and terminates on cycles.

## What's deliberately lossy (and surfaced, never silent)

Cross-platform gaps go into `review-manifest.yaml` as a work plan, not dropped:
Copilot connectors ↔ Orchestrate MCP/OpenAPI tools, SaaS knowledge ↔ vector-DB
re-ingestion, model swaps, Teams/M365 channels, content-moderation posture,
connected-agent wiring. See `MIGRATION_DESIGN.md` for the field-by-field matrix.
