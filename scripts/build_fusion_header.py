"""
Fusion Model Header — compact, consistent runtime model notation.

Emits a one-line header for every response showing:
  WHO  — model emoji (🧠🤖🌐🔧🎛)
  WHAT — thinking tier symbol (★⌂⚡✓✎≡☰)
  MODE — scenario label (Strategy, Personal, etc.)

## Thinking Tiers — what happened inside the black box

Each tier symbol describes the KIND of processing the model did,
not just which model ran. This is the "open the black box" layer.

    ★ Strategic  — high-judgment reasoning, decisions, direction
    ⌂ Routine   — well-understood path, mechanical execution
    ⚡ Fast      — minimum-viable thinking, cheap throughput
    ✓ Audit     — verification, validation, quality checking
    ✎ Creative  — writing, narrative, composition, synthesis
    ≡ Blend     — MoA aggregation, multi-source synthesis
    ☰ Collect   — data gathering, search, retrieval, no judgment

The tier is derived from the Fusion plan phase data
(judgment_to_retain, tools, phase count) or auto-inferred
for direct responses.

## Header Format

    🧠 ★ Codex (gpt-5.6-sol) · Strategy
    🔧 ⌂ DeepSeek (deepseek-v4-flash) · Ops
    🔧 ⚡ DeepSeek (deepseek-v4-flash) · Audit
    🔧 ☰ DeepSeek (deepseek-v4-flash) · Research
    🤖 ✎ Claude (claude-opus-4-8) · Writing
    🧠 ✓ Codex (gpt-5.6-sol) · Audit
    🧠 ≡ Codex (gpt-5.6-sol) · Personal
    🌐 ★ Grok (grok-4.5) · Uncensored

Pipeline:

    🧠 ★ Codex > 🔧 ⌂ DeepSeek · Strategy

MoA blend:

    (🌐 ★ Grok + 🤖 ✎ Claude) > 🧠 ≡ Codex · Personal

Fallback:

    🧠 ★ Codex (gpt-5.6-sol) ◇ Ops [🎛 gpt-5.6-sol-low]
"""

import re

# ── Thinking Tier Symbols ────────────────────────────────────────────
# These describe what KIND of processing, not which model.
# Geometrically distinct — cannot be confused with model emojis or each other.

TIER_STRATEGIC  = "★"    # High-judgment reasoning, decisions
TIER_ROUTINE    = "⌂"    # Well-understood mechanical execution
TIER_FAST       = "⚡"   # Minimum-viable thinking, cheap
TIER_AUDIT      = "✓"    # Verification, validation
TIER_CREATIVE   = "✎"    # Writing, narrative, composition
TIER_BLEND      = "≡"    # MoA synthesis, multi-source
TIER_COLLECT    = "☰"    # Data gathering, retrieval, no judgment
TIER_UNKNOWN    = "?"     # Cannot determine

def infer_tier(plan: dict | None = None) -> str:
    """Auto-infer the thinking tier from plan data.

    Rules:
      - MoA blend → ≡ Blend (always synthesis)
      - judgment_to_retain with strategic keywords → ★ Strategic
      - Only collection/search tools → ☰ Collect
      - Audit/verification task_type → ✓ Audit
      - Writing/creative task_type → ✎ Creative
      - Cheap/fast model + simple phase → ⚡ Fast
      - Default for DeepSeek with operational task → ⌂ Routine
      - Multi-phase with diverse models → ★ Strategic (lead retained judgment)
    """
    if not plan:
        return TIER_UNKNOWN

    # MoA blend always means synthesis
    moa = plan.get("moa_blend")
    if moa and moa.get("layers"):
        return TIER_BLEND

    task_type = (plan.get("task_type") or "").lower()

    # task_type-based tiers
    task_tiers = {
        "audit": TIER_AUDIT,
        "writing": TIER_CREATIVE,
        "document": TIER_CREATIVE,
        "monitoring": TIER_COLLECT,
        "collect": TIER_COLLECT,
        "research": TIER_COLLECT,
    }
    if task_type in task_tiers:
        return task_tiers[task_type]

    # Explicit and personal are strategic — they involve judgment about
    # tone, brand fit, emotional framing, audience sensitivity
    if task_type in ("explicit", "personal"):
        return TIER_STRATEGIC

    # judgment_to_retain implies strategic thinking
    judgments = plan.get("judgment_to_retain", [])
    if judgments:
        judgment_text = " ".join(str(j).lower() for j in judgments)
        strategic_keywords = ["decision", "recommend", "approve", "strategy",
                              "direction", "commit", "budget", "allocate",
                              "accept", "reject", "weight", "prioritize",
                              "compare", "evaluate", "assess"]
        if any(kw in judgment_text for kw in strategic_keywords):
            return TIER_STRATEGIC

    # Phases analysis
    phases = plan.get("phases", [])
    if not phases:
        lead_model = (plan.get("lead_model") or "").lower()
        # Cheap model → fast tier
        cheap_models = {"deepseek-v4-flash", "grok-4.1-fast", "grok-4-1-fast-reasoning"}
        if lead_model in cheap_models:
            return TIER_FAST
        return TIER_STRATEGIC if task_type in ("strategy", "personal", "explicit") else TIER_ROUTINE

    # Multi-phase → depends on what's being done
    if len(phases) >= 2:
        # Check phase tools to determine primary thinking mode
        all_tools = []
        for p in phases:
            tools = p.get("tools", [])
            if isinstance(tools, list):
                all_tools.extend(tools)

        tool_str = " ".join(all_tools).lower()
        if "search" in tool_str or "collect" in tool_str or "fetch" in tool_str:
            if len(all_tools) > len(phases):  # more tools than phases = data-heavy
                return TIER_COLLECT
        if "review" in tool_str or "check" in tool_str or "verify" in tool_str:
            return TIER_AUDIT

        # Default for multi-phase → strategic (lead retained judgment)
        return TIER_STRATEGIC

    # Single phase
    lead_model = (plan.get("lead_model") or "").lower()
    cheap_models = {"deepseek-v4-flash", "grok-4.1-fast", "grok-4-1-fast-reasoning"}
    if lead_model in cheap_models:
        return TIER_FAST

    return TIER_ROUTINE


# ── Model emoji map ──────────────────────────────────────────────────

MODEL_EMOJI = {
    "gpt-5.6-sol": "🧠",
    "gpt-5.6-luna": "🧠",
    "gpt-5.6-terra": "🧠",
    "gpt-5.5": "🧠",
    "gpt-5.5-pro": "🧠",
    "gpt-5.4": "🧠",
    "pools/gpt-5.6-sol-low": "🧠",
    "pools/gpt-5.6-sol-fast": "🧠",
    "pools/gpt-5.6-luna-low": "🧠",
    "pools/gpt-5.6-terra-low": "🧠",
    "gpt-mix/gpt-5.5": "🧠",
    "gpt-5.4-mini": "🧠",
}

MODEL_EMOJI.update({
    "claude-opus-4-8": "🤖",
    "claude-opus-4-7": "🤖",
    "claude-opus-4-6": "🤖",
    "claude-sonnet-4-6": "🤖",
    "claude-haiku-4-5": "🤖",
    "claude-max/claude-opus-4-8": "🤖",
    "claude-max/claude-sonnet-4-6": "🤖",
    "claude-max/claude-haiku-4-5": "🤖",
})

MODEL_EMOJI.update({
    "grok-4.5": "🌐",
    "grok-4-20-reasoning": "🌐",
    "grok-4.3": "🌐",
    "grok-4.1-fast": "⚡",
    "grok-4-1-fast-reasoning": "⚡",
    "grok-4-1-fast-non-reasoning": "⚡",
    "xai/grok-4.5": "🌐",
})

MODEL_EMOJI.update({
    "deepseek-v4-flash": "🔧",
    "deepseek-v4-pro": "🔧",
    "ds-sp/deepseek-v4-flash": "🔧",
    "ds-sp/deepseek-v4-pro": "🔧",
})

MODEL_EMOJI.update({
    "gemini-3.1-pro-preview": "💎",
    "gemini-3.1-flash-lite-preview": "💎",
    "seed-2-0-pro-260328": "🌱",
    "glm-4-7-251222": "🏮",
    "glm-z1-air": "🏮",
})

PROVIDER_EMOJI = {
    "openai-codex": "🧠",
    "claude-team": "🤖",
    "anthropic": "🤖",
    "xai-oauth": "🌐",
    "xai": "🌐",
    "deepseek": "🔧",
    "xrtoken-cheap": "🎛",
    "xrtoken-claude-max": "🎛",
    "xrtoken-sonnet": "🎛",
    "xrtoken-grok": "🎛",
    "xrtoken-deepseek": "🎛",
    "zai": "🏮",
    "custom:hermes-gpt-mcp": "🔗",
}

PROVIDER_NAME = {
    "openai-codex": "Codex",
    "claude-team": "Claude",
    "anthropic": "Claude",
    "xai-oauth": "Grok",
    "xai": "Grok",
    "deepseek": "DeepSeek",
    "xrtoken-cheap": "XRToken",
    "xrtoken-claude-max": "XRToken",
    "xrtoken-sonnet": "XRToken",
    "xrtoken-grok": "XRToken",
    "xrtoken-deepseek": "XRToken",
    "zai": "Z.AI",
    "custom:hermes-gpt-mcp": "Adapter",
}

# Scenario labels
SCENARIO_MAP = {
    "strategy": "Strategy",
    "personal": "Personal",
    "explicit": "Uncensored",
    "research": "Research",
    "document": "Doc",
    "operations": "Ops",
    "monitoring": "Monitor",
    "analysis": "Analysis",
    "planning": "Plan",
    "writing": "Writing",
    "audit": "Audit",
    "debate": "Debate",
}


def _model_short(model: str) -> str:
    return model.rsplit("/", 1)[-1]


def _emoji_for(model: str, provider: str = "") -> str:
    model_l = model.lower().strip()
    if model_l in MODEL_EMOJI:
        return MODEL_EMOJI[model_l]
    provider_l = provider.lower().strip()
    if provider_l in PROVIDER_EMOJI:
        return PROVIDER_EMOJI[provider_l]
    return "⚙️"


def _name_for(provider: str) -> str:
    p = provider.lower().strip()
    return PROVIDER_NAME.get(p, p)


def _scenario_label(plan: dict | None) -> str:
    task_type = (plan or {}).get("task_type", "")
    return SCENARIO_MAP.get(task_type.lower(), "")


def _model_label(model: str, provider: str = "", tier: str = "") -> str:
    """Single-model segment: EMOJI TIER Name (model)

    Falls back to EMOJI TIER (model) when provider is unknown.
    """
    emoji = _emoji_for(model, provider)
    name = _name_for(provider)
    short = _model_short(model)
    if tier and name:
        return f"{emoji} {tier} {name} ({short})"
    if tier:
        return f"{emoji} {tier} ({short})"
    if name:
        return f"{emoji} {name} ({short})"
    return f"{emoji} ({short})"


def build_fusion_header(
    *,
    plan: dict | None = None,
    phase_results: list | None = None,
    moa_blend: dict | None = None,
    fallback_occurred: bool = False,
    fallback_model: str | None = None,
    scenario: str | None = None,
    tier: str | None = None,
) -> str:
    """Build a compact model header from Fusion v3 execution data.

    Args:
        plan: Fusion v3 plan dict (phases, moa_blend, task_type, judgment_to_retain).
        phase_results: List of (output, ok, provider, model, fallback) tuples.
        moa_blend: MoA blend dict if used.
        fallback_occurred: True if any phase used a fallback.
        fallback_model: Model used as fallback.
        scenario: Override scenario label. Auto-derived from plan.task_type.
        tier: Override thinking tier. Auto-inferred from plan data.

    Returns:
        One-line compact header, or empty string if no data.
    """
    if not plan and not phase_results and not moa_blend:
        return ""

    # Derive scenario and tier
    if scenario is None:
        scenario = _scenario_label(plan)
    if tier is None:
        tier = infer_tier(plan)

    phases = (plan or {}).get("phases", [])
    results = phase_results or []
    moa = moa_blend or (plan or {}).get("moa_blend")

    if moa and moa.get("layers"):
        return _build_moa_header(moa, fallback_occurred, fallback_model, scenario, tier)

    if results:
        return _build_pipeline_header(phases, results, fallback_occurred, fallback_model, scenario, tier)

    # Single model — use the lead model with tier
    lead_model = (plan or {}).get("lead_model", "")
    lead_provider = (plan or {}).get("lead_provider", "")
    if lead_model:
        label = _model_label(lead_model, lead_provider, tier)
        if scenario:
            label += f" ◇ {scenario}"
        if fallback_occurred and fallback_model:
            label += f" [🎛 {_model_short(fallback_model)}]"
        elif fallback_occurred:
            label += " [fallback]"
        return label

    return ""


def _build_pipeline_header(
    phases: list, results: list,
    fallback_occurred: bool, fallback_model: str | None,
    scenario: str = "", tier: str = "",
) -> str:
    """Build compact pipeline notation from phased execution results."""
    seen = []
    for i, (phase, result) in enumerate(zip(phases, results)):
        if result is None or len(result) < 4:
            continue
        _, ok, prov, mod, fb = result
        if not ok or not mod:
            continue
        key = (mod, prov, fb if isinstance(fb, bool) and fb else False)
        if not seen or seen[-1][:2] != (mod, prov):
            seen.append(key)

    if not seen:
        return ""

    # Single deduped entry — show tier on the model label
    if len(seen) == 1:
        mod, prov, fb = seen[0]
        label = _model_label(mod, prov, tier)
        if scenario:
            label += f" ◇ {scenario}"
        if fb or fallback_occurred:
            label += " [fallback]"
        return label

    # Pipeline — tier annotates the whole chain as group label on its own line
    parts = []
    for mod, prov, fb in seen:
        label = _model_label(mod, prov)
        if fb:
            label += " [fb]"
        parts.append(label)

    pipeline = " > ".join(parts)
    qualifiers = []
    if tier:
        qualifiers.append(tier)
    if scenario:
        qualifiers.append(f"◇ {scenario}")
    if qualifiers:
        pipeline += " " + " ".join(qualifiers)
    if fallback_occurred:
        pipeline += " [fallback]"
    return pipeline


def _build_moa_header(
    moa: dict,
    fallback_occurred: bool,
    fallback_model: str | None,
    scenario: str = "", tier: str = "",
) -> str:
    """Build MoA blend header with tier annotation."""
    layers = moa.get("layers", [])
    synthesis = moa.get("synthesis", {})
    agg_model = synthesis.get("model", "")
    agg_provider = synthesis.get("provider", "")

    layer_models = set()
    for layer in layers:
        for key in ("model_1", "model_2"):
            m = layer.get(key)
            if m:
                p_key = key.replace("model", "provider")
                p = layer.get(p_key, "")
                layer_models.add((m, p))

    if not layer_models and not agg_model:
        return ""

    layer_parts = []
    for m, p in sorted(layer_models):
        layer_parts.append(_model_label(m, p))

    if agg_model:
        agg_label = _model_label(agg_model, agg_provider, tier or TIER_BLEND)
        if len(layer_parts) == 1:
            header = f"{layer_parts[0]} > {agg_label}"
        else:
            parallel = " + ".join(layer_parts)
            header = f"({parallel}) > {agg_label}"
    else:
        header = " + ".join(layer_parts)

    if scenario:
        header += f" ◇ {scenario}"
    if fallback_occurred:
        header += " [fallback]"
    return header


# ── Header detection (for dedup) ─────────────────────────────────────

_KNOWN_HEADER_PATTERNS = [
    re.compile(r"^[🧠🤖🌐🔧🎛💎⚡⚙️🏮🔗]\s+[★⌂⚡✓✎≡☰?]\s+.+$"),
    re.compile(r"^\([🧠🤖🌐🔧🎛💎⚡⚙️🏮🔗].+\)\s*>\s*[🧠🤖🌐🔧🎛💎⚡⚙️🏮🔗].+$"),
    re.compile(r"^[🧠🤖🌐🔧🎛💎⚡⚙️🏮🔗]\s+.+\s*>\s*[🧠🤖🌐🔧🎛💎⚡⚙️🏮🔗].+$"),
]


def response_has_model_header(text: str) -> bool:
    """Check if a response already starts with a known model header."""
    if not text:
        return False
    first_line = text.lstrip().splitlines()[0].strip()
    for pattern in _KNOWN_HEADER_PATTERNS:
        if pattern.match(first_line):
            return True
    return False


def ensure_header(response: str, header: str) -> str:
    """Prepend a model header to a response, deduplicating if already present."""
    if not header:
        return response
    if not response:
        return header
    if response_has_model_header(response):
        return response
    return f"{header}\n\n{response}"
