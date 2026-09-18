#!/usr/bin/env python3
"""Validate Hermes Fusion v3 plan packets."""

import argparse
import json
import sys
from pathlib import Path


def load(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ValueError(f"file not found: {path}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON line {exc.lineno}, column {exc.colno}: {exc.msg}")
    if not isinstance(data, dict):
        raise ValueError("packet root must be an object")
    return data


def validate_plan(data):
    errors = []
    required = {
        "fusion_version": str,
        "task_type": str,
        "lead_model": str,
        "lead_provider": str,
        "judgment_to_retain": list,
        "model_selection_rationale": str,
        "phases": list,
        "inputs": list,
        "deliverables": list,
        "scope_in": list,
        "scope_out": list,
        "constraints": list,
        "evidence_requirements": list,
        "verification": list,
        "stop_conditions": list,
        "max_retries": int,
    }

    for field, expected in required.items():
        if field not in data:
            errors.append(f"missing field: {field}")
            continue
        value = data[field]
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            errors.append(f"{field}: expected {expected.__name__}, got {type(value).__name__}")

    if errors:
        return errors

    # Validate version (v4 adds team; v3 legacy still accepted)
    if str(data["fusion_version"]) not in {"3.0", "4.0"}:
        errors.append("fusion_version must be '3.0' or '4.0'")

    # v4: team roster required; roster-level model pinning is the gate
    if str(data["fusion_version"]) == "4.0":
        team = data.get("team")
        if not isinstance(team, dict):
            errors.append("v4 packet requires a team object")
        else:
            for tfield in ("template", "middle_manager", "members"):
                if tfield not in team:
                    errors.append(f"team missing field: {tfield}")
            mm = team.get("middle_manager")
            if isinstance(mm, dict):
                for mfield in ("model", "provider"):
                    if not mm.get(mfield):
                        errors.append(f"team.middle_manager missing field: {mfield}")
            else:
                errors.append("team.middle_manager must be an object with model+provider")
            members = team.get("members")
            if not isinstance(members, list) or not members:
                errors.append("team.members must be a non-empty list")
            elif len(members) > 4:
                errors.append(f"team.members has {len(members)} workers (max 4 — keep teams small)")
            else:
                for midx, mem in enumerate(members):
                    if not isinstance(mem, dict):
                        errors.append(f"team.members[{midx}] must be an object")
                        continue
                    for mfield in ("role", "model", "provider", "delegation"):
                        if not mem.get(mfield):
                            errors.append(f"team.members[{midx}] missing field: {mfield}")
                    if mem.get("delegation") not in {"delegate_task", "kanban_handoff", "clean_session"}:
                        errors.append(f"team.members[{midx}].delegation must be delegate_task|kanban_handoff|clean_session")
                    if mem.get("thread_policy") not in (None, "continuous", "fresh_per_item"):
                        errors.append(f"team.members[{midx}].thread_policy must be 'continuous' or 'fresh_per_item'")
            ec = team.get("escalation_contract")
            if ec is not None and not isinstance(ec, dict):
                errors.append("team.escalation_contract must be an object")

    # Validate task_type
    allowed_types = {"strategy", "personal", "explicit", "research", "document", "operations", "monitoring", "technical", "direct"}
    if data["task_type"] not in allowed_types:
        errors.append(f"task_type must be one of: {sorted(allowed_types)}")

    # Validate phases
    if not data["phases"]:
        errors.append("phases must contain at least one phase")
    else:
        for idx, phase in enumerate(data["phases"]):
            if not isinstance(phase, dict):
                errors.append(f"phases[{idx}] must be an object")
                continue
            for pfield in ("phase", "model", "provider", "model_reason", "max_turns", "output"):
                if pfield not in phase:
                    errors.append(f"phases[{idx}] missing field: {pfield}")
            if "tools" in phase and not isinstance(phase["tools"], list):
                errors.append(f"phases[{idx}].tools must be a list")

    # Validate moa_blend if present
    moa = data.get("moa_blend")
    if moa is not None:
        if not isinstance(moa, dict):
            errors.append("moa_blend must be an object or null")
        else:
            for mfield in ("blend_type", "aggregator_model", "aggregator_provider", "layers"):
                if mfield not in moa:
                    errors.append(f"moa_blend missing field: {mfield}")
            if moa.get("blend_type") not in {"layer", "parallel", "amplify", "audit", "debate"}:
                errors.append("moa_blend.blend_type must be one of: layer, parallel, amplify, audit, debate")
            if not isinstance(moa.get("layers"), list) or not moa["layers"]:
                errors.append("moa_blend.layers must be a non-empty list")
            else:
                for lidx, layer in enumerate(moa["layers"]):
                    if not isinstance(layer, dict):
                        errors.append(f"moa_blend.layers[{lidx}] must be an object")
                        continue
                    for lfield in ("layer", "purpose", "model_1", "provider_1", "output"):
                        if lfield not in layer:
                            errors.append(f"moa_blend.layers[{lidx}] missing field: {lfield}")
            if "synthesis" not in moa:
                errors.append("moa_blend missing synthesis block")
            elif not isinstance(moa["synthesis"], dict):
                errors.append("moa_blend.synthesis must be an object")
            else:
                for sfield in ("model", "provider", "instruction"):
                    if sfield not in moa["synthesis"]:
                        errors.append(f"moa_blend.synthesis missing field: {sfield}")

    # Validate non-empty lists
    for field in ("judgment_to_retain", "deliverables", "scope_in", "constraints", "evidence_requirements", "verification", "stop_conditions"):
        if not data.get(field):
            errors.append(f"{field} must not be empty")

    # Validate retries
    if not 1 <= data["max_retries"] <= 4:
        errors.append("max_retries must be between 1 and 4")

    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="Path to plan packet JSON")
    args = parser.parse_args()
    try:
        packet = load(args.path)
        errors = validate_plan(packet)
    except ValueError as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    if errors:
        print("INVALID", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"VALID plan: {args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
