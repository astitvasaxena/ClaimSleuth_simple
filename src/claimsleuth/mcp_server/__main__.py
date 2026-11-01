"""Start the ClaimSleuth MCP server (Streamable HTTP).

    python -m claimsleuth.mcp_server [--host 127.0.0.1] [--port 8102] [--data-dir DIR] [--db FILE] [--log-file FILE]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from claimsleuth import config
from claimsleuth.mcp_server.server import MCP_PATH, build_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ClaimSleuth MCP server (Streamable HTTP transport)")
    parser.add_argument("--host", default=os.environ.get("CLAIMSLEUTH_MCP_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CLAIMSLEUTH_MCP_PORT", "8102")))
    parser.add_argument("--data-dir", type=Path, default=config.DATA_DIR)
    parser.add_argument("--db", type=Path, default=config.DB_PATH)
    parser.add_argument("--log-file", type=Path, default=config.RUNTIME_DIR / "logs" / "mcp_tool_calls.jsonl")
    parser.add_argument("--pid-file", type=Path, default=config.RUNTIME_DIR / "pids" / "mcp_server.pid")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server, log = build_server(args.data_dir, args.db, args.log_file)

    args.pid_file.parent.mkdir(parents=True, exist_ok=True)
    args.pid_file.write_text(str(os.getpid()), encoding="ascii")
    print(f"ClaimSleuth MCP server pid={os.getpid()} transport=streamable-http "
          f"url=http://{args.host}:{args.port}{MCP_PATH} health=http://{args.host}:{args.port}/health "
          f"tool_log={log.path}", file=sys.stderr, flush=True)
    try:
        server.run("streamable-http", host=args.host, port=args.port, streamable_http_path=MCP_PATH)
    finally:
        if args.pid_file.exists() and args.pid_file.read_text(encoding="ascii").strip() == str(os.getpid()):
            args.pid_file.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
