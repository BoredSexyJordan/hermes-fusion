"""Per-harness model detection: what does THIS agent actually have installed?

Each harness reads its own native config and returns the configured
(provider, model) pairs it can call today. `detect_all()` merges them so the
roster builds the Frontier -> Middle Manager -> Frontier structure from the
fleet available across every harness Jordan runs.

Detection is best-effort and never crashes on a missing/unparseable config.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from .classify import is_frontier
from .paths import hermes_home


def _home_dir() -> Path:
    return Path.home()


# ── Hermes ─────────────────────────────────────────────────────────────

def detect_hermes(home: Path | None = None) -> list[dict]:
    home = home or hermes_home()
    cfg = {}
    try:
        import yaml
        p = home / "config.yaml"
        if p.exists():
            cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        cfg = {}

    out = []
    model_sec = cfg.get("model") or {}
    for prov, blk in model_sec.items():
        if isinstance(blk, dict):
            m = blk.get("model") or blk.get("default_model")
            if m and isinstance(m, str) and m.strip():
                out.append(_mk("hermes", prov, m.strip()))
    dm = model_sec.get("default")
    dp = model_sec.get("provider") or model_sec.get("default_provider")
    if dm and dp:
        out.append(_mk("hermes", dp, dm))
    for fb in model_sec.get("fallback_providers") or []:
        if isinstance(fb, dict) and fb.get("provider") and fb.get("model"):
            out.append(_mk("hermes", fb["provider"], fb["model"]))
    return out


# ── Codex ──────────────────────────────────────────────────────────────

def detect_codex(codex_home: Path | None = None) -> list[dict]:
    root = codex_home or (_home_dir() / ".codex")
    cfg = {}
    try:
        import tomllib
        p = root / "config.toml"
        if p.exists():
            with p.open("rb") as fh:
                cfg = tomllib.load(fh)
    except Exception:
        cfg = {}

    out = []
    def add(prov, model):
        if model and isinstance(model, str) and model.strip():
            out.append(_mk("codex", prov or "openai", model.strip()))
    add(cfg.get("model_provider"), cfg.get("model"))
    add(cfg.get("model_provider"), cfg.get("default_subagent_model"))
    for prov, blk in (cfg.get("model_providers") or {}).items():
        if isinstance(blk, dict):
            add(prov, blk.get("model") or blk.get("default_model"))
    return out


# ── Claude ─────────────────────────────────────────────────────────────

def detect_claude() -> list[dict]:
    out = []
    # settings.json model (per-project or global)
    p = _home_dir() / ".claude" / "settings.json"
    model = ""
    try:
        if p.exists():
            import json
            model = (json.loads(p.read_text(encoding="utf-8")) or {}).get("model") or ""
    except Exception:
        model = ""
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if model:
        out.append(_mk("claude", "anthropic", model))
    elif has_key:
        # no explicit model -> assume the standard frontier/cheap pairing
        out.append(_mk("claude", "anthropic", "claude-opus-4-8"))
        out.append(_mk("claude", "anthropic", "claude-haiku-4-5"))
    return out


# ── Grok / Grokbot / Grok Build ───────────────────────────────────────

def detect_grok(home: Path | None = None) -> list[dict]:
    home = home or hermes_home()
    out = []
    # xai-oauth credential present in Hermes auth.json
    try:
        import json
        auth = json.loads((home / "auth.json").read_text(encoding="utf-8")) or {}
        provs = auth.get("providers") or {}
        if "xai-oauth" in provs:
            out.append(_mk("grok", "xai", "grok-4.6"))
            out.append(_mk("grok", "xai", "grok-4.1-fast"))
    except Exception:
        pass
    if os.environ.get("XAI_API_KEY"):
        out.append(_mk("grok", "xai", "grok-4.6"))
    return out


# ── aggregation ───────────────────────────────────────────────────────

def _mk(harness: str, provider: str, model: str) -> dict:
    return {
        "harness": harness,
        "provider": provider,
        "model": model,
        "frontier": is_frontier(provider, model),
    }


def detect_all(home: Path | None = None, harness: str = "all") -> list[dict]:
    home = home or hermes_home()
    by_name = {
        "hermes": detect_hermes(home),
        "codex": detect_codex(),
        "claude": detect_claude(),
        "grok": detect_grok(home),
    }
    if harness in by_name:
        return by_name[harness]
    # 'all': merge, dedupe on (harness, provider, model), keep frontier flag
    seen, out = set(), []
    for lst in by_name.values():
        for e in lst:
            key = (e["harness"], e["provider"], e["model"])
            if key in seen:
                continue
            seen.add(key)
            out.append(e)
    out.sort(key=lambda e: (not e["frontier"], e["harness"]))
    return out
