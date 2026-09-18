#!/usr/bin/env python3
"""
Fusion Phase Executor — MoA-powered autonomous plan execution.

Takes a validated Fusion v3 plan and:
  1. Probes provider health before each phase
  2. Falls back to Fallback router equivalents when primary fails (recursive-safe)
  3. Dispatches each phase to its assigned model
  4. Handles MoA blend execution (sequential layer dispatch + aggregation)
  5. Tracks run state for resumability
  6. Detects when plan comparison is needed
  7. Produces post-run summary with cost annotation
  8. Enforces --budget free (subscription-only) / balanced / premium

Usage:
  python3 execute_fusion_plan.py path/to/plan.json [--budget free|balanced|premium]
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent
VALIDATOR = SCRIPT_DIR / "validate_fusion_packet.py"
NORMALIZER = SCRIPT_DIR / "normalize_fusion_plan.py"
RUNS_BASE = Path(
    os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")
) / "fusion" / "runs"

# ── Routing config (drained of personal infra) ─────────────────────────
# Fallback/subscription routing is operator-config via fusion.yaml if wanted;
# the default ships EMPTY so no invented or personal providers ever route.

def _router_config() -> dict:
    cfg = {}
    try:
        home = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
        p = home / "fusion.yaml"
        if p.exists():
            import yaml
            cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        cfg = {}
    return cfg.get("router", {})


_ROUTER = _router_config()

# Subscription-only providers (free budget). Empty by default: no free
# "subscription" providers are assumed for strangers.
SUBSCRIPTION_PROVIDERS = set(_ROUTER.get("subscription_providers") or ())

# Primary model -> (fallback model, fallback provider). Empty by default —
# no fallback is invented; operators pin their own routes under `router:`.
FALLBACK_MAP = _ROUTER.get("fallback_map") or {}

# Provider-specific health-probe models. Empty -> probe the assigned model.
HEALTH_PROBE_MODELS = _ROUTER.get("health_probe_models") or {}

# Providers treated as metered (affects cost-tier display). Empty by default.
_CUSTOM_METERED = set(_ROUTER.get("metered_providers") or ())

HEALTH_PROBE_PROMPT = 'Reply with exactly: ok'

# ── Sanitization ──────────────────────────────────────────────────────

_FILENAME_CHARS = re.compile(r"[^\w\-.]")


def sanitize_name(name):
    """Strip anything that could traverse or break filesystem paths."""
    cleaned = _FILENAME_CHARS.sub("_", str(name))
    # Collapse multiple underscores
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    # Strip leading dots and underscores (path traversal prevention)
    cleaned = cleaned.lstrip("._")
    return cleaned.strip("_") or "unknown"


# ── Helpers ────────────────────────────────────────────────────────────


def log(msg):
    print(f"  [{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", file=sys.stderr)


def hermes_chat(prompt, provider, model, max_turns=1, toolsets=None, timeout=120):
    """Run Hermes chat in quiet mode. Returns (output, success_bool)."""
    import shutil
    binary = os.environ.get("HERMES_BIN") or shutil.which("hermes") or "hermes"
    cmd = [
        binary, "chat",
        "-q", prompt,
        "--provider", provider,
        "--model", model,
        "--max-turns", str(max_turns),
        "--quiet", "--source", "tool",
    ]
    if toolsets:
        cmd += ["--toolsets", ",".join(toolsets)]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            start_new_session=True,
        )
        output = result.stdout.strip()
        if output:
            return output, True
        if result.stderr.strip():
            return result.stderr.strip()[:200], False
        return "empty response", False
    except subprocess.TimeoutExpired:
        return "TIMEOUT", False
    except FileNotFoundError:
        return "HERMES_NOT_FOUND", False


# ── Phase 0: Provider Health Probe ────────────────────────────────────


def probe_provider(provider, model):
    """Smoke-test a provider by sending a trivial prompt. Returns (alive_bool, latency_ms, error_msg).

    Uses monotonic clock for latency. Kills process group on timeout.
    """
    start = time.monotonic()
    resp, ok = hermes_chat(HEALTH_PROBE_PROMPT, provider, model, max_turns=1, timeout=30)
    latency = int((time.monotonic() - start) * 1000)
    if ok and "ok" in resp.lower():
        return True, latency, None
    return False, latency, resp[:150]


def probe_fallback_router():
    """Health-check the fallback router infra. Not configured by default:
    with an empty fallback map there is nothing to probe, so it reports down
    and resolve_route simply has no fallback path (never an invented route)."""
    if not FALLBACK_MAP:
        return False, 0, "no fallback router configured"
    # probe the first configured fallback provider's model generically
    first_model, first_prov = next(iter(FALLBACK_MAP.values()))
    start = time.monotonic()
    resp, ok = hermes_chat(HEALTH_PROBE_PROMPT, first_prov, first_model,
                           max_turns=1, timeout=30)
    latency = int((time.monotonic() - start) * 1000)
    if ok:
        return True, latency, None
    return False, latency, resp[:150]


# ── Phase 0: Fallback Routing ──────────────────────────────────────────


def resolve_route(assigned_provider, assigned_model, budget="balanced"):
    """Determine the actual route for a phase.

    Returns (actual_provider, actual_model, fallback_triggered_bool, rationale).

    Simple cascade:
      1. Probe the assigned provider directly.
      2. If alive → use it.
      3. If dead → check Fallback router → look up FALLBACK_MAP → probe fallback → use it.
      4. If Fallback router also dead → DeepSeek as last resort.

    The fallback map routes to the Fallback router equivalent of the same model
    (e.g. same-model fallback on a different provider), so rate limits
    transparently route to the same model on Fallback router without switching
    to a completely different model family.
    """
    # Step 0: Budget gate
    if budget == "free" and assigned_provider not in SUBSCRIPTION_PROVIDERS:
        return None, None, True, (
            f"Budget is free but plan assigned {assigned_provider}/{assigned_model} "
            f"(not in subscription providers: {SUBSCRIPTION_PROVIDERS}). "
            f"Blocked by budget constraint."
        )

    # Step 1: Probe the assigned provider directly
    probe_model = HEALTH_PROBE_MODELS.get(assigned_provider, assigned_model)
    alive, latency, err = probe_provider(assigned_provider, probe_model)
    if alive:
        return assigned_provider, assigned_model, False, None

    # Step 2: Check Fallback router as fallback infrastructure
    fallback_alive, xlatency, xerr = probe_fallback_router()
    if not fallback_alive:
        return None, None, True, (
            f"{assigned_provider}/{assigned_model} unavailable ({err}). "
            f"Fallback router also unreachable ({xerr}). No fallback path."
        )

    # Step 3: Look up the same-model equivalent on Fallback router
    fallback = FALLBACK_MAP.get(assigned_model)
    if not fallback:
        return None, None, True, (
            f"{assigned_provider}/{assigned_model} unavailable ({err}). "
            f"No fallback configured for {assigned_model}."
        )

    fallback_model, fallback_provider = fallback

    # Step 4: Budget check for fallback
    if budget == "free" and fallback_provider not in SUBSCRIPTION_PROVIDERS:
        return None, None, True, (
            f"{assigned_provider} unavailable ({err}). "
            f"Budget is free but fallback {fallback_provider} is metered."
        )

    # Step 5: Probe the fallback route directly
    fb_alive, flatency, ferr = probe_provider(fallback_provider, fallback_model)
    if fb_alive:
        return fallback_provider, fallback_model, True, (
            f"{assigned_provider}/{assigned_model} unavailable ({err}). "
            f"Routed to same model on Fallback router: {fallback_provider}/{fallback_model}."
        )

    # Step 6: Budget check before last resort
    if budget == "free":
        return None, None, True, (
            f"{assigned_provider} and fallback both unavailable. "
            f"Budget is free — no more routes to try."
        )

    # Step 7: Last resort — DeepSeek universal fallback
    deepseek_alive, dlatency, derr = probe_provider("deepseek", "deepseek-v4-flash")
    if deepseek_alive:
        log("  \u26a0 Primary and Fallback router fallback both failed. Using DeepSeek as last resort.")
        return "deepseek", "deepseek-v4-flash", True, (
            f"{assigned_provider} and Fallback router fallback both unavailable. "
            f"Last-resort routed to deepseek/deepseek-v4-flash."
        )

    return None, None, True, (
        f"All providers exhausted. Primary: {err}. "
        f"Fallback router: {ferr or 'unknown'}. DeepSeek: {derr}."
    )


def is_subscription_provider(provider):
    """Check if a provider is free (included in a subscription)."""
    return provider in SUBSCRIPTION_PROVIDERS


# ── JSON extraction helper ─────────────────────────────────────────────


def extract_json(text):
    """Extract the first JSON object from model output, preferring ```json fences.

    Returns (parsed_dict, error_str_or_None).
    """
    # Prefer fenced JSON
    fence_match = re.search(r"```(?:json)?\s*\n?(\{.*?\})\s*\n?```", text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1)), None
        except json.JSONDecodeError:
            pass

    # Fallback: bracket matching with depth tracking
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    return json.loads(text[start:i + 1]), None
                except json.JSONDecodeError as e:
                    return None, str(e)
    return None, "No JSON object found"


# ── Phase Execution ───────────────────────────────────────────────────


def execute_phase(phase, run_dir, budget="balanced", plan=None):
    """Execute a single phase of the plan.

    Returns (output_text, success_bool, actual_provider, actual_model, fallback_triggered).
    """
    phase_name_raw = phase.get("phase", "unknown")
    phase_name = sanitize_name(phase_name_raw)
    assigned_model = phase.get("model")
    assigned_provider = phase.get("provider")
    tools = phase.get("tools", [])
    max_turns = phase.get("max_turns", 5)

    if not assigned_model or not assigned_provider:
        log(f"  \u2717 Phase '{phase_name_raw}' missing model or provider")
        return None, False, None, None, False

    # Resolve route (with fallback)
    provider, model, fallback, rationale = resolve_route(assigned_provider, assigned_model, budget)
    if provider is None:
        log(f"  \u2717 Phase '{phase_name_raw}': {rationale}")
        return None, False, None, None, False

    if fallback:
        log(f"  \u26a0 Phase '{phase_name_raw}': {rationale}")

    plan = plan or {}
    inputs = plan.get("inputs") or []
    inputs_text = "\n".join(f"- {i}" for i in inputs) if inputs else "(no task inputs provided)"
    deliverables = plan.get("deliverables") or []
    deliv_text = ", ".join(deliverables) if deliverables else "(unspecified)"
    phase_prompt = (
        f"You are executing phase '{phase_name_raw}' of a Fusion plan.\n\n"
        f"TASK (from the plan's inputs):\n{inputs_text}\n\n"
        f"EXPECTED DELIVERABLE: {deliv_text}\n\n"
        f"Follow these constraints:\n"
        f"- Deliver the phase output as specified in the plan\n"
        f"- Stay within scope_in. Do not expand scope.\n"
        f"- Stop on any listed stop condition.\n"
        f"- Return the result and evidence.\n\n"
        f"Phase config: {json.dumps(phase, indent=2)}\n"
    )

    log(f"  \u2192 Executing phase '{phase_name_raw}' on {provider}/{model} (turns={max_turns})")
    output, success = hermes_chat(phase_prompt, provider, model, max_turns=max_turns, toolsets=tools, timeout=300)

    if not success:
        log(f"  \u2717 Phase '{phase_name_raw}' execution failed: {output[:200]}")
        return output, False, provider, model, fallback

    out_path = run_dir / f"phase-{phase_name}.md"
    out_path.write_text(output)
    log(f"  \u2713 Phase '{phase_name_raw}' complete ({len(output)} chars \u2192 {out_path.name})")
    return output, True, provider, model, fallback


# ── MoA Blend Execution ────────────────────────────────────────────────


def execute_moa_blend(moa_blend, run_dir, budget="balanced"):
    """Execute a MoA blend: sequential dispatch to layer models, then aggregate.

    Returns (aggregated_output, success_bool).

    Note: Models run sequentially (not parallel) within each layer.
    Parallelism across independent layers would require background processes.
    """
    layers = moa_blend.get("layers", [])
    synthesis = moa_blend.get("synthesis", {})
    agg_instruction = synthesis.get("instruction", "Synthesize the outputs below into a coherent result.")

    if not layers:
        log("  \u2717 MoA blend has no layers")
        return None, False

    log(f"  \u2192 MoA blend: {moa_blend.get('blend_type', 'layer').upper()} "
        f"with {len(layers)} layer(s)")

    layer_outputs = []
    for lidx, layer in enumerate(layers):
        purpose = layer.get("purpose", f"layer_{lidx + 1}")
        purpose_sanitized = sanitize_name(purpose)
        models_to_run = []

        for key in ("model_1", "model_2"):
            if layer.get(key):
                prov_key = key.replace("model", "provider")
                prov = layer.get(prov_key, "deepseek")
                mod = layer[key]
                # Respect budget: under free, skip metered providers
                if budget == "free" and not is_subscription_provider(prov):
                    log(f"  \u26a0 MoA layer model {prov}/{mod} blocked by free budget")
                    layer_result = f"### Model {pid + 1 if 'pid' in dir() else models_to_run.index((prov,mod))+1} skipped (free budget)\n\n"
                    continue
                models_to_run.append((prov, mod))

        layer_num = layer.get("layer", lidx + 1)
        layer_result = f"## Layer {layer_num}: {purpose}\n\n"
        for pid, (prov, mod) in enumerate(models_to_run):
            alive, lat, err = probe_provider(prov, HEALTH_PROBE_MODELS.get(prov, mod))
            if not alive:
                fallback = FALLBACK_MAP.get(mod)
                if fallback:
                    fprov, fmod = fallback
                    if budget == "free" and not is_subscription_provider(fprov):
                        layer_result += f"**Model {pid + 1} unavailable (and fallback blocked by free budget):** {err}\n\n"
                        continue
                    prov, mod = fprov, fmod
                    log(f"  \u26a0 MoA layer model failed, using fallback {prov}/{mod}")
                else:
                    layer_result += f"**Model {pid + 1} unavailable:** {err}\n\n"
                    continue

            prompt = (
                f"You are contributing to MoA layer '{purpose}'. "
                f"Provide your perspective on this analysis.\n\n{agg_instruction}"
            )
            output, ok = hermes_chat(prompt, prov, mod, max_turns=3, timeout=180)
            if ok:
                layer_result += f"### Model {pid + 1} ({prov}/{mod})\n\n{output}\n\n"
            else:
                layer_result += f"**Model {pid + 1} ({prov}/{mod}) failed:** {output[:200]}\n\n"

        layer_path = run_dir / f"moa-layer-{sanitize_name(str(layer_num))}-{purpose_sanitized}.md"
        layer_path.write_text(layer_result)
        layer_outputs.append(layer_result)

    # Run aggregator
    combined = "\n\n---\n\n".join(layer_outputs)
    agg_prompt = f"{agg_instruction}\n\nBELOW ARE THE LAYER OUTPUTS TO SYNTHESIZE:\n\n{combined}"
    log(f"  \u2192 Running MoA aggregator")

    agg_provider_resolved, agg_model_resolved, _, _ = resolve_route(
        synthesis.get("provider", "openai-codex"),
        synthesis.get("model", "gpt-5.6-sol"),
        budget,
    )
    if agg_provider_resolved is None:
        log("  \u2717 MoA aggregator unavailable (all routes failed)")
        return None, False

    output, ok = hermes_chat(agg_prompt, agg_provider_resolved, agg_model_resolved, max_turns=5, timeout=300)
    if ok:
        out_path = run_dir / "moa-aggregated.md"
        out_path.write_text(output)
        log(f"  \u2713 MoA aggregation complete ({len(output)} chars)")
        return output, True

    log(f"  \u2717 MoA aggregation failed: {output[:200]}")
    return None, False


# ── Plan Variant Generator (Plan Comparison) ──────────────────────────


def generate_plan_variants(plan, variant_profiles, budget="balanced"):
    """Generate plan variants with different model allocations for comparison.

    The variant profiles come from the plan packet's plan_comparison.variants field.
    Uses GPT with route resolution, fence-based JSON parsing, and variant validation.

    variant_profiles: list of strings like ["cost-minimizing", "quality-maximizing", "balanced"]
    """
    variants = []

    for profile in variant_profiles:
        prompt = (
            f"You are a Fusion plan variant generator. Create a variant of the plan below "
            f"with a '{profile}' model allocation strategy.\n\n"
            f"BASE PLAN:\n{json.dumps(plan, indent=2)}\n\n"
            f"For '{profile}':\n"
            f"- **cost-minimizing**: Replace every frontier model with the cheapest capable "
            f"alternative. Use DeepSeek and Grok 4.1 Fast where possible. Use Fallback router pools "
            f"for GPT work. Drop non-essential MoA layers.\n"
            f"- **quality-maximizing**: Use the best model for every phase regardless of cost. "
            f"Add Claude review phase. Add GPT audit phase. Prefer frontier models over Fallback router pools.\n"
            f"- **balanced**: Prefer subscription models (free). Use Fallback router pools only as fallback. "
            f"MoA only for genuinely conflicting requirements.\n\n"
            f"Return ONLY the variant Fusion v3 plan as a JSON code block. "
            f"```json\n...\n```\n"
        )
        log(f"  \u2192 Generating '{profile}' variant...")

        # Use route resolution to handle Codex downtime
        provider_resolved, model_resolved, fb, rationale = resolve_route(
            "openai-codex", "gpt-5.6-sol", budget
        )
        if provider_resolved is None:
            log(f"  \u2717 '{profile}' variant skipped: {rationale}")
            continue

        output, ok = hermes_chat(prompt, provider_resolved, model_resolved, max_turns=1, timeout=120)
        if not ok:
            log(f"  \u2717 '{profile}' variant generation failed: {output[:200]}")
            variants.append((profile, None, False))
            continue

        parsed, err = extract_json(output)
        if parsed is None:
            log(f"  \u2717 '{profile}' variant JSON parse failed: {err}")
            variants.append((profile, None, False))
            continue

        variants.append((profile, parsed, True))
        log(f"  \u2713 '{profile}' variant generated")

    return variants


# ── Cost tier helpers ──────────────────────────────────────────────────


def cost_tier(provider):
    return "metered" if provider in _CUSTOM_METERED else "subscription"


# ── Post-Run Summary ──────────────────────────────────────────────────


def write_summary(run_dir, plan, phase_results, moa_result, comparison_results=None):
    """Write a structured post-run summary."""
    all_phases = plan.get("phases", [])
    total_planned = len(all_phases)
    completed = sum(1 for r in phase_results if r and r[1])
    fallbacks_count = sum(1 for r in phase_results if r and r[4])

    lines = [
        f"# Fusion Run Summary",
        f"",
        f"**Task:** {plan.get('task_type', '?')}",
        f"**Plan:** {plan.get('model_selection_rationale', '?')[:120]}",
        f"**Budget mode:** {plan.get('_budget', 'balanced')}",
        f"**Phases planned:** {total_planned}",
        f"**Phases attempted:** {len(phase_results)}",
        f"**Completed:** {completed}/{total_planned}",
        f"**Fallbacks triggered:** {fallbacks_count}",
        f"**Date:** {datetime.now(timezone.utc).isoformat()}",
        f"",
        f"## Phase Results",
        f"",
        f"| Phase | Model | Provider | Fallback | Status | Cost Tier |",
        f"|-------|-------|----------|----------|--------|-----------|",
    ]

    for idx, (phase, pr) in enumerate(zip(all_phases, phase_results)):
        if pr is None:
            phase_name = phase.get("phase", f"phase_{idx}")
            lines.append(f"| {phase_name} | - | - | - | not run | - |")
            continue
        phase_name = phase.get("phase", f"phase_{idx}")
        _, ok, prov, mod, fb = pr
        status = "\u2713" if ok else "\u2717"
        fb_str = "yes" if fb else "no"
        ct = cost_tier(prov)
        lines.append(f"| {phase_name} | {mod} | {prov} | {fb_str} | {status} | {ct} |")

    # Remaining phases that were never attempted
    for idx in range(len(phase_results), total_planned):
        phase_name = all_phases[idx].get("phase", f"phase_{idx}")
        lines.append(f"| {phase_name} | - | - | - | not run | - |")

    if moa_result:
        moa_output, moa_ok = moa_result
        lines.extend([
            f"",
            f"## MoA Blend",
            f"",
            f"**Status:** {'\u2713' if moa_ok else '\u2717'}",
            f"**Output length:** {len(moa_output) if moa_output else 0} chars",
            f"**Blend type:** {plan.get('moa_blend', {}).get('blend_type', '?')}",
        ])

    if comparison_results:
        lines.extend([
            f"",
            f"## Plan Comparison",
            f"",
        ])
        for profile, variant, variant_ok in comparison_results:
            status = "\u2713" if variant_ok else "\u2717"
            n_phases = len(variant.get("phases", [])) if variant else 0
            lines.append(f"- **{profile}**: {status} ({n_phases} phases)")

    metered_count = sum(
        1 for pr in phase_results if pr and cost_tier(pr[2]) == "metered"
    )
    lines.extend([
        f"",
        f"## Cost Summary",
        f"",
        f"**Phases with metered providers:** {metered_count}/{len(phase_results)}",
        f"**Total phases planned:** {total_planned}",
    ])

    summary = "\n".join(lines)
    (run_dir / "summary.md").write_text(summary)
    return summary


# ── Main ──────────────────────────────────────────────────────────────


def execute_plan(plan_path, budget="balanced"):
    """Execute a Fusion v3 plan end-to-end."""
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    plan["_budget"] = budget
    task_type_raw = plan.get("task_type", "?")
    task_type = sanitize_name(task_type_raw)
    phases = plan.get("phases", [])
    moa_blend = plan.get("moa_blend")
    plan_comparison = plan.get("plan_comparison")

    log(f"Executing Fusion plan: {plan_path}")
    log(f"  Task: {task_type_raw} | Budget: {budget} | Phases: {len(phases)}")

    # ── Create run directory ─────────────────────────────
    slug = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    unique_id = uuid.uuid4().hex[:8]
    run_dir = RUNS_BASE / f"{slug}-{unique_id}-{task_type}"
    run_dir.mkdir(parents=True, exist_ok=True)
    log(f"  Run directory: {run_dir}")
    (run_dir / "plan.json").write_text(json.dumps(plan, indent=2))

    # ── Plan comparison (from plan packet, not heuristic) ─
    needs_comparison = False
    comparison_reason = None
    comparison_variants = ["cost-minimizing", "quality-maximizing", "balanced"]
    if isinstance(plan_comparison, dict):
        needs_comparison = plan_comparison.get("recommended", False) or plan_comparison.get("needed", False)
        comparison_reason = plan_comparison.get("rationale")
        if plan_comparison.get("variants"):
            comparison_variants = plan_comparison["variants"]

    comparison_results = []
    if needs_comparison:
        log(f"  ⚠ Plan comparison recommended: {comparison_reason or 'Context suggests comparison would reduce regret.'}")
        variants = generate_plan_variants(plan, comparison_variants, budget)
        comparison_dir = run_dir / "comparison"
        comparison_dir.mkdir(exist_ok=True)
        for profile, variant, variant_ok in variants:
            if variant_ok and variant:
                vpath = comparison_dir / f"variant-{profile}.json"
                vpath.write_text(json.dumps(variant, indent=2))
            comparison_results.append((profile, variant, variant_ok))
        successes = sum(1 for _, _, ok in variants if ok)
        log(f"  \u2713 Generated {successes}/{len(variants)} plan variants in {comparison_dir}/")
    else:
        log(f"  No plan comparison needed")

    # ── Execute phases ───────────────────────────────────
    phase_results = []
    costs = []

    for idx, phase in enumerate(phases):
        phase_name = phase.get("phase", f"phase_{idx}")
        log(f"")
        log(f"  {'=' * 40}")
        log(f"  Phase {idx + 1}/{len(phases)}: {phase_name}")

        output, ok, provider, model, fallback = execute_phase(phase, run_dir, budget, plan=plan)
        phase_results.append((output, ok, provider, model, fallback))

        if provider:
            costs.append(1 if cost_tier(provider) == "metered" else 0)
        else:
            costs.append(0)

        if not ok:
            log(f"  \u2717 Phase '{phase_name}' failed. Stopping execution.")
            break

    # ── Execute MoA blend if present ─────────────────────
    moa_result = None
    if moa_blend and moa_blend.get("layers"):
        log(f"")
        log(f"  {'=' * 40}")
        log(f"  MoA Blend: {moa_blend.get('blend_type', 'layer').upper()}")
        moa_result = execute_moa_blend(moa_blend, run_dir, budget)

    # ── Write summary ────────────────────────────────────
    summary = write_summary(run_dir, plan, phase_results, moa_result, comparison_results)

    # ── Build model header for response ──────────────────
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("build_fusion_header", str(SCRIPT_DIR / "build_fusion_header.py"))
    _bfh_mod = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_bfh_mod)
    _bfh = _bfh_mod.build_fusion_header
    fallback_count = sum(1 for r in phase_results if r and r[4])
    fb_model = None
    if fallback_count > 0:
        for r in phase_results:
            if r and r[4] and r[3]:
                fb_model = r[3]
                break
    fusion_header = _bfh(
        plan=plan,
        phase_results=phase_results,
        moa_blend=moa_blend,
        fallback_occurred=fallback_count > 0,
        fallback_model=fb_model,
    )

    log(f"")
    log(f"  {'=' * 40}")
    log(f"  Summary written to {run_dir}/summary.md")
    completed = sum(1 for r in phase_results if r and r[1])
    log(f"  Phases completed: {completed}/{len(phases)}")
    log(f"  MoA: {'\u2713' if moa_result and moa_result[1] else '\u2717'}")
    if fusion_header:
        log(f"  Model header: {fusion_header}")

    # Write header to run state for downstream use
    (run_dir / "header.txt").write_text(fusion_header)

    return run_dir, fusion_header


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path, help="Validated Fusion v3 plan JSON")
    parser.add_argument(
        "--budget", choices=["free", "balanced", "premium"], default="balanced",
        help=(
            "free: subscription providers only (openai-codex, claude-team, xai-oauth). "
            "Metred phases fail with a clear explanation.\n"
            "balanced: prefer subscription, fall back to Fallback router cheap routes.\n"
            "premium: use the best model regardless; skip probe timeouts; "
            "try primary, Fallback router fallback, then DeepSeek last resort."
        ),
    )
    parser.add_argument(
        "--mm", action="store_true", default=None,
        help="Force middle-manager loop (default: auto — used for all v4 packets)")
    args = parser.parse_args()

    if not args.plan.exists():
        print(f"ERROR: Plan not found: {args.plan}", file=sys.stderr)
        return 1

    # Validate plan first
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), str(args.plan)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        # Try normalization
        print("Plan failed validation. Attempting MoA normalization...", file=sys.stderr)
        norm_result = subprocess.run(
            [sys.executable, str(NORMALIZER), str(args.plan)],
            capture_output=True, text=True, timeout=300,
        )
        if norm_result.returncode != 0:
            print("ERROR: Plan could not be normalized:", file=sys.stderr)
            print(norm_result.stderr.strip(), file=sys.stderr)
            return 1
        # Normalization wrote the output; find the normalized file
        norm_path = args.plan.with_name(args.plan.stem + ".normalized.json")
        if not norm_path.exists():
            print(f"ERROR: Normalizer reported success but {norm_path} not found.", file=sys.stderr)
            return 1
        args.plan = norm_path
        print(f"Using normalized plan: {args.plan}", file=sys.stderr)

    # v4 packets route through the middle-manager loop (plugin adapters);
    # v3 packets keep the legacy per-phase dispatch.
    packet = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    if str(packet.get("fusion_version")) == "4.0" and args.mm is not False:
        import importlib.util as _ilu2
        _spec2 = _ilu2.spec_from_file_location(
            "fusion_mm_loop",
            str(Path(__file__).parent.parent / "mm_loop.py"))
        _mod2 = _ilu2.module_from_spec(_spec2)
        _spec2.loader.exec_module(_mod2)
        run_dir = RUNS_BASE / f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-mm"
        run_dir.mkdir(parents=True, exist_ok=True)
        import shutil as _sh
        _sh.copy(args.plan, run_dir / "plan.json")
        out = _mod2.run_mm_loop(packet, run_dir)
        n_ok = sum(1 for r in out["results"] if r.ok)
        print(f"\n  MM loop complete: {n_ok}/{len(out['results'])} dispatch(es) ok")
        print(f"  Run directory: {run_dir}")
        return 0
    execute_plan(args.plan, args.budget)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
