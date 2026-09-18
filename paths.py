"""Shared, portable path resolution for the fusion plugin.

Runs inside Hermes, so paths must follow HERMES_HOME (profile-aware) rather
than a hardcoded home. The plugin never knows Jordan's machine layout.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def hermes_home() -> Path:
    """HERMES_HOME if set, else ~/.hermes (matches Hermes' own default)."""
    ev = os.environ.get("HERMES_HOME")
    if ev:
        return Path(ev)
    return Path.home() / ".hermes"


def hermes_python() -> str:
    """Python interpreter for subprocess script calls (honor HERMES_PYTHON)."""
    return os.environ.get("HERMES_PYTHON") or sys.executable or "python3"


def hermes_bin() -> str:
    """Locate the `hermes` executable, honoring HERMES_BIN/HERMES_HOME.

    Order: explicit HERMES_BIN env -> `hermes` on PATH -> the usual venv
    locations under HERMES_HOME -> bare 'hermes' (let PATH try).
    """
    ev = os.environ.get("HERMES_BIN")
    if ev:
        return ev
    found = shutil.which("hermes")
    if found:
        return found
    base = hermes_home()
    for cand in (
        base / "hermes-agent" / "venv" / "bin" / "hermes",
        base / "hermes-agent" / ".venv" / "bin" / "hermes",
        base / "bin" / "hermes",
    ):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return "hermes"
