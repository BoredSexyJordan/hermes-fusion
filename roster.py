"""Detection + roster building shared by the `hermes fusion` CLI.

Pulls the user's installed model fleet from EVERY harness (hermes, codex,
claude, grok) via harnesses.py, tiers it, and constructs the canonical
routing STRUCTURE:

    Frontier (consult/synthesis) -> Middle Manager -> Frontier (workers)

No hardcoded fleet. Override any roster in HERMES_HOME/fusion.yaml.
"""
from __future__ import annotations

from paths import hermes_home
from classify import is_frontier
import harnesses

HARNESSES = ("hermes", "codex", "claude", "grok")


def detected_pool(home=None, harness: str = "all") -> list[dict]:
    """Configured (harness, provider, model) entries tagged with their tier."""
    return harnesses.detect_all(home or hermes_home(), harness=harness)


def build_rosters(home=None, harness: str = "all",
                  overrides: dict | None = None) -> dict[str, dict]:
    pool = detected_pool(home, harness=harness)
    frontier = [e for e in pool if e["frontier"]]
    cheap = [e for e in pool if not e["frontier"]]
    all_e = frontier + cheap

    def mm() -> dict:
        if cheap:
            e = cheap[0]
        elif all_e:
            e = all_e[-1]
        else:
            return {"model": "", "provider": "", "note": "no models detected"}
        return {"model": e["model"], "provider": e["provider"]}

    def workers(n: int) -> list[dict]:
        picked, seen = [], set()
        for e in frontier:
            key = (e["provider"], e["model"])
            if key in seen:
                continue
            seen.add(key)
            picked.append({
                "role": f"worker-{len(picked)+1}", "model": e["model"],
                "provider": e["provider"], "delegation": "delegate_task",
                "thread_policy": "continuous",
            })
            if len(picked) == n:
                break
        return picked

    def consult() -> list[dict]:
        if not frontier:
            return []
        e = frontier[0]
        return [{
            "role": "consult", "model": e["model"], "provider": e["provider"],
            "delegation": "clean_session", "trigger": "final synthesis / decision",
        }]

    rosters = {
        "default": {"task_type": "general", "middle_manager": mm(),
                     "members": workers(2), "consults": consult()},
        "technical": {"task_type": "technical", "middle_manager": mm(),
                       "members": workers(2), "consults": consult()},
        "strategy": {"task_type": "strategy", "middle_manager": mm(),
                      "members": workers(3), "consults": []},
        "operations": {"task_type": "operations", "middle_manager": mm(),
                        "members": workers(1), "consults": []},
        "single": {"task_type": "general", "middle_manager": mm(),
                    "members": [], "consults": []},
    }
    for name, tmpl in (overrides or {}).items():
        rosters[name] = tmpl
    return rosters


def detect_hint(home=None, harness: str = "all") -> str:
    pool = detected_pool(home, harness=harness)
    if not pool:
        return "none detected"
    return ", ".join(
        f"{e['harness']}:{e['provider']}:{e['model']}"
        f"{' (frontier)' if e['frontier'] else ' (cheap)'}" for e in pool
    )


def harness_breakdown(home=None) -> dict[str, list[dict]]:
    """Per-harness pool for the `teams` listing."""
    home = home or hermes_home()
    return {h: harnesses.detect_all(home, harness=h) for h in HARNESSES}
