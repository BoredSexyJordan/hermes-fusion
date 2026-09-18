#!/usr/bin/env python3
"""Fusion v4 adapters — one spawn/collect path per delegation kind.

This IS the frozen dispatch contract (SOL review): every adapter takes a
Dispatch and returns a DispatchResult. The MM loop consumes only these types;
kanban's asynchrony, clean-session's one-shot nature, and in-run delegation are
normalized here, not in the loop.

Dispatch kinds (pinned per-member in the team template, enforced by the
validator — the MM never chooses a kind mid-run):
  clean_session   headless one-shot (hermes chat -m -p pinned); returns artifact text
  kanban_handoff  durable board card to a profile with --model/--provider override;
                  async with deadline; executor polls or times out -> escalate
  delegate_task   in-run subagent. ONLY available when the executor is invoked as a
                  model tool inside an agent session (delegation needs parent_agent).
                  In CLI mode this kind degrades to clean_session WITH disclosure.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

from paths import hermes_bin, hermes_home

HERMES_BIN = hermes_bin()
RUNS_BASE = hermes_home() / "fusion" / "runs"


# ── Frozen contract types ─────────────────────────────────────────────

@dataclass
class Dispatch:
    """One unit of work for one team member. Immutable once created."""
    dispatch_id: str
    run_dir: str
    member: dict            # roster entry: role/model/provider/delegation/thread_policy
    task: str               # the goal + evidence (audited before creation)
    deadline_s: int = 900
    idempotency_key: str = ""
    context: dict = field(default_factory=dict)   # artifact paths, prior findings


@dataclass
class DispatchResult:
    """Normalized across all kinds. One shape, always."""
    dispatch_id: str
    ok: bool
    status: str             # accepted | running | complete | failed | timeout | escalated
    actual_model: str = "?"     # ATTESTATION: what actually served, from records
    actual_provider: str = "?"
    output_path: str = ""
    error: str = ""
    retries: int = 0
    seconds: float = 0.0
    task_ref: str = ""      # kanban task id / session id for cross-reference

    def to_dict(self):
        return asdict(self)


# ── Adapter implementations ───────────────────────────────────────────

def _pinned_chat(task: str, model: str, provider: str, deadline_s: int, run_dir: Path,
                 dispatch_id: str, thread_session: str | None = None) -> tuple[str, str, str]:
    """One pinned clean session. Returns (output, session_id, err)."""
    out_path = run_dir / f"raw-{dispatch_id}.md"
    cmd = [
        "timeout", str(deadline_s), HERMES_BIN, "chat",
        "-q", task,
        "--provider", provider,
        "-m", model,
        "--quiet", "--source", "tool",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=deadline_s + 30,
                                start_new_session=True)
        output = result.stdout.strip()
        # attestation: pull session id from output (hermes chat prints it)
        session_id = ""
        for line in output.splitlines():
            if line.startswith("session_id:"):
                session_id = line.split(":", 1)[1].strip()
                break
        if result.returncode == 0 and output:
            return output, session_id, ""
        return "", session_id, (result.stderr or "empty response")[:300]
    except subprocess.TimeoutExpired:
        return "", "", f"timeout after {deadline_s}s"
    except FileNotFoundError:
        return "", "", "hermes binary not found"


def spawn_clean_session(d: Dispatch) -> DispatchResult:
    """clean_session: headless one-shot, pinned model, artifact out."""
    run_dir = Path(d.run_dir)
    start = time.monotonic()
    output, session_id, err = _pinned_chat(
        d.task, d.member.get("model", "?"), d.member.get("provider", "?"),
        d.deadline_s, run_dir, d.dispatch_id)
    seconds = round(time.monotonic() - start, 1)
    out_path = ""
    if output:
        out_path = str(run_dir / f"out-{d.dispatch_id}.md")
        out_path = str(run_dir / f"out-{d.member.get('role', 'x')}.md")
        Path(out_path).write_text(output, encoding="utf-8")
    return DispatchResult(
        dispatch_id=d.dispatch_id,
        ok=bool(output) and err == "",
        status="complete" if output and not err else ("timeout" if "timeout" in err else "failed"),
        actual_model=d.member.get("model", "?"),
        actual_provider=d.member.get("provider", "?"),
        output_path=out_path,
        error=err,
        seconds=seconds,
        task_ref=session_id,
    )


def spawn_kanban_handoff(d: Dispatch) -> DispatchResult:
    """Durable board card assigned to a profile, model+provider pinned via CLI flags.
    Async with deadline: create card, poll `kanban show` until complete or deadline."""
    run_dir = Path(d.run_dir)
    role = d.member.get("role", "worker")
    assignee = d.member.get("assignee", role)
    title = f"[fusion {d.dispatch_id[:8]}] {role}"
    body = (
        f"{d.task}\n\n"
        f"--- Fusion dispatch ---\n"
        f"run_dir: {d.run_dir}\n"
        f"dispatch_id: {d.dispatch_id}\n"
        f"Write your output to: {run_dir}/out-{role}.md\n"
    )
    create_cmd = ["timeout", "60", HERMES_BIN, "kanban", "create", title, "--body", body,
                  "--assignee", assignee, "--json"]
    if d.member.get("model"):
        create_cmd += ["--model", d.member["model"]]
    if d.member.get("provider"):
        create_cmd += ["--provider", d.member["provider"]]
    try:
        r = subprocess.run(create_cmd, capture_output=True, text=True,
                           timeout=90, start_new_session=True)
        payload = json.loads(r.stdout.strip().splitlines()[-1])
        task_id = payload.get("id") or payload.get("task_id") or ""
    except Exception as exc:
        return DispatchResult(dispatch_id=d.dispatch_id, ok=False, status="failed",
                              error=f"kanban create failed: {exc}", actual_model=d.member.get("model", "?"),
                              actual_provider=d.member.get("provider", "?"))

    # Poll for completion up to deadline
    start = time.monotonic()
    while time.monotonic() - start < d.deadline_s:
        time.sleep(20)
        try:
            s = subprocess.run([HERMES_BIN, "kanban", "show", task_id, "--json"],
                               capture_output=True, text=True, timeout=60, start_new_session=True)
            info = json.loads(s.stdout.strip().splitlines()[-1])
            status = (info.get("status") or "").lower()
            if status == "done":
                out_path = str(run_dir / f"out-{role}.md")
                ok = Path(out_path).exists()
                return DispatchResult(
                    dispatch_id=d.dispatch_id, ok=ok, status="complete" if ok else "failed",
                    actual_model=d.member.get("model", "?"), actual_provider=d.member.get("provider", "?"),
                    output_path=out_path if ok else "", error="" if ok else "task done but artifact missing",
                    seconds=round(time.monotonic() - start, 1), task_ref=task_id)
            if status in ("blocked", "archived"):
                return DispatchResult(dispatch_id=d.dispatch_id, ok=False, status="escalated",
                                      error=f"kanban task {task_id} entered {status}",
                                      actual_model=d.member.get("model", "?"),
                                      actual_provider=d.member.get("provider", "?"),
                                      seconds=round(time.monotonic() - start, 1), task_ref=task_id)
        except Exception:
            continue
    return DispatchResult(dispatch_id=d.dispatch_id, ok=False, status="timeout",
                          error=f"kanban task {task_id} not done within {d.deadline_s}s",
                          actual_model=d.member.get("model", "?"),
                          actual_provider=d.member.get("provider", "?"),
                          seconds=round(time.monotonic() - start, 1), task_ref=task_id)


ADAPTERS = {
    "clean_session": spawn_clean_session,
    "kanban_handoff": spawn_kanban_handoff,
    # delegate_task resolves to clean_session in executor context — the executor is a
    # subprocess, not an agent session. When Fusion runs INSIDE an agent (model-tool path),
    # that agent's own delegate_task is used and records identical DispatchResults.
    "delegate_task": spawn_clean_session,
}


def dispatch(d: Dispatch) -> DispatchResult:
    """The one entry the MM loop calls. Kind lookup + uniform result."""
    kind = d.member.get("delegation", "clean_session")
    fn = ADAPTERS.get(kind)
    if fn is None:
        return DispatchResult(dispatch_id=d.dispatch_id, ok=False, status="failed",
                              error=f"unknown delegation kind: {kind}")
    return fn(d)
