"""Independent MCP client check for the ClaimSleuth MCP server.

Talks to a *running* server only through the MCP protocol (Streamable HTTP) and its /health and
/tool-calls HTTP routes. It never imports the server or the tool functions; it imports the shared
Pydantic output models only to validate what comes back over the wire.

    python scripts/mcp_client_check.py [--url http://127.0.0.1:8102/mcp] [--run-id RUN-...] [--quiet]

Exit code 0 only if every check passes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request
import uuid
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from mcp import Client
from pydantic import BaseModel, ValidationError

from claimsleuth.schemas import ClaimHistoryLookupOutput, PolicySearchOutput

REQUIRED = {"warranty_policy_search", "claim_history_lookup"}
RUN_ID_META_KEY = "claimsleuth/run_id"


class Checker:
    def __init__(self, quiet: bool) -> None:
        self.quiet, self.passed, self.failed = quiet, 0, 0

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        self.passed += ok
        self.failed += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" - {detail}" if detail else ""))
        return ok

    def show(self, title: str, payload: Any) -> None:
        if not self.quiet:
            print(f"      {title}:")
            print("      " + json.dumps(payload, indent=2, default=str).replace("\n", "\n      "))


def _base(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _get_json(url: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


async def run(url: str, run_id: str, quiet: bool) -> int:
    c = Checker(quiet)
    base = _base(url)
    print(f"MCP server: {url}\nRun id:     {run_id}\n")

    # 0. health (plain HTTP route served by the same process)
    status, health = _get_json(f"{base}/health")
    c.check("health endpoint reports ok", status == 200 and health.get("status") == "ok",
            f"HTTP {status}, sdk {health.get('mcp_sdk_version')}, transport {health.get('transport')}")

    expected_calls: list[tuple[str, str]] = []          # (tool, "ok"|"error") in call order

    async with Client(url) as client:
        # 1-3. discovery over MCP
        listed = await client.list_tools()
        names = {t.name for t in listed.tools}
        c.check("connected and listed tools over MCP", bool(listed.tools), ", ".join(sorted(names)))
        c.check("both required tools are discovered", REQUIRED <= names)
        c.check("no extra tools are exposed", names == REQUIRED, f"{len(names)} tool(s)")
        for t in listed.tools:
            c.show(f"tool {t.name} input schema properties", t.input_schema.get("properties"))

        async def call(tool: str, args: dict[str, Any]):
            result = await client.call_tool(tool, args, meta={RUN_ID_META_KEY: run_id})
            expected_calls.append((tool, "error" if result.is_error else "ok"))
            return result

        def valid(label: str, result, model: type[BaseModel], expect_matches: bool):
            if not c.check(f"{label}: call succeeded", not result.is_error,
                           "" if not result.is_error else result.content[0].text):
                return None
            try:
                parsed = model.model_validate(result.structured_content)
            except ValidationError as exc:
                c.check(f"{label}: structured output validates", False, str(exc).splitlines()[0])
                return None
            c.check(f"{label}: structured output validates as {model.__name__}", True)
            c.check(f"{label}: {'has' if expect_matches else 'has no'} matches",
                    (parsed.total_matches > 0) == expect_matches, f"total_matches={parsed.total_matches}")
            c.show(f"{label} result", result.structured_content)
            return parsed

        # 4. warranty_policy_search
        r = valid("policy search 'water damage' on laptops",
                  await call("warranty_policy_search", {"search_query": "water damage laptop"}),
                  PolicySearchOutput, True)
        if r:
            top = r.matches[0]
            c.check("top match is POL-LAPTOP-STD-02 with exclusion 'water damage'",
                    top.policy_id == "POL-LAPTOP-STD-02" and "water damage" in top.terms.exclusions,
                    f"sources {[t.source for t in top.matched_terms]}")
        r = valid("policy search by policy_id", await call("warranty_policy_search", {"policy_id": "POL-APPLIANCE-PLUS-03"}),
                  PolicySearchOutput, True)
        if r:
            terms = r.matches[0].terms
            c.check("policy terms come from the register",
                    (str(terms.deductible), str(terms.max_coverage_limit), terms.duration_months) == ("5000.00", "60000.00", 36))
        valid("policy search with no match", await call("warranty_policy_search", {"search_query": "spaceship"}),
              PolicySearchOutput, False)

        # 5. claim_history_lookup
        r = valid("history by serial", await call("claim_history_lookup", {"serial_number": "NMB-X15-551230"}),
                  ClaimHistoryLookupOutput, True)
        if r:
            c.check("record HST-2025-1102 (claimant USR-3305, APPROVED) returned",
                    [(x.claim_id, x.claimant_id, x.outcome) for x in r.records] == [("HST-2025-1102", "USR-3305", "APPROVED")],
                    r.record_sources[0])
        r = valid("history by claimant with limit", await call("claim_history_lookup", {"claimant_id": "USR-6610", "limit": 1}),
                  ClaimHistoryLookupOutput, True)
        valid("history with no records", await call("claim_history_lookup", {"claim_id": "CS-007"}),
              ClaimHistoryLookupOutput, False)

        # 7-8. invalid input must come back as MCP tool errors, never as data
        invalid = [
            ("warranty_policy_search", {}, "no filter"),
            ("warranty_policy_search", {"policy_id": "bad id!"}, "bad policy_id pattern"),
            ("warranty_policy_search", {"search_query": "water", "limit": 99}, "limit out of range"),
            ("warranty_policy_search", {"search_query": "the of and"}, "only stopwords"),
            ("claim_history_lookup", {"serial_number": "x' OR '1'='1"}, "SQL-injection-shaped serial"),
            ("claim_history_lookup", {"claimant_id": "USR-6610", "limit": 0}, "limit zero"),
            ("claim_history_lookup", {"claimant_id": 42}, "wrong type"),
            ("claim_history_lookup", {}, "no filter"),
        ]
        for tool, args, why in invalid:
            result = await call(tool, args)
            message = result.content[0].text if result.content else ""
            c.check(f"invalid input rejected ({tool}: {why})",
                    result.is_error and not result.structured_content, message.splitlines()[0][:110])

        result = await call("delete_everything", {})
        c.check("unknown tool rejected", result.is_error, result.content[0].text if result.content else "")

    # 9. the server logged exactly these calls under this run id
    status, body = _get_json(f"{base}/tool-calls?run_id={run_id}&limit=500")
    logged = [(x["tool"], x["status"]) for x in body.get("calls", [])]
    c.check("server log has one record per call for this run id", logged == expected_calls,
            f"{len(logged)} logged / {len(expected_calls)} made")
    calls = body.get("calls", [])
    c.check("every log record has run id, start time, duration and status",
            bool(calls) and all(x["run_id"] == run_id and x["run_id_source"] == "mcp_meta" and x["started_at"]
                                and x["duration_ms"] is not None and x["status"] in ("ok", "error") for x in calls))
    c.check("every failed call has error text in the log",
            all(x["error"] for x in calls if x["status"] == "error"))
    if calls:
        c.show("example log record (last call)", calls[-1])

    print(f"\n{c.passed} passed, {c.failed} failed")
    return 0 if c.failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8102/mcp")
    parser.add_argument("--run-id", default=f"RUN-mcpcheck-{uuid.uuid4().hex[:8]}")
    parser.add_argument("--quiet", action="store_true", help="hide the full JSON results")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        return asyncio.run(run(args.url, args.run_id, args.quiet))
    except (OSError, urllib.error.URLError) as exc:
        print(f"[FAIL] cannot reach the MCP server at {args.url}: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
