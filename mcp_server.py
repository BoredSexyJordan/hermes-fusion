"""Fusion router as an MCP server (stdio + HTTP).

Exposes the harness-agnostic router to ANY MCP host — Claude Code, Grok Build,
Codex, and other agents all speak MCP. Tools:

  fusion_teams  -> detected fleet + Frontier->MM->Frontier rosters
  fusion_route  -> the roster/team for a given task type (auto-detected fleet)
  fusion_status -> recent run state

Run standalone:
  python3 mcp_server.py                         # stdio
  FUSION_TOKEN=secret python3 mcp_server.py --http --port 8765   # HTTP/HTTPS
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import roster

from .paths import hermes_home

TOKEN = os.environ.get("FUSION_TOKEN", "")
HOST = os.environ.get("FUSION_HOST", "0.0.0.0")
PORT = int(os.environ.get("FUSION_PORT", "8765"))


# ── Router tools (host-agnostic) ──────────────────────────────────────

def _teams(harness: str = "all") -> dict:
    return {
        "harness": harness,
        "pool": roster.detected_pool(harness=harness),
        "rosters": roster.build_rosters(harness=harness),
    }


def _route(task: str, task_type: str | None = None, harness: str = "all") -> dict:
    rosters = roster.build_rosters(harness=harness)
    key = task_type or "default"
    team = rosters.get(key) or rosters["default"]
    return {
        "task": task,
        "routed_roster": team,
        "structure": "Frontier -> Middle Manager -> Frontier",
        "note": "actual dispatch needs the harness's subagent/agent spawn; this is the roster selection.",
    }


def _status(limit: int = 5) -> dict:
    runs = []
    base = hermes_home() / "fusion" / "runs"
    if base.exists():
        for d in sorted(base.iterdir(), reverse=True)[:limit]:
            if d.is_dir():
                runs.append({"run": d.name, "dir": str(d)})
    return {"runs": runs}


TOOLS = {
    "fusion_teams": {
        "description": "Detected model fleet + Frontier->MM->Frontier rosters.",
        "params": {"harness": {"type": "string", "enum": ["all", "hermes", "codex", "claude", "grok"], "default": "all"}},
    },
    "fusion_route": {
        "description": "Choose the roster/team to route a task to.",
        "params": {
            "task": {"type": "string", "description": "The task or goal."},
            "task_type": {"type": "string", "enum": ["default", "technical", "strategy", "operations", "single"], "default": None},
            "harness": {"type": "string", "enum": ["all", "hermes", "codex", "claude", "grok"], "default": "all"},
        },
    },
    "fusion_status": {
        "description": "Recent fusion run state.",
        "params": {"limit": {"type": "integer", "default": 5}},
    },
}


def _call(name: str, args: dict) -> str:
    if name == "fusion_teams":
        out = _teams(args.get("harness", "all"))
    elif name == "fusion_route":
        out = _route(args.get("task", ""), args.get("task_type"), args.get("harness", "all"))
    elif name == "fusion_status":
        out = _status(args.get("limit", 5))
    else:
        out = {"error": f"unknown tool {name}"}
    return json.dumps(out, indent=2)


# ── MCP framing (minimal JSON-RPC 2.0) ────────────────────────────────

def _tools_payload() -> dict:
    return {
        "jsonrpc": "2.0", "id": "tools",
        "result": {
            "tools": [
                {"name": n, "description": t["description"],
                 "inputSchema": {"type": "object", "properties": t["params"]}}
                for n, t in TOOLS.items()
            ],
        },
    }


def _handle(payload: dict) -> dict:
    method = payload.get("method", "")
    rid = payload.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
            "serverInfo": {"name": "fusion-router", "version": "0.1.0"}}}
    if method == "tools/list":
        return _tools_payload() if rid is not None else payload
    if method == "tools/call":
        p = payload.get("params", {})
        name = p.get("name", "")
        args = p.get("arguments", {}) or {}
        result = _call(name, args)
        return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": result}]}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "method not found"}}


def serve_stdio() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = _handle(payload)
        if resp.get("id") is not None or "error" in resp:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()


def _auth_ok(headers: dict) -> bool:
    if not TOKEN:
        return True
    return headers.get("authorization") == f"Bearer {TOKEN}"


def serve_http(host: str = HOST, port: int = PORT) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _read(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def _send(self, status: int, obj: dict):
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                return self._send(200, {"ok": True})
            if self.path == "/tools":
                return self._send(200, _tools_payload()["result"])
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not _auth_ok(self.headers):
                return self._send(401, {"error": "unauthorized"})
            if self.path != "/mcp":
                return self._send(404, {"error": "not found"})
            try:
                payload = self._read()
            except Exception:
                return self._send(400, {"error": "bad json"})
            is_batch = isinstance(payload, list)
            payloads = payload if is_batch else [payload]
            responses = [_handle(p) for p in payloads]
            responses = [r for r in responses if r.get("id") is not None]
            return self._send(200, responses if is_batch else (responses[0] if responses else {}))

    print(f"fusion MCP listening on {host}:{port} (auth={'enabled' if TOKEN else 'OFF'})", file=sys.stderr)
    ThreadingHTTPServer((host, port), Handler).serve_forever()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--http", action="store_true", help="Serve over HTTP instead of stdio")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--host", default=HOST)
    args = ap.parse_args()
    if args.http:
        serve_http(host=args.host, port=args.port)
    else:
        serve_stdio()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
