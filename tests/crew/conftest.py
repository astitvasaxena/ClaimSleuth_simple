"""Fixtures for the CrewAI tests (run them with .venv-crew; the main .venv skips them).

The MCP server is started with the *main* environment's Python (mcp 2.3.0), exactly as in production;
the CrewAI agent talks to it from .venv-crew (mcp 1.30.0) over Streamable HTTP.
"""
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAIN_PYTHON = ROOT / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class McpServerProcess:
    def __init__(self, data_dir: Path, db: Path, tmp: Path) -> None:
        if not MAIN_PYTHON.exists():
            pytest.skip(f"main environment not found at {MAIN_PYTHON}")
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self.log_file = tmp / f"tool_calls_{self.port}.jsonl"
        self.out = (tmp / f"mcp_{self.port}.log").open("w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [str(MAIN_PYTHON), "-m", "claimsleuth.mcp_server", "--port", str(self.port), "--data-dir", str(data_dir),
             "--db", str(db), "--log-file", str(self.log_file), "--pid-file", str(tmp / f"{self.port}.pid")],
            cwd=ROOT, stdout=self.out, stderr=subprocess.STDOUT)
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/health", timeout=2):
                    return
            except (urllib.error.URLError, OSError):
                if self.proc.poll() is not None:
                    raise RuntimeError(f"MCP server exited, see {self.out.name}")
                time.sleep(0.2)
        raise RuntimeError("MCP server did not start")

    def log_records(self) -> list[dict]:
        if not self.log_file.exists():
            return []
        return [json.loads(x) for x in self.log_file.read_text(encoding="utf-8").splitlines()]

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=10)
        self.out.close()


@pytest.fixture(scope="module")
def mcp(dataset, tmp_path_factory):
    server = McpServerProcess(*dataset, tmp_path_factory.mktemp("crew_mcp"))
    yield server
    server.stop()
