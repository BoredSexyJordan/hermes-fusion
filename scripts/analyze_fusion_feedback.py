#!/usr/bin/env python3
"""Fusion Feedback Analyzer v4 — unified scoring + open-minded recommendations.

Scores TEAM TEMPLATES per task type (the v4 learning unit), tracks per-member
reliability, maintains the living model index (via sync_model_index.py), and
emits recommendations that may propose models OUTSIDE the current roster when
the index shows a better capability/cost fit.

Reads:  ~/.hermes/fusion/runs/*/  (plan.json, team.json, summary.md, provenance.json)
Writes: ~/.hermes/fusion/feedback/feedback-<date>.md
        ~/.hermes/fusion/model-index.json  (refreshed if stale)

Schedule: weekly (cron job `Fusion Feedback Loop`, Sundays 09:00)
"""

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
RUNS_BASE = HOME / "fusion" / "runs"
REPORT_DIR = HOME / "fusion" / "feedback"
INDEX_PATH = HOME / "fusion" / "model-index.json"
SYNC_SCRIPT = HOME / "scripts" / "sync_model_index.py"
INDEX_MAX_AGE_DAYS = 14
MIN_RUNS_FOR_TEMPLATE_CHANGE = 3

sys.path.insert(0, str(SYNC_SCRIPT.parent))


def ensure_model_index():
    """Refresh the model index if missing or stale."""
    import subprocess

    fresh = False
    if INDEX_PATH.exists():
        try:
            idx = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
            gen = datetime.fromisoformat(idx.get("_generated", "2000-01-01T00:00:00+00:00"))
            age_days = (datetime.now(timezone.utc) - gen).days
            if age_days <= INDEX_MAX_AGE_DAYS:
                return idx, age_days, False
        except Exception:
            pass
    try:
        subprocess.run(
            [sys.executable, str(SYNC_SCRIPT), "--config", str(HOME / ".hermes" / "config.yaml")],
            check=True, capture_output=True, timeout=60,
        )
        fresh = True
    except Exception as exc:
        print(f"model index sync failed: {exc}", file=sys.stderr)
    if INDEX_PATH.exists():
        idx = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        return idx, 0, fresh
    return {"models": {}, "_generated": None}, INDEX_MAX_AGE_DAYS + 1, fresh


def collect_runs(since_days=7):
    """Collect run artifacts from the last N days (completed AND failed)."""
    runs = []
    now = datetime.now(timezone.utc)
    if not RUNS_BASE.exists():
        return runs
    for run_dir in sorted(RUNS_BASE.iterdir()):
        if not run_dir.is_dir():
            continue
        parts = run_dir.name.replace("_", "-").split("-", 2)
        if len(parts) < 2:
            continue
        try:
            run_time = datetime.strptime(f"{parts[0]}-{parts[1]}", "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if (now - run_time).days > since_days:
            continue

        def read(p):
            return p.read_text(encoding="utf-8") if p.exists() else ""

        def readj(p):
            try:
                return json.loads(read(p) or "{}")
            except json.JSONDecodeError:
                return {}

        runs.append({
            "dir": run_dir.name,
            "timestamp": run_time.isoformat(),
            "age_days": (now - run_time).days,
            "task_type": readj(run_dir / "plan.json").get("task_type", "?"),
            "plan": readj(run_dir / "plan.json"),
            "team": readj(run_dir / "team.json"),
            "summary": read(run_dir / "summary.md"),
            "provenance": readj(run_dir / "provenance.json"),
        })
    return runs


def member_records(run):
    """Unify per-member execution records from team.json or provenance.json."""
    records = []
    team = run.get("team") or {}
    members = team.get("members") or []
    provs = run.get("provenance") if isinstance(run.get("provenance"), list) else []

    if members:
        # team.json is source of truth for v4 runs; match provenance by model
        prov_by_model = defaultdict(list)
        for p in provs:
            if isinstance(p, dict) and p.get("model"):
                prov_by_model[p["model"]].append(p)
        for m in members:
            plist = prov_by_model.get(m.get("model"), [])
            ok = all(p.get("ok", True) for p in plist) if plist else True
            records.append({
                "role": m.get("role", "?"),
                "model": m.get("model", "?"),
                "provider": m.get("provider", "?"),
                "tier": m.get("tier", "T2"),
                "ok": ok,
                "route": "primary" if all((p.get("route", "primary") == "primary") for p in plist) else "fallback",
                "calls": len(plist) or 1,
            })
    else:
        # v3-style runs: phases are the members
        for p in provs:
            if isinstance(p, dict) and p.get("model"):
                records.append({
                    "role": p.get("phase", "?"),
                    "model": p.get("model", "?"),
                    "provider": p.get("provider", "?"),
                    "tier": "T1/T2",
                    "ok": p.get("ok", True),
                    "route": p.get("route", "primary"),
                    "calls": 1,
                })
    return records


def template_key(run):
    team = run.get("team") or {}
    tmpl = team.get("template")
    if tmpl:
        return tmpl
    # legacy: derive from plan phase models
    models = tuple(sorted({p.get("model", "?") for p in (run.get("plan") or {}).get("phases", [])}))
    return "legacy:" + "+".join(models) if models else "legacy:unknown"


def analyze(runs):
    if not runs:
        return {"status": "no_data"}

    templates = defaultdict(lambda: {"runs": 0, "members": defaultdict(lambda: {"calls": 0, "ok": 0, "fallbacks": 0}),
                                     "cost_metered": 0, "cost_subscription": 0, "audit_violations": 0})
    providers = defaultdict(lambda: {"calls": 0, "failures": 0, "fallbacks": 0})
    task_types = defaultdict(int)

    for run in runs:
        tt = run["task_type"]
        task_types[tt] += 1
        tk = template_key(run)
        tmpl = templates[tk]
        tmpl["runs"] += 1
        tmpl["audit_violations"] += int((run.get("team") or {}).get("audit_violations", 0) or 0)

        for rec in member_records(run):
            ms = tmpl["members"][rec["model"]]
            ms["calls"] += rec.get("calls", 1)
            ms["ok"] += 1 if rec["ok"] else 0
            ms["fallbacks"] += 1 if rec.get("route") != "primary" else 0
            ps = providers[rec["provider"]]
            ps["calls"] += rec.get("calls", 1)
            ps["failures"] += 0 if rec["ok"] else 1
            ps["fallbacks"] += 1 if rec.get("route") != "primary" else 0

        # cost split from summary markers (subscription vs metered)
        summary = run.get("summary", "")
        for line in summary.splitlines():
            if "metered" in line.lower():
                tmpl["cost_metered"] += 1
            elif "subscription" in line.lower() or "included" in line.lower():
                tmpl["cost_subscription"] += 1

    return {
        "status": "ok",
        "total_runs": len(runs),
        "task_types": dict(task_types),
        "templates": {k: {
            "runs": v["runs"],
            "audit_violations": v["audit_violations"],
            "cost_metered": v["cost_metered"],
            "cost_subscription": v["cost_subscription"],
            "members": {m: dict(s) for m, s in v["members"].items()},
        } for k, v in templates.items()},
        "providers": {k: dict(v) for k, v in providers.items()},
    }


def ctx_label(context_tokens):
    """Human label for a context size."""
    m = (context_tokens or 0) // 1000
    if m >= 1000:
        return f"{m // 1000}M context"
    return f"{m}K context"


def index_research_note(idx, capability_hint):
    """Find candidate models in the index matching a capability hint."""
    candidates = []
    for model, entry in (idx.get("models") or {}).items():
        meta = entry.get("metadata") or {}
        blob = json.dumps({"model": model, **meta}).lower()
        if capability_hint.lower() in blob:
            candidates.append({"model": model, "providers": entry.get("providers", []),
                               "context": meta.get("context"), "reasoning": meta.get("reasoning")})
    return candidates


def generate_report(analysis, idx, index_age_days, index_refreshed):
    if analysis.get("status") == "no_data":
        lines = [
            "# Fusion Feedback Report (v4)",
            "",
            "No Fusion runs in the trailing 7-day window.",
            "",
            f"- Model index: {index_age_days} days old" + (" — refreshed this run" if index_refreshed else ""),
            f"- Indexed models: {len(idx.get('models', {}))} across {len(idx.get('_configured', idx.get('configured_providers', {})))} providers",
            "",
            "No template changes recommended (insufficient data).",
        ]
        return "\n".join(lines)

    lines = [
        "# Fusion Feedback Report (v4)",
        "",
        f"**Total runs:** {analysis['total_runs']}",
        f"**Model index:** {index_age_days} days old" + (" (refreshed this run)" if index_refreshed else ""),
        "",
        "## Team Template Performance",
        "",
        "| Template | Runs | Audit violations | Metered | Subscription |",
        "|---|---|---|---|---|",
    ]
    for tk, ts in sorted(analysis["templates"].items()):
        lines.append(f"| {tk} | {ts['runs']} | {ts['audit_violations']} | {ts['cost_metered']} | {ts['cost_subscription']} |")

    lines += ["", "## Member Reliability", "", "| Model | Calls | OK | Fallbacks | Reliability |", "|---|---|---|---|---|"]
    for tk, ts in sorted(analysis["templates"].items()):
        for model, ms in sorted(ts["members"].items()):
            rel = f"{(ms['ok'] / max(ms['calls'], 1)) * 100:.0f}%"
            lines.append(f"| {model} ({tk}) | {ms['calls']} | {ms['ok']} | {ms['fallbacks']} | {rel} |")

    lines += ["", "## Recommendations", ""]
    recs = 0
    for tk, ts in sorted(analysis["templates"].items()):
        if ts["runs"] < MIN_RUNS_FOR_TEMPLATE_CHANGE:
            lines.append(f"- **{tk}**: {ts['runs']} run(s) — below the {MIN_RUNS_FOR_TEMPLATE_CHANGE}-run minimum for template changes.")
            continue
        for model, ms in sorted(ts["members"].items()):
            calls = ms["calls"]
            if calls and ms["ok"] / calls < 0.8:
                lines.append(f"- **{tk}** member `{model}`: reliability {(ms['ok']/calls)*100:.0f}% — consider same-model provider fallback as default, or a roster change.")
                recs += 1
        if ts["audit_violations"] > 0:
            lines.append(f"- **{tk}**: {ts['audit_violations']} MM prompt-audit violations — review delegation prompts before dispatch.")
            recs += 1

    # Open-minded model suggestions from the live index
    suggestions = index_research_note(idx, "reasoning")
    if suggestions:
        strong = [s for s in suggestions if (s.get("context") or 0) >= 1000000 and s.get("reasoning")]
        if strong:
            # Prefer a cheap configured candidate for the worked example (DeepSeek/GLM
            # family), falling back to the first listed.
            cheap_first = sorted(strong, key=lambda s: 0 if ("deepseek" in s["model"].lower() or "glm" in s["model"].lower()) else 1)
            example = cheap_first[0]
            ctx_m = (example.get("context") or 0) // 1000
            provs = ", ".join(example["providers"]) or "(no configured route)"
            lines += [
                "",
                "## Index Watch (open-minded candidates)",
                "",
                f"- Long-context reasoning models in the index: {', '.join(s['model'] for s in strong[:5])}.",
                f"- Example fit: complex subjects within bounded parameters — a model like **{example['model']}** "
                f"({ctx_label(example['context'])}, reasoning, cheap tier) may serve. "
                f"Configured route: {provs}. "
                "Research providers before adding to a roster; never silently swap.",
            ]

    if recs == 0:
        lines.append("- All tracked templates healthy. No changes needed.")
    return "\n".join(lines)


def main():
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    idx, index_age_days, refreshed = ensure_model_index()
    runs = collect_runs(since_days=7)
    analysis = analyze(runs)
    report = generate_report(analysis, idx, index_age_days, refreshed)

    report_path = REPORT_DIR / f"feedback-{datetime.now(timezone.utc).strftime('%Y%m%d')}.md"
    report_path.write_text(report, encoding="utf-8")
    print(f"Feedback report written to {report_path}")
    print(f"Runs analyzed: {analysis.get('total_runs', 0)}; templates: {len(analysis.get('templates', {}))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
