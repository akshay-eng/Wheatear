# wheatear

Migrate AI agents and workflows between orchestration platforms. First corridor:
Microsoft Copilot Studio → IBM watsonx Orchestrate.

## Install (development)

```bash
cd engine
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,anthropic,google]"
```

LLM providers are optional extras — install whichever you'll actually use
(`anthropic`, `google`; `openai` and `watsonx` extras exist but have no adapter yet).

## Usage

### Interactive (recommended for humans)

```bash
wheatear
```

Asks for the export directory, the output path, and the LLM provider/API key, then
runs the full pipeline with live progress. LLM provider choice is remembered in
`~/.config/wheatear/config.json` between runs — the API key itself is never written
to disk, only the name of the environment variable that holds it.

### Scripted / CI

```bash
# 1. Export the source agent yourself (Wheatear doesn't shell out to `pac`):
pac copilot clone --bot <bot-id> --output-dir ./my-agent-clone

# 2. Sanity-check the export (recognizes both a `pac copilot clone` workspace
#    and a Dataverse solution export)
wheatear extract ./my-agent-clone

# 3. Run the full pipeline (needs an LLM key for the Translate stage)
export ANTHROPIC_API_KEY=sk-...   # or GEMINI_API_KEY with --llm-provider google
wheatear migrate --from copilot-studio --to orchestrate ./my-agent-clone ./out

# 4. Review ./out/review-manifest.yaml (if present) before importing
orchestrate agents import -f ./out/agent.yaml
```

## Status

Early stage, but the Copilot Studio side has been calibrated against one real
generative-agent export (a Dataverse solution export), not just documentation and
synthetic fixtures. Both real-world export shapes are handled:

- `pac copilot clone` workspaces (`.mcs.yml` dialog-tree topics)
- Dataverse solution exports (`solution.xml` + `botcomponents/`), including
  generative/GPT-orchestrated agents whose real behavior lives in a system prompt
  rather than a topic tree

Still expect rough edges on export shapes not yet seen in the wild.

## Tests

```bash
pytest
```
