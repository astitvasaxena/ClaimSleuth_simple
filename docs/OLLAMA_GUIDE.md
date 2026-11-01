# ClaimSleuth — local Ollama inference for the CrewAI Forensic Agent

## Two modes, never mixed

| | `test-double` (default) | `ollama-local` |
|---|---|---|
| What answers CrewAI | `ForensicTestDoubleLLM`: deterministic code, **not a language model** | **qwen2.5:3b** running in Ollama on this laptop: **genuine local inference** |
| Protocol CrewAI uses | ReAct text (`Action:` / `Final Answer:`) | native function calling (`/v1/chat/completions` with `tools`) |
| Result labels | `model: "test-double"`, `provider: "offline-test-double"` | `model: "qwen2.5:3b"`, `provider: "ollama-local"` |
| Token usage | none (the double reports none) | from Ollama's `usage` per call |
| Network | loopback-only guard | loopback-only guard (Ollama and MCP are both on 127.0.0.1) |

`CLAIMSLEUTH_CREW_LLM` selects the mode. Any other value gives `NOT_CONFIGURED`. If Ollama or the model
is unavailable, live mode fails with `FAILED` and a reason, and **never falls back** to the test double.
There is no Gemini, OpenAI or other cloud provider anywhere in this path, and no API key.

## How it is wired (`src/claimsleuth/crew_service/ollama.py`)

- **Provider:** CrewAI 1.15.33's own `ollama` provider (`crewai.llms.providers.openai_compatible`),
  which talks to Ollama's OpenAI-compatible `/v1` endpoint through the `openai` SDK that comes with
  CrewAI. **No extra dependency**, no LiteLLM, no CrewAI upgrade.
- **`LocalOllamaLLM`:** a subclass of that provider class. Its only addition is a run budget,
  checked before every model call.
- **Configuration:** environment variables, each checked against an allowed range.

| Variable | Default | Allowed | Meaning |
|---|---|---|---|
| `CLAIMSLEUTH_OLLAMA_MODEL` | `qwen2.5:3b` | plain local tag | model; names containing "cloud" or a registry host are rejected |
| `CLAIMSLEUTH_OLLAMA_URL` | `http://127.0.0.1:11434/v1` | http, loopback host, `/v1` | anything else is rejected (`NOT_CONFIGURED`) |
| `CLAIMSLEUTH_OLLAMA_CALL_TIMEOUT` | 360 s | 30–900 | HTTP timeout of one model call |
| `CLAIMSLEUTH_OLLAMA_RUN_BUDGET` | 900 s | 60–3600 | no new model call starts after this |
| `CLAIMSLEUTH_OLLAMA_MAX_TOKENS` | 1200 | 256–4096 | output cap per call |
| `CLAIMSLEUTH_OLLAMA_MAX_ITER` | 4 | 2–8 | CrewAI agent iterations (stops tool-call loops) |
| `CLAIMSLEUTH_OLLAMA_GUARDRAIL_RETRIES` | 1 | 0–2 | task-guardrail retries |
| fixed | temperature 0, seed 7, `max_retries=0`, `max_retry_limit=0` | | no hidden HTTP or agent retries |

**Pre-run check (before every live run):**
- `GET /api/version`, then `GET /api/tags`.
- The model must be listed **exactly**, with a non-zero file size, no `remote_host` / `remote_model`,
  and the `tools` capability.
- A missing model is **not downloaded**; the run fails and lists the models that are available.

**Timeouts:**
- Each HTTP call is bounded by `CALL_TIMEOUT`.
- The run budget is checked before each call, so a run can overrun by at most one call timeout.
- CrewAI's own `Agent.max_execution_time` is **not used**: in 1.15.33 it waits for its worker thread
  to finish before raising, so it can't cut a run short.
- On the ADK side the A2A wait is explicit: `CLAIMSLEUTH_A2A_TIMEOUT` / `--a2a-timeout`, default
  180 s, range 30–3600. Use about 1500 s with `ollama-local`. A timeout gives a `FAILED` CrewAI result.

**Unchanged:**
- the MCP tools and their run, claim and caller headers;
- the `ForensicsAnalysis` output schema and the strict task guardrail (verbatim quotes; history
  sources must come from MCP);
- the `ForensicsReport`;
- the A2A contract;
- the deterministic adjudicator.

The CrewAI A2A service runs one investigation at a time.

## Commands (PowerShell, from `D:\Claimsleuth_simple`)

```powershell
# 0. Ollama is installed and running; the model is already local (nothing is downloaded)
Invoke-RestMethod http://127.0.0.1:11434/api/tags | ConvertTo-Json -Depth 5

# 1. MCP server (window 1)
.venv\Scripts\python -m claimsleuth.mcp_server --port 8102

# 2a. CrewAI alone, live, one claim (window 2)
$env:PYTHONIOENCODING = "utf-8"; $env:CLAIMSLEUTH_CREW_LLM = "ollama-local"
.venv-crew\Scripts\python -m claimsleuth.crew_service CS-003 --run-id RUN-live-manual-01

# 2b. or the A2A service in live mode, then the full integration (window 3)
.venv-crew\Scripts\python -m claimsleuth.crew_service.a2a_server --port 8103
Invoke-RestMethod http://127.0.0.1:8103/health | ConvertTo-Json -Depth 5      # llm.mode = ollama-local
.venv\Scripts\python -m claimsleuth.integration CS-003 --run-id RUN-live-demo-01 --a2a-timeout 1500

# back to the offline double
Remove-Item Env:CLAIMSLEUTH_CREW_LLM
```

Tests:
```powershell
.venv-crew\Scripts\python -m pytest tests\crew                                    # offline (incl. fake-Ollama tests)
.venv-crew\Scripts\python -m pytest -m live tests\crew\test_ollama_live.py -s     # LIVE, several minutes
```

The offline tests use `tests/crew/fake_ollama.py`, a **fake Ollama HTTP server** that is a test
fixture, not a model. With it they cover:
- unreachable Ollama, a missing model, remote or empty models, models without tool support;
- non-loopback addresses and cloud-style model names;
- invalid settings and unknown modes, with no fallback;
- the run budget;
- the native-function-call loop bound (CrewAI makes real MCP calls);
- HTTP call timeouts and the service health check.

## First live run: CS-003, 2026-10-31 (genuine local inference). Result: FAILED, and why

`RUN-live-cs003-01` went through the full integration: A2A service in `ollama-local` mode → CrewAI
`Crew.kickoff()` → qwen2.5:3b in Ollama. Files are in `runtime/runs/RUN-live-cs003-01/`.

| | Measured |
|---|---|
| Wall time (CrewAI side) | 529 s |
| Model call 1 | 3,805 prompt → **1,200** completion tokens (`finish_reason=length`, hit `max_tokens`), 305 s |
| Model call 2 (guardrail retry) | 2,050 prompt → 1,196 completion tokens (`stop`), 222 s |
| Ollama speeds (server log) | prompt 36–41 tok/s, generation 6.4–7.0 tok/s, CPU only |
| Network | `blocked_hosts: []`; both requests `POST /v1/chat/completions` from 127.0.0.1 |
| MCP | **no** `claim_history_lookup` call: the model answered directly without using the tool |
| Output | call 1's JSON contained the correct SERIAL_INCONSISTENCY (FRL-RF-77100-A vs FRL-RF-99400-B); the final output failed `ForensicsAnalysis` validation (`category`/`severity` = "NOT_CHECKED") |
| Final adjudication | ESCALATE by DETERMINISTIC_RULES (serial mismatch). The failed CrewAI result was marked unavailable and not used. |

**Root cause: the context window.**
- For our `/v1` request, Ollama started llama-server with `-c 4096 --context-shift`.
- Call 1 (3,805 prompt tokens plus output) overflowed and discarded 2,045 tokens mid-generation
  (`slot context shift ... n_discard = 2045`, `truncated = 1`).
- Call 2's prompt (5,058 tokens) was cut to its **last 2,050 tokens**
  (`truncating input prompt limit=2050 prompt=5058`). The model never saw the system prompt, the tool
  definitions or the start of the claim.
- Ollama's OpenAI-compatible endpoint can't set the context size per request.

**Fix options (each needs your approval; none applied):**
1. A derived local tag from a Modelfile (`FROM qwen2.5:3b` + `PARAMETER num_ctx 8192`). This uses the
   same weights, with no download, but adds a model tag to Ollama's store.
2. Set `OLLAMA_CONTEXT_LENGTH=8192` for the Ollama server. This needs an Ollama restart, and another
   program on this laptop was using Ollama (`POST /api/chat`, 10 min, 20:19–20:29).

Separately, the prompt (~3.8k tokens) should shrink, and `max_tokens` (1,200) was too small for the
full report. Also, the 3B model chose not to call the MCP tool.
