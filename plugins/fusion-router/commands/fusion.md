---
description: Route a task through the Fusion Frontier -> Middle Manager -> Frontier structure.
argument-hint: <task or goal>
---

Run the Fusion routing flow for the argument: `{{argument}}`.

1. **Scope (Middle Manager).** Restate the goal, the deliverables, and the stop
   conditions. Do not design the solution yet.
2. **Dispatch (Frontier worker).** Hand the goal + evidence to the
   `fusion-frontier-worker` subagent. Ask it to implement and verify in one
   continuous thread, then report findings briefly.
3. **Synthesize (Frontier consult).** When the worker returns, brief the
   `fusion-consult` subagent (clean context) with the worker's findings and any
   decision or open question. Let it verify and own final synthesis / escalation.

Constraints:
- Keep delegation prompts to goal + evidence + constraints only — never
  prescribe implementation (no "use this pattern", "style it like X").
- Escalate only real decisions (scope ambiguity, conflicting evidence,
  irreversible choices), never routine implementation stalls.
- Final output: the goal, what the worker built and verified, the consult's
  decision, and any remaining risks. Chain-of-trust: state which subagent
  produced what.
