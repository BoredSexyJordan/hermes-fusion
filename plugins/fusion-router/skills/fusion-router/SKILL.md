---
name: fusion-router
description: Multi-model fusion routing. Use when a task should be routed through a cheap Middle Manager and Frontier worker(s) with a Frontier consult, following the Frontier -> Middle Manager -> Frontier structure.
---

# Fusion Router

Route substantial work through three layers instead of doing it in one flat
thread:

```
Frontier (consult/decision)  ->  Middle Manager (cheap orchestrator)  ->  Frontier (worker)
```

## When to use

- A task is big enough to benefit from a plan-then-implement split.
- Quality matters and you want a strong implementation pass plus an independent
  synthesis/verification pass.
- You want cost discipline: a cheap model plans and coordinates; the frontier
  model does the hard implementation; a frontier model owns the final call.

## Roles

- **Middle Manager** (`fusion-middle-manager`): reads the task, plans phases,
  delegates to the worker with goal + evidence only, enforces stop conditions.
- **Frontier Worker** (`fusion-frontier-worker`): implements in one continuous
  thread, verifies with real execution, reports a short findings recap.
- **Frontier Consult** (`fusion-consult`): final synthesis, verification, and
  decisions on scope ambiguity / conflicting evidence / irreversible moves.

## Doctrine

- Delegation prompts = goal + evidence + constraints. Never prescribe HOW
  (no style specs, no "use the pattern", no "make sure you").
- The Middle Manager never implements; the Worker never plans endlessly; the
  Consult never babysits.
- Escalate only real decisions. Never bypass an unresolved audit by dispatching
  anyway — resolve or escalate.
- Provenance: record which agent produced which output so the final answer
  carries a chain of trust.

## Invocation

- Slash command: `/fusion <task>` for an interactive routed run.
- Or invoke the subagents directly: brief `fusion-middle-manager`, then
  `fusion-frontier-worker`, then `fusion-consult`.
