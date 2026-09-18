---
name: fusion-router
description: Multi-model fusion routing. Use when a task should be routed through a cheap Middle Manager and Frontier worker(s) with a Frontier consult, following the Frontier -> Middle Manager -> Frontier structure.
metadata:
  author: Jordan Rosenberg
  short-description: Frontier -> Middle Manager -> Frontier routing
---

# Fusion Router (Codex)

Route substantial work through three layers instead of doing it in one flat
thread:

```
Frontier (consult/decision)  ->  Middle Manager (cheap orchestrator)  ->  Frontier (worker)
```

When the hosted fusion MCP server is available, call its tools to get the
detected fleet and the routed roster for a task:

- `fusion_teams` — the model fleet Codex/Hermes/Claude/Grok can call, tiered
  frontier / cheap, and the Frontier->MM->Frontier rosters.
- `fusion_route task task_type` — the roster to route a task to.
- `fusion_status` — recent run state.

Fall back to the structural doctrine directly when the MCP endpoint is not
reachable:

## Roles

- **Middle Manager**: reads the task, plans phases, delegates with goal +
  evidence only, enforces stop conditions.
- **Frontier Worker**: implements in one continuous thread, verifies with real
  execution, reports a short findings recap.
- **Frontier Consult**: final synthesis, verification, and decisions on scope
  ambiguity / conflicting evidence / irreversible moves.

## Doctrine

- Delegation prompts = goal + evidence + constraints. Never prescribe HOW.
- The Middle Manager never implements; the Worker never plans endlessly; the
  Consult never babysits.
- Escalate only real decisions. Never bypass an unresolved audit.
- Provenance: record which layer produced which output.
