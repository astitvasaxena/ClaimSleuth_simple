# ClaimSleuth MCP server — guide

## What MCP is

The **Model Context Protocol (MCP)** is an open protocol that lets an AI application (the *client*) discover
and call *tools* offered by a separate *server*. The client asks the server which tools exist
(`tools/list`); each tool comes with a name, a description and a JSON Schema for its input and output.
The client then calls a tool by name with arguments (`tools/call`) and gets back a structured result or a
tool error. Messages are JSON-RPC 2.0 sent over a transport; ClaimSleuth uses **Streamable HTTP**.

MCP connects an agent to **tools and data**. It is not an agent and does not make decisions. Talking
between agents is a different protocol (A2A, Phase 3).

## Why ClaimSleuth needs it

- The Google ADK agents (Phase 4) and the CrewAI Forensic Agent (Phase 3) run in **different
  processes and Python environments**. MCP gives both of them the same tools, with the same schemas and
  the same validation, over the network, without either one importing the other's code.
- The tools return **records from the dataset** (policy terms and claim history) with a source reference
  on each, so an agent can cite evidence instead of making it up.
- It meets teacher criterion C-2 (tool integration through MCP, at least two tools).
- In the dashboard, the MCP panel shows the raw tool results. It shows **no decision**, because MCP
  is a tool protocol.

## How the server works

| Part | Where |
|---|---|
| Server object: `mcp.server.MCPServer` from the official SDK (`mcp==2.3.0`) | `src/claimsleuth/mcp_server/server.py` |
| Transport: Streamable HTTP at `http://127.0.0.1:8102/mcp` | `server.run("streamable-http", ...)` |
| Start-up entry point | `src/claimsleuth/mcp_server/__main__.py` |
| Tool logic (no MCP code) | `src/claimsleuth/policy_search.py`, `src/claimsleuth/history.py::query_history` |
| Input/output models | `src/claimsleuth/schemas.py` (`PolicySearchInput/Output`, `ClaimHistoryLookupInput/Output`) |

How a request is handled:

1. The SDK builds each tool's **input schema** from the function's type hints. The pattern, length
   and range limits come from the shared Pydantic types. The **output schema** comes from the
   return type (a Pydantic model).
2. A **server middleware** wraps every `tools/call`. It runs before argument validation, so it also
   sees and logs rejected calls and unknown tools.
3. The SDK validates the arguments. A wrong type, a bad pattern or an out-of-range limit is
   returned as a tool error (`isError: true`) that names the field.
4. The tool function builds the Pydantic input model. That model enforces the cross-field rule:
   at least one filter is required. The function then calls the data function and returns a
   validated Pydantic output model.
5. Errors are never turned into data. Bad input, a missing database or an unreadable policy file
   becomes `isError: true` with a message, and `structuredContent` is empty.
6. The middleware writes one JSON line per call to `runtime/logs/mcp_tool_calls.jsonl`.

**Run ID correlation.** The caller puts its run ID in the MCP request metadata:
`_meta: {"claimsleuth/run_id": "RUN-..."}`. If that is missing, the server reads the HTTP header
`X-ClaimSleuth-Run-Id`. The log records which source was used (`mcp_meta`, `http_header`, `none` or
`invalid`).

**Log record fields:** `call_id`, `run_id`, `run_id_source`, `tool`, `arguments` (keys that look like
secrets are replaced by `[REDACTED]`; long strings are truncated), `started_at` (UTC), `request_id`,
`status` (`ok`/`error`), `error`, `result_summary` (match counts), `duration_ms`.

**HTTP routes outside MCP** (operations only; they are not MCP tools and are labelled as such):

| Route | Purpose |
|---|---|
| `GET /health` | On every call: checks both tools are registered, reads `policies.json`, runs `COUNT(*)` on `claim_history` (read-only) and checks the log folder. Returns 200 `ok`, or 503 `unhealthy` with the failing check. |
| `GET /tool-calls?run_id=RUN-...&limit=100` | Returns the logged tool calls, filtered by run ID. Used by the client check and, later, the dashboard. |

## The two tools

### `warranty_policy_search`
Keyword search over the policy register `data/policies.json`, the same file the deterministic
baseline uses.

| Input | Type | Rule |
|---|---|---|
| `policy_id` | string, optional | `^[A-Z0-9][A-Z0-9-]{2,39}$` |
| `search_query` | string, optional | 2–300 characters, must contain a searchable word |
| `limit` | integer | 1–10, default 5 |

At least one of `policy_id` and `search_query` is required. Words in the query are matched against
the policy id, name, product category and each exclusion. Words such as *deductible*, *limit*,
*period*, *depreciation*, *receipt* and *diagnostic* point to the matching numeric or yes/no term.
For each matching policy the output contains:
- `terms`: the full stored policy terms;
- `matched_terms`: each matched term with its value and a JSON-pointer source such as
  `policies.json#/POL-LAPTOP-STD-02/exclusions/0`;
- `score`: the share of query words matched.

Results are sorted by score. No match gives `matches: []` and `total_matches: 0`.

### `claim_history_lookup`
Earlier claims from the SQLite table `claim_history` in `data/claimsleuth.db` (the same database as
the baseline).

| Input | Type | Rule |
|---|---|---|
| `claim_id` | string, optional | historical claim id, e.g. `HST-2025-1102` |
| `claimant_id` | string, optional | customer id, e.g. `USR-6610` |
| `serial_number` | string, optional | letters, digits and hyphens; case-insensitive |
| `limit` | integer | 1–50, default 10 |

**Investigation scope (added in Phase 7).** When a call carries a claim ID, through the `claimsleuth/claim_id`
`_meta` key or the `X-ClaimSleuth-Claim-Id` header, the server loads that claim from its data directory. Every ADK
and CrewAI agent call carries one, and the ID is set by code, not by the model.
- **Arguments:** `claimant_id` must be the claim's claimant, and `serial_number` must appear on the claim form, the
  invoice or the repair report.
- **Results:** every returned record must be linked to the claim by claimant or serial.
- **Refusals:** anything else gets the tool error `Out of scope - ...`, and the refusal is logged.
- **Limitation:** calls without a claim ID, such as `scripts/mcp_client_check.py`, are treated as operator lookups
  and are not scoped.

Tests: `tests/test_mcp_scope.py`.

At least one filter is required, and filters are combined with AND. Values are always sent as SQL
parameters, never pasted into the query text. The output has the records (newest first), `returned`,
`total_matches`, `truncated`, the table `source`, and one `record_sources` entry per record, such as
`sqlite:claimsleuth.db#claim_history/claim_id=HST-2025-1102`. Claims being investigated (`CS-…`) are
not in the history table, so looking one up returns no records.

## How tool discovery works

After connecting, a client sends `initialize` and then `tools/list`. The server answers with exactly
two tools, each with its description, `inputSchema` and `outputSchema`. Clients build their tool calls
from that schema: the ADK `McpToolset` turns it into function declarations for the model. The SDK
client checks every successful result against the advertised `outputSchema`.

## Start and stop (PowerShell, from `D:\Claimsleuth_simple`)

One-time setup (already done in this workspace):
```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements\main.txt
.venv\Scripts\python -m pip install -e .
.venv\Scripts\python -m claimsleuth.dataset          # builds data\ (policies, claims, SQLite)
```

**Start in the foreground** (logs in the console; stop with Ctrl+C):
```powershell
.venv\Scripts\python -m claimsleuth.mcp_server --port 8102
```

**Start in the background** (process log in `runtime\logs\mcp_server.log`):
```powershell
New-Item -ItemType Directory -Force runtime\logs | Out-Null
Start-Process -FilePath .venv\Scripts\python.exe -ArgumentList "-m","claimsleuth.mcp_server","--port","8102" `
  -RedirectStandardError runtime\logs\mcp_server.log -RedirectStandardOutput runtime\logs\mcp_server.out.log -WindowStyle Hidden
```

**Check it is running:**
```powershell
Invoke-RestMethod http://127.0.0.1:8102/health | ConvertTo-Json -Depth 5
```

**Stop the background server** (the server writes its own process id to `runtime\pids\mcp_server.pid`):
```powershell
Stop-Process -Id (Get-Content runtime\pids\mcp_server.pid); Remove-Item runtime\pids\mcp_server.pid
```

Options: `--host`, `--port`, `--data-dir`, `--db`, `--log-file`, `--pid-file`. You can also set
`CLAIMSLEUTH_MCP_HOST` and `CLAIMSLEUTH_MCP_PORT`.

## Run the independent client

With the server running:
```powershell
.venv\Scripts\python scripts\mcp_client_check.py              # full JSON results
.venv\Scripts\python scripts\mcp_client_check.py --quiet      # PASS/FAIL lines only
.venv\Scripts\python scripts\mcp_client_check.py --url http://127.0.0.1:8102/mcp --run-id RUN-myrun-0001
```
The program uses only the MCP client (`mcp.Client`) and the two HTTP routes. It does not import the
server or the tool functions. In order, it checks: health; discovery (both tools, and no others);
three policy searches (keywords, policy ID, no match); three history lookups (serial, claimant with a
limit, no records); eight invalid inputs and one unknown tool, which must all come back as tool errors;
and that the server log holds exactly one record per call under its run ID. The exit code is 0 only
if every check passes.

Use a **new run ID** for each run. The log keeps old records, so reusing an ID means the
"one record per call" check sees the earlier records too and fails.

## How to verify successful tool calls

1. The client prints `37 passed, 0 failed` and exits with code 0 (`$LASTEXITCODE`).
2. Look up the calls for one run:
   ```powershell
   Invoke-RestMethod "http://127.0.0.1:8102/tool-calls?run_id=RUN-myrun-0001" | ConvertTo-Json -Depth 6
   ```
3. Read the raw log:
   ```powershell
   Get-Content runtime\logs\mcp_tool_calls.jsonl -Tail 5
   Get-Content runtime\logs\mcp_server.log -Tail 20
   ```
4. Automated tests (each test starts its own server on a free port with a temporary copy of the dataset):
   ```powershell
   .venv\Scripts\python -m pytest tests\test_mcp_server.py tests\test_policy_search_and_history.py -v
   ```

## Limitations

- Policy search is **keyword matching** over the structured policy register, not embedding-based
  retrieval. Words that appear in several policies give partial matches: "water damage laptop" also
  returns the smartphone policy at score 0.333, because its exclusion "intentional damage" contains
  "damage".
- There is no authentication on `/mcp`, `/health` or `/tool-calls`. The server binds to `127.0.0.1`
  by default. Authentication is needed before any cloud deployment (Phase 9).
- The tool-call log is a local JSON-lines file per server process. `/tool-calls` reads the whole file,
  which is fine at this dataset's scale.
- SDK validation error messages repeat the rejected value (for example `input_value='bad id'`). The
  arguments are not secrets, but that text reaches the caller and the log.
