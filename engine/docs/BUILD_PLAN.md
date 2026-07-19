# Wheatear build plan

Companion to `MIGRATION_DESIGN.md`. That doc is the *what/why* (the mapping);
this is the *how/when* (the build), sequenced and grounded in a real export:
`/copilot-export` (the "Helper Bee" HR agent, a Dataverse solution export).

## Ground truth — current engine vs. the Helper Bee export

Verified by running Normalize + Map on the real export.

**Already works:** solution-format detection · name · the 2,632-char GPT system
prompt · model hint (`GPT5Chat`) · all 13 topics flagged `is_system_topic` ·
SharePoint knowledge source flagged for re-indexing.

**Gaps this one export proves (drive Phase 1):**
| # | Gap | Evidence in Helper Bee |
|---|---|---|
| G1 | Phantom knowledge source | `SearchAndSummarizeContent` → fake KB `search-content` (no `knowledgeSource`) |
| G2 | `configuration.json` never read | drops `contentModeration: Low`, `channels: [msteams, M365Copilot]`, `gptCapabilities.webBrowsing: true`, `GenerativeActionsEnabled: true` |
| G3 | No model tiering | `GPT5Chat` ignored; exporter hardcodes llama-3-3-70b, no review flag |
| G4 | Welcome message dropped | lives in `ConversationStart` `OnConversationStart` SendActivity |
| G5 | Guardrail not structured | LEGAL/COMPLIANCE auto-escalate block is a textbook Orchestrate Guideline, stays prose |

## Phases

### Phase 1 — Fidelity foundation (deterministic, no LLM) ← *in progress*
Close G1–G4 and lay schema for G5. All verifiable without an API key.
- Extend IR: `Guideline`; `Agent.{welcome_message, starter_prompts, guidelines,
  channels, model_family, content_moderation, web_search}`; `ToolRef.{kind, bridge}`;
  `KnowledgeRef.ingest_plan`.
- `solution_importer`: parse `configuration.json`; extract welcome message from
  `ConversationStart`; fix the `search-content` phantom (only explicit
  `knowledgeSource` → KB ref; bare search → `web_search`/semantic flag).
- `model_map.py`: source model → tier → target model, always `review_required`.
- `exporter`: emit `welcome_message`, `starter_prompts`, `style`, `guidelines`,
  tiered `llm`; add channel + model review items to the manifest.
- Tests + full deterministic run against `/copilot-export`.
- **Exit:** `agent.yaml` carries welcome message + tiered model; no phantom KB;
  manifest lists the Teams-channel and model-swap decisions.

### Phase 2 — Structured behavior (LLM-assisted)
- Translate emits structured `Guideline`s from the prompt's conditional guardrails
  (G5), not just a monolithic instructions string.
- Content-moderation level → a guardrail Guideline.
- **Exit:** Helper Bee's legal/compliance rule appears as a discrete Guideline.

### Phase 3 — The connector catalog (the core IP; not exercised by Helper Bee)
- `catalog/*.yaml` (seed with the working SNOWMCP mapping) + `catalog.py` resolver.
- Tier-1 OpenAPI extractor for Copilot custom/REST connectors.
- Map resolves tools via: native MCP → OpenAPI → catalog MCP → manual stub.
- **Exit:** a ServiceNow/Jira export produces a hostable MCP tool + endpoint task.

### Phase 4 — Knowledge re-ingestion planner
- Turn re-index flags into concrete tasks (source → target vector instance),
  enforce the 30 MB upload cap, plan SharePoint/SaaS re-ingestion.

### Phase 5 — Multi-agent & round-trip  ← *spine done*
- ✅ IR-hub made real: `connectors/registry.py` (direction-aware dispatch),
  `connectors/base.py` (neutral `ImportResult`), typed `errors.py`.
- ✅ Bidirectional: added the **Copilot Studio exporter** (`orchestrate →
  copilot-studio`); CLI honors `--from`/`--to`; deterministic `--no-llm` path.
- ✅ Multi-agent: `Workflow` + `AgentRef` collaborators, leaf-first
  `migration_order()` (cycle-safe); collaborators wired through both sides.
- Remaining: importers that discover a *bundle* of agents (multiple files) and
  return a populated `Workflow`, not just one agent + collaborator names;
  reference rewrite to renamed target agents; Copilot connected-agent *import*
  parsing (needs a real multi-agent Copilot export to calibrate against).

### Phase 6 — Live connectors & UX
- Real `pac` / Dataverse pull and `orchestrate` push (deployer).
- Grouped `review-manifest.yaml` as a human work plan; wizard surfaces it.
