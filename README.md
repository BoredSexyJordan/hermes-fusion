# Fusion Router

A multi-model routing plugin for Hermes Agent. It detects the model fleet your
agent harnesses actually have installed (Hermes, Codex, Claude, Grok) and builds
a **Frontier → Middle Manager → Frontier** routing structure: a cheap capable
middle manager plans and delegates; frontier models implement; a frontier model
synthesizes and owns decisions. Models are pinned per roster with provenance
from actual execution records.

No hardcoded provider fleet. The router reads your own install.

## Install

```bash
hermes plugins install fusion     # via the plugin catalog (once admitted)
hermes plugins enable fusion
```

Or from a local checkout:

```bash
hermes plugins install <path-to-this-repo>
hermes plugins enable fusion
```

## Commands

Run `hermes fusion --help` after enabling. Core surface:

```
hermes fusion teams                 # Detected fleet per harness + rosters
hermes fusion teams --harness codex # Restrict detection to one harness
hermes fusion teams --show default  # Full roster JSON for one template
hermes fusion validate <plan.json>  # Validate a plan packet (v3/v4)
hermes fusion plan <raw>            # Extract + validate a packet from raw output
hermes fusion run <plan.json>       # Execute through the middle-manager loop
hermes fusion index                 # Sync/query the model index
hermes fusion report                # Weekly feedback scorer report
hermes fusion status                # Recent runs
```

## How detection works

`./roster.py` + `./harnesses.py` scan each harness's native config for what it
can actually call today:

- **Hermes** — `config.yaml` `model:` blocks, default provider, fallback list
- **Codex** — `~/.codex/config.toml` (model, subagent model, model_providers)
- **Claude** — `~/.claude/settings.json` / `ANTHROPIC_API_KEY`
- **Grok / Grok Build / Grokbot** — `auth.json` `xai-oauth` / `XAI_API_KEY`

Each (provider, model) is tiered **frontier** / **cheap** by name heuristics
(`./classify.py`), then composed into rosters. Everything is overridable in
`HERMES_HOME/fusion.yaml` (see `fusion.example.yaml`).

## Structure

The routing spine is the same in every roster:

```
T0  Frontier consult  — final synthesis / decisions (clean session)
T1  Middle Manager    — cheapest capable model runs the loop, delegates
T2  Frontier workers  — implement in continuous cached threads
```

See `./scripts/` for the packet validator, normalizer, executor and header
builder — all packaged with the plugin so it is self-contained.

## License

MIT. See `LICENSE`.
