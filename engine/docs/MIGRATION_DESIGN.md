# Copilot Studio → watsonx Orchestrate: mapping & migration design

Status: design reference. Grounds the `Map` / `Translate` stages and the connector
catalog. Derived from a side-by-side audit of both GUIs (see the object model below)
plus the existing IR in `wheatear/ir/schema.py`.

---

## 1. The one idea that shapes the whole engine

Copilot Studio and Orchestrate look far apart but are architecturally close at the
top: **both are generative, LLM-orchestrated agents** made of `instructions + model +
knowledge + tools + collaborator agents`. Copy the system prompt across and you're 60%
done. They diverge sharply in exactly **two** places:

1. **The tool/connector supply chain.** Copilot ships ~1,500 prebuilt Power Platform
   connectors (ServiceNow, Jira, Salesforce, SAP…). Orchestrate has **no prebuilt SaaS
   connector catalog** — every tool arrives via MCP server, OpenAPI import, catalog, or
   an agentic workflow. This is the migration bottleneck.
2. **Knowledge ingestion.** Copilot knowledge is *live-indexed SaaS content*
   (SharePoint, Dataverse, Salesforce…). Orchestrate knowledge is *vector-DB instances
   you already own* (Milvus, Elasticsearch, Astra, OpenSearch) or a ≤30 MB file upload.
   There is no "point at SharePoint" button on the Orchestrate side.

Everything else either maps 1:1 or **folds into the instructions string**. So the engine's
real IP is (a) the **connector catalog** that resolves Copilot connectors → Orchestrate
tools, and (b) the **knowledge re-ingestion planner**. The LLM translator is the easy part.

---

## 2. Platform object model (what each GUI actually exposes)

### Copilot Studio (source)
| Area | Fields / objects observed |
|---|---|
| Overview | Name, Description, Agent status/warnings, **Instructions** (system prompt), Model (e.g. *Claude Opus 4.6*), Monitor metrics |
| Orchestration mode | Generative ("dynamic") vs Classic (topic-driven); **Deep reasoning** (preview) |
| Knowledge | SharePoint, OneDrive, Dataverse, Dynamics 365, Salesforce, ServiceNow, Azure AI Search, Azure SQL; *Advanced:* Snowflake, Databricks, Confluence, Oracle DB, SAP OData, Zendesk, Bing Custom Search; **Web Search** toggle; file upload |
| Tools | **Connectors** (~1.5k catalog), **Prompt**, **Flow / Agent flow**, **REST API**, **MCP**, **Computer use**, **Custom connector** (OpenAPI under the hood) |
| Work IQ | M365 personalization layer (no analog anywhere) |
| Triggers | Event-driven activation ("when X happens") |
| Agents | Connected / child agents ("let other agents use this one") |
| Topics | Dialog units w/ trigger phrases + nodes. System topics: Greeting, Goodbye, Escalate, Fallback… |
| Suggested prompts | Teams/M365 conversation starters |
| Settings | Content **moderation level** (slider), Response **formatting**, **User feedback**, Security, Connection settings, **Entities**, **Skills** (legacy), **Voice**, **Languages**, Component collections |

### watsonx Orchestrate (target)
| Area | Fields / objects observed |
|---|---|
| Profile | Name, **Description**, **Welcome message**, Model (e.g. *GPT-OSS 120B*), **Starter prompts** |
| Agent style | **Default** (intrinsic planning) vs **ReAct** (think/act/observe loop) |
| Voice modality | Preview |
| Knowledge | Sources = **instances you own**: Milvus (recommended), Elasticsearch, Astra DB, OpenSearch, Custom service; or **file upload ≤30 MB** |
| Toolset | Tools from **Catalog / Local instance / MCP server / OpenAPI**; create **Agentic workflow** |
| Behavior | **Instructions** (system prompt) + **Guidelines** (structured `Name / Condition / Action / optional Tool`) + Chat-with-documents toggle |
| Agents | Collaborators to delegate to; add via Catalog / Local / Import |
| Channels | Preview |
| Scheduling | Run prompts/workflows on a schedule |

---

## 3. Comparison matrix (field → field, with fidelity)

Fidelity tiers: **Direct** (copy, ~1.0) · **Adapted** (LLM/derive, 0.7–0.9) ·
**Bridged** (needs a catalog/OpenAPI/instance, 0.4–0.7, review) ·
**Manual** (no clean target, 0.0, review).

| # | Copilot Studio | → Orchestrate | Fidelity | Notes |
|---|---|---|---|---|
| 1 | Name | Profile ▸ Name | Direct | — |
| 2 | Description | Profile ▸ Description | Direct | — |
| 3 | Instructions (system prompt) | Behavior ▸ Instructions | Direct→Adapted | Highest-value carry-over. Strip MS-specific refs (SharePoint, Work IQ) during Translate. |
| 4 | Greeting/first message | Profile ▸ Welcome message | Adapted | Extract from Greeting system topic; 100-char cap on target. |
| 5 | Suggested prompts | Profile ▸ Starter prompts | Direct | Clean 1:1. |
| 6 | Model (Claude Opus 4.6…) | Profile ▸ Model | Adapted | No 1:1 model. Map to nearest available *family/tier* (see §5). Always review. |
| 7 | Generative orchestration = **Yes** | Agent style ▸ Default | Direct | Both generative-first. |
| 8 | Deep reasoning (preview) | Agent style ▸ ReAct | Adapted | Closest analog to explicit reason/act. |
| 9 | Classic orchestration (topic-driven) | *(no analog)* | Manual | Orchestrate is generative-only. Decompose topics → instructions + Guidelines. |
| 10 | Custom topic (real business logic) | Behavior ▸ Instructions and/or a Guideline | Adapted | Condition→Guideline.Condition, node actions→Action. Big opportunity (see §6). |
| 11 | System topics (Goodbye/Escalate/Fallback) | *(drop)* / Welcome msg | Direct | `is_system_topic` already flags these. |
| 12 | Entities / slots | *(fold into instructions)* | Manual | No entity concept on target. |
| 13 | **Knowledge: uploaded files** | Knowledge ▸ Upload files | Bridged | Re-upload; **≤30 MB cap** — split/flag if larger. |
| 14 | **Knowledge: SharePoint/OneDrive** | *(no analog)* → Milvus/ES + re-ingest | Bridged | Content must be re-indexed into a vector instance. Planner emits a re-ingest task. |
| 15 | Knowledge: Salesforce/ServiceNow/Dataverse/Snowflake… | Custom service **or** re-ingest to vector DB | Bridged | Same story as #14; live SaaS retrieval isn't reproducible as-is. |
| 16 | Knowledge: Azure AI Search | Elasticsearch / OpenSearch / Milvus | Bridged | Index migration; closest structural match. |
| 17 | Web Search toggle | *(no analog)* → web-search **tool** | Manual | Add a web-search MCP/tool if behavior is required. |
| 18 | **Tool: prebuilt connector** (ServiceNow, Jira…) | Toolset ▸ MCP server / OpenAPI | Bridged | **The core problem — see §7 catalog.** |
| 19 | Tool: Custom connector (OpenAPI) | Toolset ▸ OpenAPI import | Direct→Bridged | Copilot custom connectors *are* OpenAPI → highest automatable fidelity. Extract & re-import the spec. |
| 20 | Tool: REST API | Toolset ▸ OpenAPI import | Adapted | Wrap the endpoint as an OpenAPI tool. |
| 21 | Tool: MCP | Toolset ▸ MCP server | Direct | Both speak MCP — re-point the endpoint. Cleanest tool corridor. |
| 22 | Tool: Prompt | Guideline or child prompt-agent | Adapted | — |
| 23 | Tool: Flow / Agent flow | Toolset ▸ **Agentic workflow** | Manual | Concept matches; Power Automate JSON ≠ Orchestrate workflow. Rebuild (LLM can skeleton it). |
| 24 | Tool: Computer use | *(no analog)* | Manual | Flag; no target capability. |
| 25 | Connected / child agents | Behavior ▸ Agents (collaborators) | Adapted | Migrate **leaf agents first**, then wire references (see §8). |
| 26 | Content moderation level (slider) | *(no slider)* → Guideline | Manual | Encode as a guardrail Guideline / instruction clause. |
| 27 | Response formatting | Guideline or instructions clause | Adapted | — |
| 28 | Triggers (events) | Scheduling (partial) / Channels | Manual | Only *scheduled* triggers have a home; true event triggers don't. |
| 29 | Channels (Teams, M365) | Channels (preview) | Manual | Different channel targets; Teams ≠ Orchestrate channels. |
| 30 | Skills (legacy) | Toolset ▸ tool / Agentic workflow | Manual | Deprecated on both; re-implement as tools. |
| 31 | Voice | Voice modality (preview) | Adapted | Both preview. |
| 32 | Languages / Language understanding | *(model-native)* | Direct | Generative models handle multilingual; drop the config. |
| 33 | Work IQ | *(no analog)* | Manual | Drop + flag. |
| 34 | User feedback (thumbs) | *(platform-native telemetry)* | Direct | Nothing to migrate. |

**Reading of the matrix:** ~1/3 Direct, ~1/3 Adapted (LLM/derive), ~1/3 Bridged/Manual —
and *almost all the Bridged/Manual rows are tools + knowledge*. That's the whole game.

---

## 4. IR changes this implies (`wheatear/ir/schema.py`)

Today the IR has `tools / knowledge / connections` plus `topics`. To carry the matrix,
extend it (additive, keeps existing importers working):

- **`Agent`**: add `model_family` (normalized tier, not raw name), `agent_style`
  (`default|react`), `welcome_message`, `starter_prompts: list[str]`,
  `guidelines: list[Guideline]`, `collaborators: list[AgentRef]`,
  `triggers: list[TriggerRef]`, `channels: list[str]`.
- **New `Guideline`**: `name, condition, action, tool_ref: str | None` — this is
  Orchestrate's structured behavior primitive and a *decomposition target* for topics.
- **`ToolRef`**: add `kind` (`connector|custom_connector|rest|mcp|flow|prompt|computer_use`)
  and `bridge` (`mcp_catalog|openapi|manual|native_mcp`) so Map records *how* it resolved,
  not just *that* it did.
- **`KnowledgeRef`**: add `ingest_plan` (`upload|reindex_vector|custom_service|unsupported`)
  and `size_bytes` (to enforce the 30 MB rule).

`needs_review` logic stays as-is; the new bridge/ingest_plan enums just feed it.

---

## 5. Model mapping (§ row 6)

Never map by exact name. Normalize source model → a **tier**, then pick the best target
model in that tier from a config table (so it updates without code changes):

```
opus/gpt-4-class  → best available frontier tier on Orchestrate
sonnet/gpt-4o-mini→ mid tier (e.g. a granite-3/large or gpt-oss-120b)
haiku/small       → small/fast tier
```
Emit `review_required` on every model row — capability parity is never guaranteed and a
human should confirm the swap.

---

## 6. Turning Copilot topics into Orchestrate Guidelines (a real win)

Orchestrate's **Guidelines** (`Condition → Action → optional Tool`) are the natural
landing spot for the *conditional* logic buried in Copilot topics — and Copilot has no
equivalent structured feature, so this is an *upgrade*, not a loss. Map:

```
Copilot topic:  trigger phrase / condition node   →  Guideline.condition
                message / action nodes             →  Guideline.action
                connector call in the node         →  Guideline.tool_ref
```
Do this in **Translate** (it's semantic), but keep it *structured* — emit Guideline objects,
not prose folded into the system prompt. System topics (`is_system_topic`) are excluded.

---

## 7. The connector gap — recommended strategy (the crux of your question)

Your instinct to build **a catalog of MCP servers** is correct. Don't make the user
"figure it out" from zero, and don't blanket-offer to host everything either. Use a
**3-tier resolver**, tried in order per connector:

**Tier 1 — OpenAPI auto-conversion (automate first, highest fidelity).**
Copilot **custom connectors and REST tools are OpenAPI under the hood** (row 19/20).
Extract the spec from the export and emit an Orchestrate OpenAPI tool import. Near-zero
human work, no new hosting. Do this *before* reaching for MCP.

**Tier 2 — Curated MCP catalog (the core IP).**
For the top ~30–50 enterprise connectors (ServiceNow, Jira, Salesforce, SAP, Workday,
Zendesk, Confluence, Box, Slack…), ship a catalog entry:

```yaml
# catalog/servicenow.yaml
copilot_connector_ids: ["shared_service-now", "servicenow", ...]
mcp_server:
  name: SNOWMCP                      # matches what Orchestrate already ingests
  image: ghcr.io/<org>/servicenow-mcp:latest
  transport: http
  auth: { type: oauth2, scopes: [...] }
  operations: [create_incident, add_comment, add_work_notes, approve_change, ...]
deploy:
  self_host: "docker run ... ENDPOINT"        # default
  managed:   "request a hosted endpoint"      # optional/premium
notes: "Maps Copilot ServiceNow connector actions 1:1 where operation names align."
```
The `Map` stage looks up the Copilot connector id here and, if found, produces a tool
whose `bridge = mcp_catalog` plus a **review-manifest task**: *"Host this MCP server (or
request a managed endpoint), then paste the endpoint URL."* Your screenshot shows
Orchestrate already consuming exactly this shape (`SNOWMCP:add_change_task`), so it's proven.

Default the deploy mode to **self-host** (a `docker run` / compose recipe + creds the
customer owns). Offer **managed hosting** only as an opt-in premium for design partners —
enterprise security teams will not route ServiceNow/SAP credentials through a third-party
host by default, so self-host is the safer default and dramatically lowers your liability
and ops burden. Managed is a great upsell, not the baseline.

**Tier 3 — Manual stub + guidance.**
Anything unresolved: emit a review-manifest entry naming the connector, the specific
operations the agent actually used (harvested from topics/flows), and the suggested path
(build an MCP server / find a community one / wrap the REST API). Never silently drop.

**Coverage math:** Tier 1 clears every custom/REST connector automatically; Tier 2 clears
the long-tail-but-common SaaS names; Tier 3 catches the rest with a clear to-do. That's the
80/20 without you having to host the world.

---

## 8. Multi-agent migration order

Connected agents form a graph. Migrate **leaf-first** (topological order): a collaborator
must exist on the target before a parent can reference it. The engine should:
1. discover the connected-agent graph during Normalize,
2. migrate agents bottom-up,
3. rewrite `collaborators[].ref` to the new Orchestrate agent ids,
4. flag cycles for human resolution.

---

## 9. Fidelity & the review manifest

Every non-Direct mapping already flows through `needs_review`. The `review-manifest.yaml`
should group by the matrix's action type so a human sees a **work plan**, not a diff:

- **Host these MCP servers** (Tier 2 connectors) — with endpoints to paste back.
- **Re-ingest this knowledge** (rows 14–16) — source → target vector instance.
- **Rebuild these flows** as agentic workflows (row 23).
- **Confirm model swap** (row 6).
- **Unsupported, dropped** (Work IQ, computer use, event triggers) — explicit, not silent.

---

## 10. Concrete next steps in the engine

1. Extend the IR (§4) — additive, no importer breakage.
2. Add `catalog/` (YAML per connector) + a `catalog.py` resolver used by `Map`.
3. Implement the **Tier-1 OpenAPI extractor** for Copilot custom/REST connectors (biggest
   automatable win, do it first).
4. Teach **Translate** to emit structured `Guideline`s from topics (§6), not just prose.
5. Add the **knowledge ingest planner** (§3 rows 13–16) to `Map`.
6. Restructure `review-manifest.yaml` into the §9 grouped work plan.
7. Seed the catalog with the SNOWMCP mapping you already have working end-to-end.
