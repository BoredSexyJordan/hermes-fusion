#!/usr/bin/env python3
"""Fusion v4 Middle-Manager loop — consumes a validated v4 packet's team block.

Design (frozen contract, per the SOL/Fable reviews):
- The MM executes each phase by dispatching to the rostered member via
  plugins/fusion/adapters.py (delegate_task | kanban_handoff | clean_session).
- Delegation kind is pinned per-member in the template; the MM NEVER chooses it.
- Every dispatch is audited (prompt auditor) BEFORE creation — hard gate,
  one MM rewrite cycle, then T0 escalation. Never bypass.
- Team state (team.json) + per-member attestation records (harness.json) are
  written to the run dir so the feedback loop scores what ACTUALLY ran.
- Provenance: actual execution identity from adapter receipts, never the roster.

Usage (from the executor or standalone):
  python3 mm_loop.py --plan plan.json --run-dir <dir> [--phase-filter name]
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Import the adapter layer from the plugin package (co-located)
sys.path.insert(0, str(Path(__file__).parent))
from adapters import Dispatch, dispatch as adapter_dispatch  # noqa: E402

DEFAULT_DEADLINE_S = 600
MAX_REWRITE_CYCLES = 1


# ── Run-state writers ─────────────────────────────────────────────────

def write_team_state(run_dir: Path, plan: dict) -> dict:
    """Persist the executed roster (team.json) with attestation slots."""
    team = plan.get("team") or {}
    state = {
        "template": team.get("template", "adhoc"),
        "middle_manager": plan.get("_mm_actual") or team.get("middle_manager", {}),
        "members": [],
        "consults": team.get("consults", []),
        "escalation_contract": team.get("escalation_contract", {}),
        "audit_violations": 0,
        "_generated": datetime.now(timezone.utc).isoformat(),
    }
    for m in team.get("members", []):
        state["members"].append({
            "role": m.get("role"), "model": m.get("model"), "provider": m.get("provider"),
            "delegation": m.get("delegation", "clean_session"),
            "thread_policy": m.get("thread_policy", "fresh_per_item"),
            "tier": m.get("tier", "T2"),
        })
    (run_dir / "team.json").write_text(json.dumps(state, indent=1), encoding="utf-8")
    return state


def record_harness(run_dir: Path, results: list[dict], mm_meta: dict) -> None:
    """harness.json — per-dispatch execution receipts (attestation, not intent)."""
    records = []
    for res in results:
        records.append({
            "dispatch_id": res.dispatch_id,
            "member_role": res.__dict__.get("member_role", "?"),
            "status": res.status,
            "ok": res.ok,
            "actual_model": res.actual_model,
            "actual_provider": res.actual_provider,
            "output_path": res.output_path,
            "task_ref": res.task_ref,
            "seconds": res.seconds,
            "retries": res.retries,
        })
    payload = {
        "harness": "hermes",
        "mm": mm_meta,
        "dispatches": records,
        "_generated": datetime.now(timezone.utc).isoformat(),
    }
    (run_dir / "harness.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")


# ── Prompt auditor (hard gate) ────────────────────────────────────────

PRESCRIPTION_MARKERS = [
    "use a ", "use an ", "make sure the", "be sure to", "don't forget to",
    "you should implement", "implement it with", "using the following code",
    "style it with", "color it", "font should be", "margin should be",
    "write the function as", "use the pattern", "structure it like",
]


def audit_dispatch_prompt(task_text: str) -> tuple[bool, list[str]]:
    """Deterministic first-pass audit: does the MM prompt prescribe HOW?

    Returns (pass, violations). The v1 auditor is heuristic (marker phrases +
    length heuristics); a frontier second-pass auditor can be layered later via
    clean_session. Fable ruling: hard gate from day one; FP sampling weekly.
    """
    violations = []
    low = task_text.lower()
    for marker in PRESCRIPTION_MARKERS:
        if marker in low:
            violations.append(f"prescriptive phrase: '{marker}'")
    # Verbose taste spec heuristic: a worker prompt > 2500 chars is likely a
    # lossy implementation spec rather than goal+evidence.
    if len(task_text) > 2500:
        violations.append(f"prompt too long ({len(task_text)} chars) — likely prescribes implementation")
    return (len(violations) == 0, violations)


def rewrite_once(task_text: str, violations: list[str]) -> str:
    """Bounded MM rewrite: strip the violating content, keep goal+evidence.

    v1 mechanical rewrite: drop lines containing prescription markers and trim
    to goal/evidence sections. (Frontier-assisted rewrite is a later upgrade.)
    """
    kept = []
    for line in task_text.splitlines():
        low = line.lower()
        if any(v.replace("prescriptive phrase: ", "").strip("'") in low for v in violations if "prescriptive" in v):
            continue
        kept.append(line)
    out = "\n".join(kept).strip()
    if len(out) > 2500:
        out = out[:2400] + "\n[truncated by audit rewrite]"
    return out


# ── The loop ──────────────────────────────────────────────────────────

def run_mm_loop(plan: dict, run_dir: Path, phase_filter: str | None = None,
                dry_run: bool = False) -> dict:
    team = plan.get("team") or {}
    members = team.get("members") or []
    by_role = {m.get("role"): m for m in members}
    inputs = plan.get("inputs") or []
    inputs_text = "\n".join(f"- {i}" for i in inputs) if inputs else "(no task inputs)"
    deliverables = plan.get("deliverables") or []
    deliv_text = ", ".join(deliverables) or "(unspecified)"

    team_state = write_team_state(run_dir, plan)
    all_results = []
    audit_log = []
    escalations = []

    phases = plan.get("phases") or []
    for idx, phase in enumerate(phases, 1):
        pname = phase.get("phase", f"phase-{idx}")
        if phase_filter and pname != phase_filter:
            continue
        # Resolve the rostered member for this phase: phase->role match, else
        # phase.model match, else round-robin across members.
        member = None
        for m in members:
            if m.get("role") == pname or m.get("model") == phase.get("model"):
                member = m
                break
        if member is None and members:
            member = members[idx % len(members)]
        if member is None:
            print(f"[mm] phase '{pname}': no rostered member, skipping", file=sys.stderr)
            continue

        goal = (
            f"TASK (from the plan's inputs):\n{inputs_text}\n\n"
            f"EXPECTED DELIVERABLE: {deliv_text}\n\n"
            f"PHASE: {pname}\n"
            f"Scope: {json.dumps(plan.get('scope_in', []))}\n"
            f"Out of scope: {json.dumps(plan.get('scope_out', []))}\n"
            f"Stop conditions: {json.dumps(plan.get('stop_conditions', []))}\n"
            f"Evidence required: {json.dumps(plan.get('evidence_requirements', []))}\n"
        )

        # Hard-gate audit
        ok_audit, violations = audit_dispatch_prompt(goal)
        cycles = 0
        while not ok_audit and cycles < 1:
            goal = rewrite_once(goal, violations)
            ok_audit, violations = audit_dispatch_prompt(goal)
            cycles += 1
            team_state["audit_violations"] += 1
        audit_log.append({"phase": pname, "violations": violations, "rewritten": cycles > 0 or not ok_audit})
        if not ok_audit:
            # escalate to T0 rather than dispatch unaudited (never bypass)
            escalations.append({"phase": pname, "reason": violations})
            print(f"[mm] phase '{pname}': audit unresolved after rewrite -> escalating to T0", file=sys.stderr)

        d = Dispatch(
            dispatch_id=str(uuid.uuid4()),
            run_dir=str(run_dir),
            member={**member, "assignee": member.get("assignee", member.get("role", "worker"))},
            task=goal,
            deadline_s=int(phase.get("deadline_s") or team.get("deadline_s") or DEFAULT_DEADLINE_S),
        )
        if dry_run:
            print(f"[mm] DRY dispatch -> {member.get('role')} ({member.get('delegation')}) "
                  f"model={member.get('model')} via {member.get('provider')}", file=sys.stderr)
            all_results.append(type("R", (), {"__dict__": {"member_role": member.get("role")},
                "dispatch_id": d.dispatch_id, "ok": True, "status": "accepted",
                "actual_model": member.get("model", "?"), "actual_provider": member.get("provider", "?"),
                "output_path": "", "task_ref": "", "seconds": 0, "retries": 0})())
            continue

        print(f"[mm] phase '{pname}' -> {member.get('role')} via {member.get('delegation')} "
              f"on {member.get('provider')}/{member.get('model')}", file=sys.stderr)
        res = adapter_dispatch(d)
        res.member_role = member.get("role", "?")  # type: ignore[attr-defined]
        all_results.append(res)
        flag = "✓" if res.ok else "✗"
        print(f"[mm]   {flag} {res.status} ({res.seconds}s) -> {res.output_path or res.err[:120]}", file=sys.stderr)

    mm_meta = {
        "model": (team.get("middle_manager") or {}).get("model", "?"),
        "provider": (team.get("middle_manager") or {}).get("provider", "?"),
        "phases": len(phases),
        "escalations": escalations,
        "audit": audit_log,
    }
    if not dry_run:
        record_harness(run_dir, all_results, mm_meta)
        # Attested provenance header: built from ACTUAL execution receipts
        # (harness.json), never the roster (Fable's planned≠ran fix).
        try:
            import importlib.util as _ilu
            _spec = _ilu.spec_from_file_location(
                "build_fusion_header",
                str(Path(__file__).parent / "scripts" / "build_fusion_header.py"))
            _mod = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            prs = [(res.output_path, res.ok, res.actual_provider, res.actual_model, False)
                   for res in all_results]
            header = _mod.build_fusion_header(plan=plan, phase_results=prs)
            if header:
                (run_dir / "header.txt").write_text(header + "\n", encoding="utf-8")
        except Exception as _exc:  # header is cosmetic — never fail the run for it
            print(f"[mm] header build skipped: {_exc}", file=sys.stderr)
    # update team.json audit count
    team_state["audit_violations"] = sum(1 for a in audit_log if a["violations"])
    (run_dir / "team.json").write_text(json.dumps(team_state, indent=1), encoding="utf-8")
    return {"results": all_results, "mm": mm_meta, "team_state": team_state}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plan", required=True, help="Validated v4 packet")
    ap.add_argument("--run-dir", required=True, help="Run directory")
    ap.add_argument("--phase-filter", default=None)
    ap.add_argument("--dry-run", action="store_true", help="Print dispatch plan, don't execute")
    args = ap.parse_args()

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    if str(plan.get("fusion_version")) != "4.0":
        print("mm_loop requires a validated v4 packet", file=sys.stderr)
        return 1
    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    out = run_mm_loop(plan, run_dir, phase_filter=args.phase_filter, dry_run=args.dry_run)
    print(f"[mm] done: {len(out['results'])} dispatch(s), "
          f"{out['mm']['model']} as MM, escalations={len(out['mm']['escalations'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
