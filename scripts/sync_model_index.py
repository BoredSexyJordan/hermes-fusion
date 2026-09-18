#!/usr/bin/env python3
"""Sync the Fusion v4 machine model index.

Intersects the models.dev cache (~/.hermes/models_dev_cache.json) with the
providers actually configured in ~/.hermes/config.yaml, annotates with Jordan's
curated catalog notes, and writes ~/.hermes/fusion/model-index.json.

The index answers: "what models can we actually call today, and what are they
good at?" — the basis of open-minded weekly recommendations.

Usage: python3 sync_model_index.py [--config PATH] [--cache PATH] [--out PATH]
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".hermes" / "config.yaml"
DEFAULT_CACHE = Path.home() / ".hermes" / "models_dev_cache.json"
DEFAULT_OUT = Path.home() / ".hermes" / "fusion" / "model-index.json"
DEFAULT_CATALOG = Path.home() / "vault" / "System" / "References" / "model-catalog.md"
STALE_DAYS = 14


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}


def load_config_providers(config_path):
    """Extract provider entries from config.yaml via lightweight line parsing.

    Only reads the `providers:` block, capturing model/base_url/key_env and the
    models list. Never returns secret values (api_key fields are skipped).
    """
    import re
    providers = {}
    text = Path(config_path).read_text(encoding="utf-8") if Path(config_path).exists() else ""
    in_providers = False
    current = None
    in_models_list = False
    for line in text.splitlines():
        if re.match(r"^[A-Za-z_]", line):
            in_providers = line.startswith("providers:")
            if in_providers:
                continue
            # any other top-level key ends the providers block
            current = None
            in_models_list = False
            continue
        if not in_providers:
            continue
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            current = m.group(1)
            providers[current] = {}
            in_models_list = False
            continue
        if current is None:
            continue
        m2 = re.match(r"^\s{4}([A-Za-z0-9_-]+):\s*(.*)$", line)
        if m2:
            key, val = m2.group(1), m2.group(2).strip().strip("'\"")
            if key in ("api_key", "password", "secret"):
                continue  # never index secrets
            if key == "models" and val == "":
                in_models_list = True
                providers[current]["models"] = []
                continue
            providers[current][key] = val
            continue
        m3 = re.match(r"^\s{6,}-\s*(.+)$", line)
        if m3 and in_models_list:
            providers[current]["models"].append(m3.group(1).strip())
    return providers


def extract_configured_models(providers):
    """Return {model_id: [provider,...]} from provider model/model/models fields."""
    found = {}
    for pname, p in providers.items():
        if not isinstance(p, dict):
            continue
        models = []
        if p.get("model"):
            models.append(p["model"])
        mlist = p.get("models")
        if isinstance(mlist, list):
            models.extend(mlist)
        for m in models:
            found.setdefault(str(m), []).append(pname)
    return found


def parse_catalog_notes(catalog_path):
    """Pull per-model strength/weakness/policy notes from the human catalog."""
    notes = {}
    p = Path(catalog_path)
    if not p.exists():
        return notes
    current = None
    section = None
    for line in p.read_text(encoding="utf-8").splitlines():
        h = __import__("re").match(r"^#{2,3}\s+(.*)$", line)
        if h:
            title = h.group(1)
            # Reset model context on new heading; track named models
            if "(" in title:
                current = title.split("(")[0].strip()
            else:
                current = title.strip() if title.startswith("###") else current
            section = title
            continue
        if current is None:
            continue
        s = line.strip()
        if s.startswith("- ") or s.startswith("* "):
            item = s[2:].strip()
            bucket = "notes"
            low = item.lower()
            if s[2:4] in ("- ",) and ("strength" in section.lower() or "strength" in s.lower() or True) is False:
                pass
            if "weakness" in section.lower():
                bucket = "weaknesses"
            elif "strength" in section.lower():
                bucket = "strengths"
            elif "best role" in section.lower():
                bucket = "roles"
            notes.setdefault(current, {"strengths": [], "weaknesses": [], "roles": [], "notes": []})
            notes[current][bucket if bucket in ("strengths", "weaknesses", "roles") else "notes"].append(item)
    return notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--cache", default=str(DEFAULT_CACHE))
    ap.add_argument("--out", default=str(Path.home() / ".hermes" / "fusion" / "model-index.json"))
    ap.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    args = ap.parse_args()

    cache = json.loads(Path(args.cache).read_text(encoding="utf-8"))
    providers_cfg = load_config_providers(args.config)
    configured_models = extract_configured_models(providers_cfg)
    catalog = parse_catalog_notes(args.catalog)

    index = {
        "_generated": datetime.now(timezone.utc).isoformat(),
        "_source": "models.dev cache + config.yaml providers + curated catalog",
        "configured_providers": {k: {kk: vv for kk, vv in v.items() if kk not in ("api_key",)} for k, v in providers_cfg.items() if isinstance(v, dict)},
        "models": {},
    }

    # Map every configured model to its providers + any models.dev metadata
    for model, provs in sorted(configured_models.items()):
        entry = {"providers": provs, "catalog": None, "metadata": None}
        for prov, pv in cache.items():
            models = pv.get("models", {}) if isinstance(pv, dict) else {}
            if model in models:
                meta = models[model]
                entry["metadata"] = {
                    "name": meta.get("name"),
                    "reasoning": meta.get("reasoning"),
                    "context": (meta.get("limit") or {}).get("context"),
                    "provider_doc": pv.get("doc"),
                }
                break
        # attach curated notes by fuzzy family match
        fam = model.split("-")[0].lower()
        for cat_name, cat_notes in catalog.items():
            if fam and fam in cat_name.lower():
                entry["catalog"] = cat_notes
                break
        index["models"][model] = entry

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(index, indent=1), encoding="utf-8")
    print(f"model index written: {out} ({len(index['models'])} models, "
          f"{len(index['configured_providers'])} providers)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
