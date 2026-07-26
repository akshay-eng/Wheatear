# Model matrix engine — how it works, what's in it, worked examples

New standalone package: `engine/wheatear/model_matrix/`. Separate from the
existing `wheatear/model_map.py` (which stays as the static, zero-dependency
fallback) as requested. Built from a live multi-agent research pass across
Claude, OpenAI, Gemini, DeepSeek, Qwen, gpt-oss, Kimi, Gemma, and Nemotron —
**71 researched model profiles**, not guessed.

## How to use it

```python
from wheatear.model_matrix import recommend
from wheatear.model_matrix.target_sources import OrchestrateModelSource

# OrchestrateModelSource shells out to the `orchestrate` ADK CLI (models list
# --raw) -- activate an env first: orchestrate env activate <name> --api-key ...
recommendation = recommend("claude-opus-4-1", OrchestrateModelSource())

print(recommendation.best.raw_id)       # the top pick
print(recommendation.best.rationale)    # why
for candidate in recommendation.ranked_candidates:
    print(candidate.score, candidate.raw_id, candidate.rationale)
```

`recommend()` never picks silently and stops there — it returns every
candidate ranked, not just the winner, specifically so a human reviewing a
migration can see the runner-ups and why they lost. `Recommendation.
review_required` is always `True` — matches the same philosophy already in
`wheatear/ir/schema.py` (`ConnectionRef.review_required`, `model_family`
comment: "a human should confirm it").

## Package layout

| File | Job |
|---|---|
| `types.py` | `ModelProfile`, `ScoredCandidate`, `Recommendation`, `CostTier` |
| `profiles.py` | The 71 curated profiles — plain data, extend freely |
| `resolver.py` | raw id/hint string → `ModelProfile` (exact match → substring match → tier-only fallback heuristic) |
| `scorer.py` | Weighted capability-similarity ranking + cost-tier downgrade penalty |
| `target_sources/base.py` | `TargetModelSource` — a `Protocol`, not tied to Orchestrate, so OpenAI/Vertex AI/Bedrock AgentCore targets (all named in PRODUCT.md) each get their own implementation later without touching anything else |
| `target_sources/orchestrate.py` | Shells out to the `orchestrate` ADK CLI (`models list --raw`), same convention `deployer.py` already uses for imports |

## How matching actually works

1. **Resolve the source model.** `resolve("claude-opus-4-1")` → exact `known_ids` match → `ModelProfile` at confidence 1.0. An unrecognized string still resolves — to a bare cost-tier guess via substring heuristics (`mini`→small, `flash`→mid, etc.), confidence 0.3, defaulting to `frontier` tier so an unknown model is never silently downgraded (mirrors `model_map.py`'s existing `DEFAULT_TIER` philosophy exactly).
2. **Resolve every live target model** the same way.
3. **Score each candidate** against the source across four weighted dimensions — agentic tool-use (40%, weighted highest because this whole engine is for *agent* migrations), coding (25%), reasoning/math (25%), general instruction-following (10%) — then apply a **downgrade penalty** (`-0.6` per cost-tier step below the source; stepping *up* is never penalized, since an over-qualified target is a cost concern for the human, not a capability-fit failure).
4. **Rank and return everything**, including candidates with no resolvable profile at all (never silently dropped).

## Worked example — your exact scenario

Source: `claude-opus-4-1`. Target platform reports: `groq/openai/gpt-oss-120b`, `gemini-3.1-pro-preview`, `Qwen/Qwen2.5-7B-Instruct`, `gemini-3.6-flash`.

```
0.60  gemini-3.1-pro-preview     -> Gemini 3.1 Pro         (frontier, matches source tier)
0.07  groq/openai/gpt-oss-120b   -> gpt-oss-120b            (mid tier, -0.6 downgrade penalty)
0.00  Qwen/Qwen2.5-7B-Instruct   -> Qwen 2.5 7B Instruct    (small tier, -1.2 downgrade penalty)
0.00  gemini-3.6-flash           -> Gemini 3.6 Flash        (mid tier, -0.6 downgrade penalty)
```

Gemini 3.1 Pro wins clearly — same frontier tier as Claude Opus 4.1, strong capability similarity across all three weighted dimensions. The other three are correctly pushed down hard: they're capable models, but picking one for a frontier-tier source would be a silent downgrade, which is exactly the failure mode this engine is built to avoid. Every one of these is real code, run and verified — not a hypothetical.

## Key research findings worth knowing

- **The Claude/OpenAI/Gemini landscape has moved fast.** As of this research (2026-07-26), current flagships are Claude Opus 5 (Jul 24, 2026) / Fable 5, OpenAI's GPT-5.6 Sol/Terra/Luna (a naming-scheme break from "mini/nano"), and Gemini 3.6 Flash / 3.1 Pro (Gemini 3.5 Pro is announced but **not yet released** — do not target it). If your matrix engine's data goes stale, these are the first three families to re-check.
- **Provider-prefixed ids matter for matching.** The same underlying model shows up differently depending on access point — e.g. Gemini via the Generative Language API is `models/gemini-2.5-pro`, via Vertex AI it's `publishers/google/models/gemini-2.5-pro` (or bare `gemini-1.5-flash-001` on n8n's separate Vertex node) — same model, different wrapper. The resolver's substring matching handles this, but a future refinement could strip known wrapper prefixes explicitly.
- **Tool-calling reliability is not correlated with raw reasoning score.** Several findings surfaced this directly: DeepSeek R1/R1-0528 have well-documented structured-tool-call failures (tool calls emitted as text, not the `tool_calls` array — see the GitHub issues cited in `profiles.py`) despite frontier-level math scores. GPT-5 (original) scored lower on one third-party agentic leaderboard (59.2%) than reasoning-focused competitors. Gemma didn't get reliable native tool-calling until Gemma 4 (Apr 2026) — Gemma 3's tau2-bench score was ~6.6%, Gemma 4's is ~86.4%. This is exactly why `agentic_tool_use_strength` is scored and weighted separately from `reasoning_math_strength` rather than assumed to track it.
- **NVIDIA's Nemotron 3 family (Nano/Super/Ultra, Dec 2025-Jun 2026) is explicitly agent-first** — tau2-bench and BFCL scores published directly in NVIDIA's own release material, a genuinely strong open-weight option for agentic migrations specifically.
