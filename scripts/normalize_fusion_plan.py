#!/usr/bin/env python3
"""MoA Fusion Plan Normalizer — schema repair layer.

Uses a two-model MoA pipeline to normalize GPT's natural Fusion v3 plan output
into the canonical schema that validate_fusion_packet.py enforces.

Pipeline:
  1. DeepSeek does first-pass structural normalization (field aliases → canonical names,
     enum coercion, MoA restructure, missing field injection).
  2. GPT-5.6-sol validates semantic correctness and fixes meaning-level issues.
  3. Deterministic validator checks the result.
  4. If still invalid, GPT gets validator errors for one constrained revision.
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
VALIDATOR = SCRIPT_DIR / "validate_fusion_packet.py"

# ── Canonical schema reference for the normalizer prompts ────────────

CANONICAL_EXAMPLE = {
    "fusion_version": "3.0",
    "task_type": "strategy",
    "lead_model": "gpt-5.6-sol",
    "lead_provider": "openai-codex",
    "judgment_to_retain": ["Final recommendation"],
    "model_selection_rationale": "Why this model selection makes sense",
    "phases": [
        {
            "phase": "phase_name",
            "model": "gpt-5.6-sol",
            "provider": "openai-codex",
            "model_reason": "Why this model for this phase",
            "tools": ["web"],
            "max_turns": 5,
            "output": "output_file.md",
        }
    ],
    "moa_blend": None,
    "inputs": ["Source A"],
    "deliverables": ["Deliverable A"],
    "scope_in": ["Included"],
    "scope_out": ["Excluded"],
    "constraints": ["Constraint A"],
    "evidence_requirements": ["Evidence A"],
    "verification": ["Verification A"],
    "stop_conditions": ["Stop condition A"],
    "max_retries": 2,
}

ALLOWED_TASK_TYPES = {
    "strategy", "personal", "explicit", "research",
    "document", "operations", "monitoring", "technical", "direct",
}

ALLOWED_BLEND_TYPES = {"layer", "parallel", "amplify", "audit", "debate"}

# ── Field alias map ──────────────────────────────────────────────────

PHASE_ALIASES = {
    "phase": "phase",
    "id": "phase",
    "name": "phase",
    "model": "model",
    "provider": "provider",
    "role": "model_reason",
    "model_reason": "model_reason",
    "model_reasoning": "model_reason",
    "inputs": "inputs",
    "outputs": "output",
    "output": "output",
    "tools": "tools",
    "max_turns": "max_turns",
}

MOA_LAYER_ALIASES = {
    "model_1": "model_1",
    "provider_1": "provider_1",
    "model_2": "model_2",
    "provider_2": "provider_2",
    "purpose": "purpose",
    "layer": "layer",
    "output": "output",
}

# ── Helpers ────────────────────────────────────────────────────────────


def hermes_chat(prompt_text, provider, model, max_turns=1, timeout=120):
    """Run a tool-free Hermes chat and return the raw output."""
    cmd = [
        "hermes", "chat",
        "-q", prompt_text,
        "--provider", provider,
        "--model", model,
        "--max-turns", str(max_turns),
        "--quiet",
        "--ignore-rules",
        "--source", "tool",
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "HERMES_ACCEPT_HOOKS": "1"},
        )
        return result.stdout.strip() or result.stderr.strip()
    except subprocess.TimeoutExpired:
        return "TIMEOUT"
    except FileNotFoundError:
        return "HERMES_NOT_FOUND"


def load_json(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return data


def validate(path):
    """Run the deterministic validator. Returns (is_valid, errors)."""
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        return True, []
    errors = [l.strip() for l in result.stderr.split("\n") if l.strip() and not l.strip().startswith("INVALID")]
    return False, errors


# ── Phase 1: DeepSeek structural normalization ────────────────────────


def structural_normalize(raw_plan, workdir):
    """Phase 1: DeepSeek normalizes field names, enums, and structure.

    Returns a dict (the normally structured plan) or None if normalization fails.
    The output is saved to workdir/plan-phase1.json.
    """
    prompt = f"""You are a Fusion plan normalizer. Your job is to restructure a raw plan JSON into the canonical Fusion v3 schema while preserving ALL semantic content. Do NOT change meaning or drop fields.

CANONICAL SCHEMA (target structure):
{json.dumps(CANONICAL_EXAMPLE, indent=2)}

ALLOWED task_type values: {sorted(ALLOWED_TASK_TYPES)}
ALLOWED blend_type values: {sorted(ALLOWED_BLEND_TYPES)}

PHASE RULES:
- Each phase must have fields: phase, model, provider, model_reason, tools (list or "none"), max_turns, output
- Map the raw phase's id/name → phase, role/description → model_reason, outputs → output
- If provider is missing, infer from model (openai-codex→"openai-codex", grok→"xai-oauth", claude→"claude-team", deepseek→"deepseek")
- If max_turns is missing, infer: 1 for planning, 5-10 for execution, 3 for synthesis
- tools: if missing, use [] for reasoning phases, ["web"] for research, ["file"] for production, ["web","x_search"] for X work

MOA RULES:
- If moa_blend uses members[] array (model+contribution objects), restructure to:
  layers[] with model_1, provider_1, purpose per meaningful contributor, plus model_2/provider_2 when paired
  synthesis block with aggregator model/provider/instruction
- If moa_blend.enabled but no layers, create a single synthetic layer
- If moa_blend is missing but model references imply blending, leave as null

TOP-LEVEL RULES:
- fusion_version must be "3.0" (coerce "3" or "3.0.0" → "3.0")
- task_type must be one of the allowed enums. Map descriptive task types (e.g. "acquisition_channel_strategy" → "strategy", "personal_with_explicit" → "explicit")
- lead_model and lead_provider are simple strings
- judgment_to_retain: accept any array of strings
- If lead_judgment.must_remain_with_lead exists, merge into judgment_to_retain

INPUT RAW PLAN:
{json.dumps(raw_plan, indent=2)}

Return ONLY the restructured JSON. No explanations, no markdown fences."""
    raw = hermes_chat(prompt, "deepseek", "deepseek-v4-flash", max_turns=1, timeout=180)
    if raw in ("TIMEOUT", "HERMES_NOT_FOUND"):
        print(f"  PHASE_1_FAILED: {raw}", file=sys.stderr)
        return None

    # Extract JSON from possibly-fenced output
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < start:
        print(f"  PHASE_1_FAILED: no JSON in DeepSeek output", file=sys.stderr)
        return None

    try:
        plan = json.loads(raw[start: end + 1])
    except json.JSONDecodeError as e:
        print(f"  PHASE_1_FAILED: JSON parse error: {e}", file=sys.stderr)
        return None

    out_path = workdir / "plan-phase1.json"
    out_path.write_text(json.dumps(plan, indent=2))
    print(f"  PHASE_1_COMPLETE: {out_path}")
    return plan


# ── Phase 2: GPT-5.6-sol semantic validation ─────────────────────────


def semantic_refine(phase1_plan, raw_plan, workdir):
    """Phase 2: GPT-5.6-sol validates and fixes semantic correctness.

    Takes the structurally normalized plan and the original raw plan.
    Returns the semantically corrected dict or None.
    """
    valid, errors = validate(workdir / "plan-phase1.json")
    schema_report = "\n".join(errors) if errors else "VALID"

    prompt = f"""You are a Fusion plan reviewer. Review the structurally normalized plan below against the original raw plan and the schema validator report.

FIX THE PLAN:
1. The structure has been normalized but semantics may be wrong or coarse.
2. Fix model_reason to be specific and compelling about WHY that model fits the phase.
3. Fix model_selection_rationale to reflect real model strengths.
4. Merge any judgment descriptions that were lost during normalization.
5. Infer missing provider/model/turns fields specifically rather than using defaults.
6. If task_type was mapped to the closest enum but doesn't fit perfectly, pick a better one.
7. Ensure moa_blend layers are semantically accurate (model_1 vs model_2 pairing, correct purposes).
8. Do NOT re-structure fields that already match the canonical schema.

SCHEMA VALIDATOR REPORT:
{schema_report}

STRUCTURALLY NORMALIZED PLAN:
{json.dumps(phase1_plan, indent=2)}

ORIGINAL RAW PLAN (for semantic reference):
{json.dumps(raw_plan, indent=2)}

Return ONLY the corrected Fusion v3 plan JSON. Return it EXACTLY matching the canonical schema. No fences, no explanations."""
    raw = hermes_chat(prompt, "openai-codex", "gpt-5.6-sol", max_turns=1, timeout=180)
    if raw in ("TIMEOUT", "HERMES_NOT_FOUND", ""):
        print(f"  PHASE_2_FAILED: {raw if raw else 'empty'}", file=sys.stderr)
        return phase1_plan  # fall back to phase 1

    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < start:
        print(f"  PHASE_2_FAILED: no JSON in GPT output", file=sys.stderr)
        return phase1_plan

    try:
        plan = json.loads(raw[start: end + 1])
    except json.JSONDecodeError:
        print(f"  PHASE_2_FAILED: JSON parse error, falling back to phase 1", file=sys.stderr)
        return phase1_plan

    out_path = workdir / "plan-phase2.json"
    out_path.write_text(json.dumps(plan, indent=2))
    print(f"  PHASE_2_COMPLETE: {out_path}")
    return plan


# ── Phase 3: Constrained revision (only if validation still fails) ────


def constrained_revision(plan, workdir):
    """Phase 3: If validator still rejects, send errors to GPT for one fix.

    Returns the corrected dict or None if everything failed.
    """
    valid, errors = validate(workdir / "plan-phase2.json")
    if valid:
        return plan

    error_text = "\n".join(errors)
    prompt = f"""The following Fusion v3 plan failed schema validation with these errors:

{error_text}

PLAN:
{json.dumps(plan, indent=2)}

Fix the plan to pass validation. Preserve all semantic content. Return ONLY the corrected JSON. No fences.

Key rules:
- fusion_version must be "3.0"
- task_type must be one of: {sorted(ALLOWED_TASK_TYPES)}
- moa_blend.blend_type must be one of: {sorted(ALLOWED_BLEND_TYPES)}
- Every phase needs: phase, model, provider, model_reason, tools (list), max_turns (int), output (str)
- judgment_to_retain must be a non-empty array of strings
- max_retries must be an int between 1 and 4
- deliverable_results in review: each item needs status (str) and evidence (str)
- quality_results: each item needs status and evidence"""
    raw = hermes_chat(prompt, "openai-codex", "gpt-5.6-sol", max_turns=1, timeout=120)
    if raw in ("TIMEOUT", "HERMES_NOT_FOUND", ""):
        print(f"  PHASE_3_FAILED: {raw if raw else 'empty'}", file=sys.stderr)
        return None

    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < start:
        return None

    try:
        plan = json.loads(raw[start: end + 1])
    except json.JSONDecodeError:
        return None

    out_path = workdir / "plan-revised.json"
    out_path.write_text(json.dumps(plan, indent=2))
    valid, errors = validate(out_path)
    if valid:
        print(f"  PHASE_3_COMPLETE: passed after revision")
    else:
        print(f"  PHASE_3_FAILED: still invalid: {errors[:3]}...", file=sys.stderr)
        return None
    return plan


# ── Orchestrator ──────────────────────────────────────────────────────


def normalize(raw_plan_path, output_path=None):
    """Run the full MoA normalization pipeline on a raw plan JSON file.

    Args:
        raw_plan_path: Path to the raw plan JSON (as generated by GPT-5.6-sol)
        output_path: Where to write the final normalized plan. Default: <input>.normalized.json

    Returns:
        Path to the validated normalized plan, or None if all phases failed.
    """
    raw_path = Path(raw_plan_path)
    raw_plan = load_json(raw_path)
    workdir = raw_path.parent
    if output_path:
        out = Path(output_path)
    else:
        out = raw_path.with_name(raw_path.stem + ".normalized.json")

    print(f"=== MoA Plan Normalizer ===", file=sys.stderr)
    print(f"  Input: {raw_plan_path}", file=sys.stderr)
    print(f"  Raw task_type: {raw_plan.get('task_type', '?')}", file=sys.stderr)
    print()

    # Phase 1: DeepSeek structural normalization
    print(f"Phase 1: DeepSeek structural normalization...", file=sys.stderr)
    phase1 = structural_normalize(raw_plan, workdir)
    if phase1 is None:
        print(f"FATAL: Phase 1 failed. Cannot continue.", file=sys.stderr)
        return None

    # Phase 2: GPT semantic refinement
    print(f"Phase 2: GPT-5.6-sol semantic refinement...", file=sys.stderr)
    phase2 = semantic_refine(phase1, raw_plan, workdir)
    if phase2 is None:
        print(f"FATAL: Phase 2 failed. Falling back to Phase 1 result.", file=sys.stderr)
        phase2 = phase1

    # Check if valid after Phase 2
    valid, errors = validate(workdir / "plan-phase2.json")
    if valid:
        print(f"✓ Plan passed validation after Phase 2", file=sys.stderr)
        out.write_text(json.dumps(phase2, indent=2))
        print(f"  Output: {out}", file=sys.stderr)
        return out

    # Phase 3: constrained revision
    print(f"  Phase 2 validation failed: {errors[:3]}...", file=sys.stderr)
    print(f"Phase 3: Constrained GPT revision...", file=sys.stderr)
    final = constrained_revision(phase2, workdir)
    if final is None:
        print(f"FATAL: All phases exhausted. Plan could not be normalized.", file=sys.stderr)
        return None

    out.write_text(json.dumps(final, indent=2))
    print(f"✓ Plan passed validation after Phase 3", file=sys.stderr)
    print(f"  Output: {out}", file=sys.stderr)
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Raw Fusion v3 plan JSON path")
    parser.add_argument("-o", "--output", type=Path, default=None, help="Output path")
    args = parser.parse_args()

    result = normalize(args.input, args.output)
    return 0 if result else 1


if __name__ == "__main__":
    raise SystemExit(main())
