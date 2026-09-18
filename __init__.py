"""Fusion v4 plugin — middle-manager orchestration with subagent/bot teams.

Registers the operator CLI (`hermes fusion ...`). The MM loop, validator,
feedback scorer, and model index live in this package's modules plus the
proven skill scripts; the skill (fusion-orchestration) is the agent-facing
playbook. This plugin adds the durable CLI + hooks surface.
"""

from __future__ import annotations

from pathlib import Path

from .cli import fusion_command, setup_cli

PLUGIN_DIR = Path(__file__).parent
SKILL_SCRIPTS = Path.home() / ".hermes" / "skills" / "autonomous-ai-agents" / "fusion-orchestration" / "scripts"


def register(ctx) -> None:
    ctx.register_cli_command(
        name="fusion",
        help="Fusion v4 orchestration: plan, run, validate, teams, index, report",
        setup_fn=setup_cli,
        handler_fn=fusion_command,
        description=(
            "Operator CLI for Fusion v4. Validates plan packets (v3+v4), shows the "
            "team-template registry, syncs/queries the living model index, runs the "
            "middle-manager loop over a packet, and prints feedback reports."
        ),
    )
