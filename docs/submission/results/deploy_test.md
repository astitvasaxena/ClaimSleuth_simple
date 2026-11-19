# Deployment test results (2026-11-19)

Host: Docker Desktop 28.3.2, aarch64 (Apple silicon), 7.6 GB RAM. Mode: OFFLINE (rule-based test doubles). No cloud account used in this local test; nothing deployed by it. The later AWS deployment and its verification are recorded in `docs/submission/DEPLOYED.txt`.

## Image builds
| Image | Dockerfile | Size | Build time (wall, includes cache hits for base layers) |
|---|---|---|---|
| claimsleuth-app | Dockerfile | 418 MB | 1 min 08 s |
| claimsleuth-crew | Dockerfile.crew | 1.53 GB | 2 min 22 s |
| claimsleuth-single | Dockerfile.single (both venvs, uid 1000, port 7860) | 1.74 GB | 5 min 24 s |
All three built first time without Dockerfile changes.

## Defect found and fixed in docker-compose.yml
First compose run returned the correct decisions but every run was `INCOMPLETE` (Forensic Agent "was not run": "the blackboard is not enabled or not usable"). Cause: the dashboard sets `CLAIMSLEUTH_BLACKBOARD_DB` for the processes it launches, but the separate `crew` container did not have it. Fix: `CLAIMSLEUTH_BLACKBOARD_DB: /app/runtime/blackboard.sqlite3` on both services (shared `runtime` volume). After the fix all three agents finished (Forensic Agent no longer FAILED). A real-deployment bug caught only by running the stack.

## Compose stack (app + crew; Caddy replaced by `docker-compose.test.yml`, port 127.0.0.1:8110)
- `docker compose ps`: app and crew both `healthy` about 60 s after `up`; MCP and CrewAI RUNNING in `/api/status`; mode OFFLINE.
- Memory at idle after 4 runs: app 200 MiB, crew 181 MiB.
- Method: for each PDF in data/samples: `POST /api/upload` (application/pdf, X-Filename), `POST /api/runs {upload_id}`, poll `/api/jobs/{run_id}`, `GET /api/runs/{run_id}` and read `final.final_decision`.

| Claim | Expected (data/sample_ground_truth.json) | Got | Run status | End-to-end time |
|---|---|---|---|---|
| WC-2026-0411 | APPROVE | APPROVE | ESCALATED (payout INR 16,400 >= 15,000 sign-off threshold, human review required by design) | 26.5 s |
| WC-2026-0412 | ESCALATE | ESCALATE | ESCALATED | 25.5 s |
| WC-2026-0413 | ESCALATE | ESCALATE | ESCALATED | 25.5 s |
| WC-2026-0414 | REQUEST_EVIDENCE | REQUEST_EVIDENCE | COMPLETED | 24.4 s |
4 of 4 match. Timings are upload to finished report, sequential, including one-second polling.

## Single container (Dockerfile.single, `docker run -p 127.0.0.1:7860:7860`)
- Runs as uid 1000; ready (API answering) 6 s after start; Docker HEALTHCHECK `healthy`; MCP and CrewAI RUNNING; memory 381 MiB after 4 runs.
- Same four claims: 4 of 4 match (APPROVE, ESCALATE, ESCALATE, REQUEST_EVIDENCE), 24.4 to 25.6 s each; statuses ESCALATED x3, COMPLETED x1.


## Image slimming and single-image decision (2026-11-19, second pass; target AWS t4g.small, ARM, 2 GB RAM)
Measured with `docker image ls` / `docker history` / `du` inside the first single image (1.74 GB). Base python:3.12-slim = 110 MB Debian + 44 MB Python. `.venv` (dashboard, ADK, MCP) 157 MB; `.venv-crew` (CrewAI) 956 MB, which was 1.0 GB of the image on its own.
Largest items in `.venv-crew` (MB): lancedb 163, pyarrow 158, kubernetes 84, onnxruntime 64, chromadb_rust_bindings 53, numpy 40 (+25 numpy.libs), openai 20, grpc 18, uvloop 17, crewai 16, pillow libs 14, pip 13, tokenizers 11, pdfminer 10, pygments 10. In `.venv`: google 53, cryptography 15, pip 13, pygments 10. Unneeded at runtime: pip x2 (26), pytest/_pytest (about 3 each), tests/ directories (38 MB), `.pyi` stubs, and the optional crewai/chromadb back-ends. The same package versions in both venvs (pydantic, a2a, mcp, cryptography, ...) cannot be shared: the two venvs have different dependency sets and a symlink/hardlink scheme would not survive image layers.
Approach: `Dockerfile.single` is now multi-stage (builder installs and prunes, runtime copies only `.venv`, `.venv-crew`, `src`, `data`). Removed from `.venv-crew` by `pip uninstall`: pytest, kubernetes, onnxruntime, lancedb, pyarrow, lance-namespace (+ client), chromadb-rust-bindings, textual, pdfplumber, pdfminer-six, pypdfium2, openpyxl, hf-xet, uvloop. Removed from both: pytest, pip, every `tests`/`test` directory, `*.pyi`. Kept deliberately: chromadb (python package; crewai imports it at import time, removing it fails with ModuleNotFoundError), numpy, tokenizers, pillow, grpcio, instructor, pendulum (import-checked as removable but left in: crewai may load them lazily in paths I did not exercise; live mode was not re-run).
Each removal was checked by importing the CrewAI service modules, the Gemini and OpenAI-compatible LLM providers and MCP client in the pruned venv, then by the full 4-claim run below.

| Image | Before | After |
|---|---|---|
| claimsleuth-single (Dockerfile.single) | 1.74 GB | **911 MB** |
| `.venv-crew` / `.venv` inside it | 956 MB / 157 MB | 389 MB / 134 MB |
| for comparison, two-image stack (app + crew) | 418 MB + 1.53 GB = 1.95 GB | unchanged files, kept as docker-compose.two-image.yml (not slimmed) |
Docker `ls` sizes are uncompressed on-disk size. Build time of the slim image from a warm cache: about 2 minutes. Not measured: compressed (registry) size, build time on a t4g.small.

### Single vs two-image decision (numbers)
| | two-image stack (app + crew, measured earlier) | single image |
|---|---|---|
| Disk (images) | 1.95 GB | 0.91 GB (0.83 GB less than before the slimming, 1.04 GB less than two-image) |
| Memory after 4 runs | 200 + 181 = 381 MiB | 382 MiB (same processes) |
| Containers to supervise | 2 + Caddy, `network_mode: service:app` coupling | 1 + Caddy |
| Same 4 decisions | yes | yes |
Decision: `docker-compose.yml` (production, with Caddy) now builds `Dockerfile.single` (the dashboard starts MCP and CrewAI itself on 127.0.0.1). Memory is identical because the processes are identical; the single image wins on disk and simplicity. The blackboard env var `CLAIMSLEUTH_BLACKBOARD_DB=/app/runtime/blackboard.sqlite3` is kept in the compose file.

### Re-run of the 4 sample claims on the slim image (`docker run -p 127.0.0.1:7860:7860`, OFFLINE, uid 1000)
| Claim | Expected | Got | Run status | Agents (Evidence / Investigator / Forensic) | End to end |
|---|---|---|---|---|---|
| WC-2026-0411 | APPROVE | APPROVE | ESCALATED (payout above sign-off threshold) | all COMPLETED | 25.7 s |
| WC-2026-0412 | ESCALATE | ESCALATE | ESCALATED | all COMPLETED | 25.4 s |
| WC-2026-0413 | ESCALATE | ESCALATE | ESCALATED | all COMPLETED | 24.4 s |
| WC-2026-0414 | REQUEST_EVIDENCE | REQUEST_EVIDENCE | COMPLETED | all COMPLETED | 24.4 s |
4 of 4 match; no ModuleNotFoundError or Traceback in the container log; health check `healthy` 15 s after start.

### Memory (`docker stats`, Docker Desktop on Apple silicon, container `claimsleuth-single:latest`)
- Idle (services started, before any run): 339 MiB.
- PEAK during the four runs: 382.7 MiB (sampled continuously, one sample about every 2.5 s because each `docker stats --no-stream` call itself takes about 1.5 s; so a sub-second spike could be missed). After the runs: 382 MiB.
- Caddy container (idle, with the dashboard behind it): 13.6 MiB. Total of the production stack: about 352 MiB idle, about 400 MiB at peak, i.e. fits a 2 GB t4g.small with room for the OS (the 2 GB swap file is a safety margin). On the actual EC2 instance this was NOT measured.

### Production compose through Caddy (local, `SITE_ADDRESS=:80`, `docker compose up -d`)
app healthy, caddy up; `curl http://127.0.0.1/` without credentials returned 401, with the basic-auth password returned 200. HTTPS and Let's Encrypt were not tested (needs a public host). `docker compose down -v` executed afterwards.

### No-Elastic-IP boot script (`deploy/boot.sh`) tested with mocked metadata only
With a fake `curl` returning an IMDSv2 token and `13.232.10.5`, and a fake `docker`: SITE_ADDRESS `auto` and empty both produced `13-232-10-5.sslip.io` in `deploy/site.env` and ran `docker compose --env-file deploy/.env --env-file deploy/site.env up -d --build`; a real hostname in `.env` left `site.env` empty. Verified that real `docker compose` lets the second env file override `SITE_ADDRESS`. Not tested on a real EC2 instance (no aws CLI/account use).

## Not tested (first pass)
Caddy (HTTPS, basic auth), `deploy/gcp/*.sh`, `deploy/aws/README.md`, live mode (no key), the Hugging Face push itself (no account). `docker compose config` was valid with BASIC_AUTH_HASH set.

## Teardown
`docker compose ... down -v` executed; the single-container test container was removed. No claimsleuth containers or volumes remain (the three images remain locally).
