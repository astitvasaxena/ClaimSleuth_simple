"""Readiness of the two local services the investigation needs, and an optional launcher for them.

Status values: RUNNING (health check passed), UNHEALTHY (answers but a check failed), STOPPED (nothing listening).
Every status comes from a real request made now.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from claimsleuth import config

MAIN_PYTHON = config.PROJECT_ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
CREW_PYTHON = config.PROJECT_ROOT / ".venv-crew" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


@dataclass
class ServiceConfig:
    mcp_port: int = 8102
    crew_port: int = 8103

    @property
    def mcp_url(self) -> str:
        return f"http://127.0.0.1:{self.mcp_port}/mcp"

    @property
    def crew_base(self) -> str:
        return f"http://127.0.0.1:{self.crew_port}"

    @property
    def crew_card(self) -> str:
        return f"{self.crew_base}/.well-known/agent-card.json"


def get_json(url: str, timeout: float = 5.0) -> tuple[int | None, Any]:
    """(HTTP status, JSON body); status None when nothing answers."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"null")
        except json.JSONDecodeError:
            return exc.code, None
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None, None


def _status(code: int | None, body: Any) -> str:
    if code is None:
        return "STOPPED"
    return "RUNNING" if code == 200 and isinstance(body, dict) and body.get("status") == "ok" else "UNHEALTHY"


def check_mcp(cfg: ServiceConfig, data_dir: Path) -> dict[str, Any]:
    code, body = get_json(f"http://127.0.0.1:{cfg.mcp_port}/health")
    out = {"name": "MCP server", "framework": "MCP Python SDK (mcp.server.MCPServer)", "url": cfg.mcp_url,
           "status": _status(code, body), "detail": body}
    if isinstance(body, dict) and body.get("data_dir") and Path(body["data_dir"]) != data_dir.resolve():
        out["warning"] = "serves a different data directory than the dashboard workspace; uploaded claims are unknown to it"
    return out


def check_crew(cfg: ServiceConfig, data_dir: Path) -> dict[str, Any]:
    code, body = get_json(f"{cfg.crew_base}/health", timeout=20)
    out = {"name": "CrewAI A2A service", "framework": "CrewAI + a2a-sdk (JSON-RPC)", "url": cfg.crew_card,
           "status": _status(code, body), "detail": body}
    llm = (body or {}).get("checks", {}).get("llm", {}) if isinstance(body, dict) else {}
    out["llm_mode"] = llm.get("mode")
    out["model"] = llm.get("model")
    if isinstance(body, dict) and body.get("data_dir") and Path(body["data_dir"]) != data_dir.resolve():
        out["warning"] = "serves a different data directory than the dashboard workspace; uploaded claims are unknown to it"
    return out


class ServiceLauncher:
    """Starts the MCP server (.venv) and the CrewAI A2A service (.venv-crew) on the workspace data directory
    when nothing is listening yet, and stops only the processes it started."""

    def __init__(self, cfg: ServiceConfig, data_dir: Path, crew_mode: str = "test-double") -> None:
        self.cfg, self.data_dir, self.crew_mode = cfg, data_dir, crew_mode
        self.procs: list[tuple[str, subprocess.Popen, Any]] = []

    def _spawn(self, name: str, cmd: list[str], env: dict[str, str]) -> None:
        log = (config.RUNTIME_DIR / "logs" / f"dashboard_{name}.log")
        log.parent.mkdir(parents=True, exist_ok=True)
        handle = log.open("a", encoding="utf-8")
        proc = subprocess.Popen(cmd, cwd=config.PROJECT_ROOT, stdout=handle, stderr=subprocess.STDOUT,
                                env={**os.environ, "PYTHONIOENCODING": "utf-8", **env})
        self.procs.append((name, proc, handle))

    def _wait(self, url: str, name: str, seconds: float) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            code, body = get_json(url, timeout=10)
            if code == 200:
                return True
            proc = next((p for n, p, _ in self.procs if n == name), None)
            if proc is not None and proc.poll() is not None:
                return False
            time.sleep(0.5)
        return False

    def start(self, log=print) -> None:
        if get_json(f"http://127.0.0.1:{self.cfg.mcp_port}/health")[0] is None:
            self._spawn("mcp_server", [str(MAIN_PYTHON), "-m", "claimsleuth.mcp_server", "--port", str(self.cfg.mcp_port),
                                       "--data-dir", str(self.data_dir), "--db", str(config.DB_PATH)], {})
            ok = self._wait(f"http://127.0.0.1:{self.cfg.mcp_port}/health", "mcp_server", 60)
            log(f"MCP server on {self.cfg.mcp_url}: {'started' if ok else 'FAILED to start (runtime/logs/dashboard_mcp_server.log)'}")
        else:
            log(f"MCP server already listening on port {self.cfg.mcp_port}; using it as is")
        if get_json(f"{self.cfg.crew_base}/health", timeout=20)[0] is None:
            if not CREW_PYTHON.exists():
                log(f"CrewAI environment not found at {CREW_PYTHON}; the CrewAI A2A service is not started")
                return
            self._spawn("crew_a2a", [str(CREW_PYTHON), "-m", "claimsleuth.crew_service.a2a_server",
                                     "--port", str(self.cfg.crew_port), "--mcp-url", self.cfg.mcp_url,
                                     "--data-dir", str(self.data_dir)], {"CLAIMSLEUTH_CREW_LLM": self.crew_mode})
            ok = self._wait(f"{self.cfg.crew_base}/health", "crew_a2a", 120)
            log(f"CrewAI A2A service on {self.cfg.crew_base} (llm mode {self.crew_mode}): "
                f"{'started' if ok else 'not healthy yet (runtime/logs/dashboard_crew_a2a.log)'}")
        else:
            log(f"CrewAI A2A service already listening on port {self.cfg.crew_port}; using it as is")

    def stop(self) -> None:
        for _, proc, handle in reversed(self.procs):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
            handle.close()
        self.procs.clear()
