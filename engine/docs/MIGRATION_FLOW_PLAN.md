# Complete migration flow plan: Copilot Studio / n8n → watsonx Orchestrate

Status of this doc: an architecture + feasibility plan, grounded in the code
that already exists (`wheatear/ir/schema.py`, `pipeline/`, `connectors/`,
`model_matrix/`) and in live-verified facts about the Orchestrate API/ADK
(2026-07-26). Read alongside `MIGRATION_DESIGN.md`, `ARCHITECTURE.md`,
`model-matrix-research.md`, and the n8n corridor notes.

---

## 0. Reality check — ~60% of what you described already exists

Before planning new work, the honest map of "already built" vs "new," because
your description overlaps heavily with the current engine:

| Your ask | Current state |
|---|---|
| Intermediate format listing agent name/description/model/instructions/guidelines/tools/collaborators | **EXISTS** — `ir/schema.py` `Agent` + `Workflow`. This *is* the IR. |
| Tool with name, MCP url, transport, member tools, confidence, review flag | **EXISTS** — `ToolRef` (ToolKind, BridgeStrategy, mcp_server_url, transport, member_tools, review_required, notes). |
| "add a flag so creds can be asked later" | **PARTIAL** — `ConnectionRef.review_required` defaults True. But tools don't yet carry an explicit `requires_credentials` flag or a link to their connection (see §1). |
| Built-in-connector flag / OpenAPI-tool flag | **PARTIAL** — `ToolKind` has CONNECTOR / CUSTOM_CONNECTOR / REST / MCP / FLOW / PROMPT, so the *kind* is modeled, but tool `description` and `input_schema` aren't captured yet (see §1). |
| KB flagging for default connectors (SharePoint/Dropbox/DB), file uploads | **PARTIAL** — `KnowledgeRef` + `IngestPlan` (UPLOAD / REINDEX_VECTOR / CUSTOM_SERVICE / UNSUPPORTED) exist; the file-upload-path and MCP-stand-up sub-flows aren't wired (see §1, §3). |
| Field-mapping "instruction → behaviour" | **EXISTS for the main fields** — `exporter.py` maps instructions, guidelines, collaborators, tools, knowledge_base, welcome_content, starter_prompts. |
| AI folds unsupported features into existing fields | **PARTIAL** — `translate.py` synthesizes instructions from a dialog graph / adapts a system prompt. The general "feature X has no field → fold into Y" logic is not generalized (see §4). |
| Model source→target via matrix, exact/rule/AI/user-select | **PARTIAL** — `model_matrix/` does resolution + scoring + ranking. The AI-fallback-with-justification and interactive user-select loop aren't wired (see §5). |
| Compare source connectors against Orchestrate catalog JSON | **NOT BUILT** — `map.py` `KNOWN_TOOL_MAPPINGS` is an empty dict, exactly the seam for this (see §6). |
| n8n importer | **NOT BUILT** — only `copilot_studio` and `orchestrate` importers exist (see §2, and the n8n corridor notes). |
| Dependency-ordered deploy | **PARTIAL** — `Workflow.migration_order()` sorts *agents* leaf-first; full multi-entity deploy ordering + the async tool-population wait is not built (this is the biggest loophole — §7). |
| Behavioral validation / repair | **PARTIAL** — `eval/generate_cases.py`, `repair.py`, `pipeline/validate.py` exist as a start. |

**Implication:** this is an *extension* project, not a greenfield build. The
new work is: (a) an n8n importer, (b) three interactive resolution loops
(model / tool / KB), (c) a real deploy planner+executor, (d) the AI
"feature-folder," and (e) the hackathon agent harness. The IR is the stable
contract everything hangs off — extend it, don't replace it.

---

## 1. IR extensions needed (concrete, additive)

Add these fields; nothing existing changes shape, so importers/exporters/tests
keep working.

```python
# ToolRef additions
description: str | None = None          # tool card text (what it does)
input_schema: dict | None = None        # the input it takes (JSON Schema)
requires_credentials: bool = False      # THE flag you asked for
credential_ref: str | None = None       # links to a ConnectionRef.ref
is_builtin_connector: bool = False      # derived from kind == CONNECTOR, but
                                        # explicit so the exporter/UI can branch
catalog_match_id: str | None = None     # Orchestrate catalog tool UUID once resolved
catalog_match_confidence: float = 0.0

# KnowledgeRef additions
requires_credentials: bool = False      # true for Milvus/Elastic/AstraDB-backed
credential_ref: str | None = None
is_file_upload: bool = False            # direct files attached in source
file_paths: list[str] = []              # populated by the human when asked
mcp_server_url: str | None = None       # if we stand up an MCP server for it
catalog_match_id: str | None = None     # matched Orchestrate KB config

# ConnectionRef additions
provided: bool = False                  # flips true once the human supplies creds
                                        # (creds themselves NEVER stored in IR)
```

Credentials note: the IR carries the *fact that a credential is needed* and a
*reference*, never the secret value. Secrets go straight into Orchestrate via
`orchestrate connections set-credentials` (verified live) — same boundary the
codebase already draws.

---

## 2. Phase A — Parse → IR (deterministic, no AI, no human)

Per corridor, produce a `Workflow` (multi-agent) + a **QuestionSet** (the list
of things a human/AI must resolve before deploy).

**Copilot Studio** (importer exists; extend to fill new IR fields): unzip the
solution → parse bots/botcomponents → topics, generative instructions, model
hint, connector refs, MCP refs, knowledge refs, connected agents. Already
distinguishes `is_system_topic` scaffolding from real logic.

**n8n** (new importer — see the n8n corridor notes for the full node map): parse
workflow JSON → detect Class A (has `@n8n/n8n-nodes-langchain.agent`) vs Class B
(pure automation, no Orchestrate home). For Class A: each Agent node → IR Agent;
`toolWorkflow` node → collaborator *or* tool depending on the referenced
workflow's class; `mcpClientTool` → MCP ToolRef (clean); connector nodes →
CONNECTOR ToolRef flagged for catalog lookup + creds; `credentials` blocks →
ConnectionRef; vector-store/file chains → KnowledgeRef.

Output of Phase A is a single artifact you can inspect/serialize (the IR is
Pydantic — `.model_dump_json()`), plus the QuestionSet grouped by type:
models to confirm, tools needing creds, tools needing implementation, KBs
needing re-ingestion, files needing paths, collaborators pointing outside the
bundle.

---

## 3. Phase B — Interactive resolution (AI + human, three loops)

This is the heart of what's new. Each loop reads flags from the IR, resolves
them (deterministic first, AI second, human last), and writes the resolved
value back into the IR. Nothing deploys yet.

### B1. Model resolution loop
1. For each agent's `model_hint`, call `model_matrix.recommend(hint, OrchestrateModelSource())`.
2. **Exact match** (confidence 1.0, same family available on target) → auto-accept, log it. Happy path.
3. **Rule-based suggestion** (confidence ≥ threshold, e.g. 0.5) → present top pick + rationale, one-tap accept.
4. **Low confidence** (< threshold) → **AI Model Recommender agent**: give it the source profile + the live target list + why the rule engine was unsure, get back a ranked recommendation *with written justification*, present as a pick-list to the human.
5. **No models available on target** → hard stop, prompt the human to enable models (needs tenant admin; the tool can't do it). Verified live: this tenant had exactly 2 allowed models.

### B2. Tool resolution loop (per flagged tool)
1. **MCP with URL** → already resolved by Map (NATIVE_MCP). Only action: confirm reachability + register the toolkit at deploy time.
2. **Built-in connector** → **catalog match**: compare against your Orchestrate catalog JSON (1152 tools). Deterministic exact-name/app match first; then **AI Tool-Catalog Matcher** for semantic matches ("n8n Slack post message" ≈ catalog "Send a message in Slack"). On a confident match, set `catalog_match_id`, then collect creds for that catalog tool's connection.
3. **Credential collection** — for every tool with `requires_credentials`, ask the human **one connection at a time**, deduplicated by connection (not per-tool — five Slack tools sharing one cred = one question). Feed straight into `orchestrate connections add/configure/set-credentials`. The n8n-credential-type → `--kind` mapping is a static table (see the tool-resolution notes).
4. **No catalog match, but it's a plain REST call** → **AI OpenAPI Synthesizer**: draft an OpenAPI spec (for n8n, mine the exact method/path from n8n's own open-source node — deterministic skeleton, AI writes only the description). Import via `orchestrate tools import -k openapi`. Still needs the human's creds + a confirm.
5. **Inline code tool** (`toolCode`, calculator) → target `orchestrate tools import -k python` (verified live: produces `binding.python`). AI ports the JS→Python; human reviews.
6. **Nothing works** → flag MANUAL, emit a stub + instructions, don't fake it.

### B3. Knowledge-base resolution loop (per flagged KB)
1. **Vector-DB-backed** (Milvus/Elastic/AstraDB — the KB connection types are real, verified in the ADK) → **catalog match** against your target-KB-formats JSON; if a matching connection type exists, collect its creds (`connections add --component knowledge --category milvus`) and re-index. Verified live: Orchestrate always re-embeds with `ibm/slate-125m-english-rtrvr-v2` — there is never a "copy the vectors" shortcut, so `REINDEX_VECTOR` is mandatory.
2. **Default SaaS connector KB** (SharePoint/Dropbox/etc. with no direct Orchestrate equivalent) → offer to **stand up an MCP server**: ask the human for creds, generate the server, tell them to deploy it, they return the deployed URL, you write it into `KnowledgeRef.mcp_server_url` and treat it as an MCP tool thereafter.
3. **Direct file uploads** (`is_file_upload`) → the export never contains file bytes (proven twice this session). Ask the human for current file paths, enforce Orchestrate's 30MB/file cap, upload via `orchestrate knowledge-bases`. If they can't provide paths, flag for manual re-attach post-migration.

At the end of Phase B the IR is "resolved": every flag either has a value or an
explicit MANUAL marker. This resolved IR is the thing you deploy from.

---

## 4. The AI "Feature-Folder" (features with no target field)

Your example — Copilot skills → condensed into Orchestrate behaviour — is the
general case: a source feature that has no 1:1 Orchestrate field must be folded
into a field that *does* exist, without silent loss. Generalize `translate.py`
into a **Feature-Folder agent** with a fixed contract:

- Input: the orphan feature (skill / content-moderation posture / trigger /
  channel / web-search capability) + the current draft `instructions` +
  `guidelines`.
- Output (structured): a decision per orphan — `fold_into: instructions | guidelines | drop`, the exact text to add, a confidence, and a note.
- Rule of thumb baked into the prompt: behavioral rules → a `Guideline`
  (condition/action, which Orchestrate has natively); tone/skill/persona →
  `instructions`; genuinely unrepresentable (Teams channel, moderation slider)
  → `drop` + a review-manifest note, never a silent drop.

This is deterministic-adjacent: the *routing table* (which feature type tends to
go where) is fixed; the AI only writes the prose and handles ambiguity.

---

## 5. Phase C — Deploy planning (deterministic) — **the loophole you spotted**

You're exactly right that emitting all YAML and pushing it won't work: the
entities are interdependent. The current exporter writes files; it does **not**
sequence live creation. You need a **DeployPlanner** that topologically sorts
*all* entity types, not just agents:

```
1. Connections (credentials)          — nothing depends on nothing; first.
2. Toolkits / tools                   — depend on connections.
   2a. WAIT for async tool population — verified live: POST /toolkits returns
       tools:[] immediately; the individual tools appear later. The agent that
       references a specific tool UUID can't be created until they exist. Poll
       GET /toolkits/{id} (or /tools?toolkit_id=) until populated, with a timeout.
3. Knowledge bases                    — depend on connections; re-index is async
       too (status: ready). Poll to ready.
4. Leaf agents                        — depend on tools + KBs + connections.
5. Parent/supervisor agents           — depend on leaf agents (collaborators).
```

`Workflow.migration_order()` already gives you step 4→5 (leaf-first). The new
work is steps 1–3 and the **two async waits** (tool population, KB indexing) —
those are the parts most likely to make a naive "push it all" deploy fail
intermittently.

Second-order gotcha, verified live: Orchestrate agents reference tools by
**UUID**, not name (`agent.tools: [uuid,...]`). The exporter currently writes
`tools: [name]`. Either (a) rely on the ADK `orchestrate agents import` to
resolve names→UUIDs at import time (needs verifying it does), or (b) for a
REST-driven deploy, resolve names→UUIDs *after* step 2 and inject them into the
agent spec at step 4. Design for (b); it's the robust path.

---

## 6. Phase D — Deploy execution + validation

- Execute the DeployPlan in order via the REST client (create/verify each entity, capture returned UUIDs, thread them forward). Idempotency: check-then-create so a re-run doesn't duplicate (the tenant already has real work in it — verified).
- After each agent is live, run the **Validator**: `eval/generate_cases.py` generates test utterances; drive the deployed agent; compare behavior to the source's intent. On failure, the **Repair agent** (`repair.py`) proposes an instruction/guideline fix and re-deploys. This is the only way to catch "imported cleanly but behaves wrong," which LLM-translated instructions can absolutely do.

---

## 7. Loopholes & failure modes (ranked by how likely they are to bite)

1. **Dependency ordering + async readiness on deploy** (you spotted the ordering half). The async half — toolkit created ≠ tools queryable, KB created ≠ index ready — is the sneakier one; it makes deploys fail *intermittently*, which is worse than failing every time. Mitigation: the DeployPlanner + poll-to-ready in §5.
2. **Tool-by-UUID vs tool-by-name** (§5). Silent-ish: an agent can import with an unresolved/stale tool ref and simply not call the tool at runtime.
3. **No catalog/MCP path for a connector** → genuinely can't auto-create; needs human to build a tool or stand up a server. Bounded by how good the catalog match rate is; unknowable until you run it against real exports.
4. **Model not enabled on target tenant** → hard stop needing admin. The matrix picks correctly but can't grant access.
5. **Round-trip fidelity loss**: the live Orchestrate agent has ~35 fields; the exporter writes ~10 (`llm_config` decoding params, `chat_with_docs`, `context_variables`, `structured_output`, `memory_enabled`, etc. are dropped). Low impact for n8n→Orchestrate (n8n lacks most), higher for Copilot→Orchestrate. Decide explicitly which of the 25 extra fields are in-scope.
6. **n8n Class B (pure automation) workflows** have no Orchestrate home — must become an opaque tool or be left running in n8n. Architecture decision, not a translation (see n8n corridor notes).
7. **Instruction translation is lossy and unverifiable without behavioral testing** — a dialog-tree → prose conversion can quietly change behavior. Only Phase D catches it.
8. **Credentials and files are irreducibly human** — no amount of AI removes these steps; the tool's job is to ask well (batched, deduped) and never fake them.

Honest bottom line on "how much works": the **deterministic clean paths**
(MCP tools, collaborator graph, generative-agent instructions verbatim,
guidelines, model exact-match) are high-confidence and mostly built. The
**AI-assisted paths** (dialog-tree translation, feature-folding, semantic
catalog match, low-confidence model pick) work but are lossy and *must* be
human-reviewed — they're accelerators, not autopilot. The **human-only paths**
(creds, files, model-enablement, standing up servers) are unavoidable; success
is measured by how little friction they carry, not by eliminating them.

---

## 8. Agent architecture — the Pi + Orchestrate hybrid (hackathon)

**Verified feasible**: Orchestrate has `AgentKind.EXTERNAL` with A2A
(`external_chat/A2A/0.2.1` and `/0.3.0`) and `orchestrate agents discover`
to register an A2A agent from a well-known URI. So Pi *can* be imported into
Orchestrate as an external agent — the hybrid you want is real, not a stretch.

### How many "agents" (AI roles) the migration actually needs
Five reasoning roles, which can collapse into fewer runtime agents:
1. **Instruction/Behavior Translator** (exists: `translate.py`)
2. **Feature-Folder** (§4)
3. **Model Recommender** (§B1 step 4)
4. **Tool-Catalog Matcher + OpenAPI/Python Synthesizer** (§B2)
5. **Validator/Repair** (§D)

Everything else — parsing, mapping MCP, dependency sort, deploy execution,
credential piping — is **deterministic Python, not agents**. Keep it that way;
agents where judgment is needed, code where it isn't.

### Recommended hybrid topology (leverages Orchestrate meaningfully)
```
        ┌──────────────────────────────────────────────┐
        │  watsonx Orchestrate (the visible surface)    │
        │                                               │
        │   [Migration Orchestrator]  (native agent)    │
        │      │  chat UI = the human-in-the-loop        │
        │      │  (creds prompts, model picks, file      │
        │      │   paths all happen as a conversation)   │
        │      ├── toolkit: "Wheatear Engine" (MCP)  ────┼──► deterministic
        │      │     parse / map / plan / deploy         │    Python engine
        │      │                                         │    (this repo) run
        │      │                                         │    as an MCP server
        │      └── collaborator: "Migration Copilot" ────┼──► Pi / Claude,
        │            (EXTERNAL / A2A agent)               │    registered via
        │            all 5 reasoning roles                │    `agents discover`
        └──────────────────────────────────────────────┘
```

- **Migration Orchestrator** (Orchestrate native): the entry point. Its
  *instructions* encode the phase sequence (§2→§6). Its *guidelines* encode the
  routing ("if a tool has no catalog match and is REST → call the synthesizer").
  This is itself an agent-migration-shaped agent, which is a nice demo.
- **Wheatear Engine** (Orchestrate toolkit, `type: mcp`): wrap the deterministic
  engine (parse export, build IR, resolve MCP, plan deploy, execute deploy,
  poll-to-ready) as MCP tools. This is where Orchestrate calls *your* code. The
  deterministic core stays testable and outside any LLM.
- **Migration Copilot** (Orchestrate external A2A agent = Pi): the five
  reasoning roles run here, as Pi subagents behind one A2A endpoint. Orchestrate
  delegates the judgment calls to it; it returns structured decisions the
  Orchestrator applies via the Wheatear Engine toolkit.

Why this scores well for an IBM hackathon: the *product* (a migration) is
itself an Orchestrate multi-agent workflow — native supervisor + MCP toolkit +
external A2A collaborator + (dogfooding) knowledge bases of migration docs. It
uses Orchestrate as the runtime, not just the target, while Pi does the heavy
reasoning as a first-class external agent. And it demonstrates the exact feature
your product migrates *into* (multi-agent + tools + external agents), which is a
strong narrative.

### If you want it simpler for a first cut
Collapse to: 1 Orchestrate native supervisor + 1 Pi external agent doing all
reasoning + the Wheatear MCP toolkit. Split the Pi reasoning into 5 internal
subagents only if a single agent's context gets unwieldy. Don't create 5
separate Orchestrate agents for the 5 roles — that's more moving parts than the
demo needs.

---

## 9. Suggested build order

1. IR field additions (§1) + tests — small, unblocks everything.
2. n8n importer (Phase A) — the missing corridor half; Copilot importer exists.
3. Catalog-match + credential loops (§B2) against your catalog JSON — highest-leverage, most-reused.
4. DeployPlanner + poll-to-ready + name→UUID resolution (§5) — the loophole; without it nothing actually lands on the target.
5. Model resolution loop wiring (§B1) — matrix is built, just wire the interactive fallback.
6. Feature-Folder + Validator/Repair (§4, §D) — quality layer.
7. Hackathon harness (§8) — wrap it once the pipeline works end-to-end headless.

Do 1–4 before touching the harness: the agent architecture is a shell around a
pipeline that has to work headless first.
