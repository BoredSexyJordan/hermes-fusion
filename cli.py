"""`hermes fusion ...` CLI — argparse tree + handlers for the fusion plugin.

Subcommands are thin: they validate/normalize/execute via the packaged scripts
in ./scripts, show the detected roster (built from the user's OWN configured
providers — never a hardcoded fleet), sync/query the model index, and print
feedback reports. The `roster` module owns detection; user overrides live in
HERMES_HOME/fusion.yaml (see fusion.example.yaml).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from paths import hermes_home, hermes_python
import roster

PLUGIN_DIR = Path(__file__).parent
HOME = hermes_home()
SCRIPTS = PLUGIN_DIR / "scripts"           # packaged alongside the plugin
INDEX_PATH = HOME / "fusion" / "model-index.json"
RUNS_BASE = HOME / "fusion" / "runs"
FEEDBACK_DIR = HOME / "fusion" / "feedback"
CONFIG_PATH = HOME / "fusion.yaml"


# ── Config (fusion.yaml) ───────────────────────────────────────────────

def _load_fusion_config() -> dict:
    """Read user overrides from HERMES_HOME/fusion.yaml (optional)."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        import yaml
        return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # config is advisory; never hard-crash the CLI
        print(f"warning: could not read {CONFIG_PATH}: {exc}", file=sys.stderr)
        return {}


def rosters(harness: str = "all") -> dict:
    cfg = _load_fusion_config()
    return roster.build_rosters(harness=harness,
                                overrides=cfg.get("team_templates"))


# ── Script dispatch ────────────────────────────────────────────────────

def _script(name: str) -> str:
    return str(SCRIPTS / name)


def _run(args: list, timeout: int = 300) -> int:
    try:
        result = subprocess.run(args, timeout=timeout)
        return result.returncode
    except FileNotFoundError:
        print(f"missing dependency: {args[0]}", file=sys.stderr)
        return 127
    except subprocess.TimeoutExpired:
        print("timed out", file=sys.stderr)
        return 124


def cmd_validate(args) -> int:
    return _run([hermes_python(), _script("validate_fusion_packet.py"), args.packet])


def cmd_plan(args) -> int:
    """plan: extract + validate (+ normalize hint on failure). Read-only assist."""
    raw = Path(args.raw)
    out = Path(args.out)
    rc = _run([hermes_python(), _script("extract_fusion_json.py"), str(raw), str(out)])
    if rc != 0:
        return rc
    return _run([hermes_python(), _script("validate_fusion_packet.py"), str(out)])


def cmd_run(args) -> int:
    argv = [hermes_python(), _script("execute_fusion_plan.py"), args.plan]
    if args.budget:
        argv += ["--budget", args.budget]
    return _run(argv, timeout=1800)


def cmd_teams(args) -> int:
    """Show detected fleets (per harness) + the Frontier->MM->Frontier rosters."""
    harness = getattr(args, "harness", "all")
    if args.show:
        tmpl = rosters(harness).get(args.show)
        if not tmpl:
            print(f"unknown template: {args.show}", file=sys.stderr)
            return 1
        print(json.dumps({**tmpl, "harness": harness, "pool": roster.detected_pool(harness=harness)}, indent=1))
        return 0

    print(f"Detected model fleet (harness filter: {harness}):")
    if harness == "all":
        for h, pool in roster.harness_breakdown().items():
            line = roster.configured_models_hint(harness=h)
            print(f"  [{h}] {line}")
    else:
        print(f"  {roster.configured_models_hint(harness=harness)}")
    print()
    print("Fusion rosters (Frontier -> Middle Manager -> Frontier):")
    print()
    for name, tmpl in sorted(rosters(harness).items()):
        mm = tmpl.get("middle_manager", {})
        workers = ", ".join(f"{m['role']}={m['model']}@{m['provider']}"
                            for m in tmpl.get("members", [])) or "(none)"
        consults = ", ".join(c["role"] for c in tmpl.get("consults", [])) or "-"
        print(f"  {name}  [{tmpl.get('task_type', 'general')}]")
        print(f"    MM:       {mm.get('model')} via {mm.get('provider')}")
        print(f"    members:  {workers}")
        print(f"    consults: {consults}")
    print()
    print(f"Override any roster in {CONFIG_PATH} (see fusion.example.yaml).")
    return 0


def cmd_index(args) -> int:
    sync = _script("sync_model_index.py")
    cfg = CONFIG_PATH if CONFIG_PATH.exists() else HOME / "config.yaml"
    rc = _run([hermes_python(), sync, "--config", str(cfg)])
    if rc != 0:
        return rc

    idx = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    if args.query:
        hits = [m for m in idx.get("models", {}) if args.query.lower() in m.lower()]
        for m in hits:
            entry = idx["models"][m]
            meta = entry.get("metadata") or {}
            print(f"  {m:40s} providers={','.join(entry.get('providers', []))} "
                  f"ctx={meta.get('context', '?')} reasoning={meta.get('reasoning', '?')}")
        if not hits:
            print(f"  no indexed model matches '{args.query}'")
        return 0
    print(f"  {len(idx.get('models', {}))} models across "
          f"{len(idx.get('configured_providers', {}))} providers "
          f"(generated {idx.get('_generated', '?')})")
    return 0


def cmd_report(args) -> int:
    analyzer = _script("analyze_fusion_feedback.py")
    rc = _run([hermes_python(), analyzer])
    if rc == 0:
        import datetime as _dt
        name = f"feedback-{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%d')}.md"
        path = FEEDBACK_DIR / name
        if path.exists():
            print(path.read_text(encoding="utf-8"))
    return rc


def cmd_status(args) -> int:
    runs = sorted(p for p in RUNS_BASE.iterdir() if p.is_dir()) if RUNS_BASE.exists() else []
    print(f"Fusion runs: {len(runs)}")
    for run_dir in runs[-5:]:
        plan = {}
        plan_path = run_dir / "plan.json"
        if plan_path.exists():
            try:
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        team_path = run_dir / "team.json"
        team = team_path.read_text(encoding="utf-8")[:40] if team_path.exists() else ""
        print(f"  {run_dir.name}  task={plan.get('task_type', '?'):12s} "
              f"v={plan.get('fusion_version', '?')}  team={'yes' if team else 'legacy'}")
    return 0


def setup_cli(subparser) -> None:
    sub = subparser.add_subparsers(dest="fusion_cmd", required=True)

    p = sub.add_parser("validate", help="Validate a plan packet (no team neural net needed)")
    p.add_argument("packet", help="Path to plan packet JSON")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("plan", help="Extract + validate a raw planner output file")
    p.add_argument("raw", help="Raw model output containing the packet JSON")
    p.add_argument("--out", default="plan.json", help="Where to write the extracted packet")
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("run", help="Execute a validated packet through the MM loop")
    p.add_argument("plan", help="Path to a VALIDATED plan packet")
    p.add_argument("--budget", choices=["free", "balanced", "premium"], default=None)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("teams", help="Show the detected fleet + Frontier->MM->Frontier rosters")
    p.add_argument("--show", help="Show full JSON for one roster name")
    p.set_defaults(func=cmd_teams)

    p = sub.add_parser("index", help="Sync + query the living model index")
    p.add_argument("--query", help="Substring filter over indexed model ids")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("report", help="Run the weekly feedback scorer and print the report")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("status", help="Show recent runs and their shape")
    p.set_defaults(func=cmd_status)


def fusion_command(args) -> int:
    return args.func(args)
