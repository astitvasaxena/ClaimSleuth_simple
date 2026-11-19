# Supervisor audit: pre-handover pass (2026-11-20)

Previous audit (2026-11-19 evening) is kept as `SUPERVISOR_AUDIT.md.bak-20261120`.

Scope: the 3 deliverables as committed at origin/main `186f279` (PR #2 merged): `ClaimSleuth_Final_Report.docx`,
`ClaimSleuth_Final_Presentation.pptx` (15 slides), code + AWS deployment, plus `results/*`, `DEPLOYED.txt`, README,
`docs/CHECKLIST.md`, `docs/EVIDENCE.md`, `docs/DEPLOYMENT_GUIDE.md`, `deploy/**`, `.github/workflows/ci.yml`.
Method: report and slide text and XML pulled out with python-docx and python-pptx. Every number was checked automatically against
`docs/EVIDENCE.md`, `results/*`, `DEPLOYED.txt`, `CONTRACT.md`, README and the pinned requirements, and the E3/E4/E5 and
latency tables were recomputed from the raw CSVs. Secrets were checked by `grep -F -f` against the password file, so nothing was printed.
The live site was probed with curl and no credentials. Quick tests were run. No other agent's file was edited. Nothing was committed.
Not done: no page or slide render (LibreOffice is still not installed; `pdftoppm` exists but has no PDF to work on). I did not
log in to the site, so the user's statement that the VM runs MODE=live with Gemini is NOT independently verified.

## Verdict: NOT READY

The deliverables are honest and match the results files, but the deployment story in all of them describes the 2026-11-19
setup, which no longer exists. The report appendix and DEPLOYED.txt give a URL that is now dead
(`https://65-2-175-255.sslip.io/` gives no response). The new URL `https://13-204-3-14.sslip.io/` is in no repo file.
"Offline mode only", "no Elastic IP, URL changes after stop/start" and "live mode not enabled" now contradict the VM.
CI/CD is mentioned in no deliverable and no doc. Fix B1 to B4 and rebuild, then the user steps U1 to U5 remain.

| Deliverable | Verdict | Blocking items |
|---|---|---|
| Final report (docx) | FAIL (stale) | B1, B2, B3; U2, U3 |
| Presentation (pptx) | FAIL (stale) | B1, B2, B3; H1 (no deployed screenshot) |
| Code + ZIP | PARTIAL | B1 (README, guides), U4 (ZIP not built) |
| Cloud deployment | PASS (site up), PARTIAL (demo risk) | B2 (live quota cap), H2 (auto-deploy during the assessment) |

## Checks

| # | Check | Result | Evidence |
|---|---|---|---|
| 1a | C-1 blackboard + messaging comparison | PARTIAL | `blackboard/store.py:119` class Blackboard, `:154` WAL; E4 in `results/messaging_comparison.csv` (3 x 40, 0 failed). Pub/sub arm not built, stated in report 7.7 and slide 7 |
| 1b | C-2 two MCP tools | PASS | `mcp_server/server.py:227` warranty_policy_search, `:246` claim_history_lookup |
| 1c | C-3 Pydantic structured output | PASS | `schemas.py:25` `extra="forbid"` |
| 1d | C-4 patterns compared | PARTIAL | `adk_service/agents.py:122` SequentialAgent; `orchestration/manager.py:82` InvestigationManager; E3 `results/architecture_comparison.csv` (medians 24747.4 / 12203.5 / 24252.0 ms match Table 14 and slide 5). Accuracy identical by construction, which is stated |
| 1e | C-5 callbacks, monitoring, heat map | PASS | `adk_service/callbacks.py:101-103`; `dashboard/static/eval.js:148`; `results/heatmap_agent_latency.png` |
| 1f | C-6 A2A ADK to CrewAI | PASS | `crew_service/a2a_server.py:101` AgentCard; `adk_service/forensic_a2a.py:1` RemoteA2aAgent |
| 1g | C-7 only ADK and CrewAI | PASS | two venvs; `requirements/main.txt`, `requirements/crew.txt` |
| 1h | C-8 security, personas, prompt variance | PASS (offline) | `security.py:26,114`; E5 116 runs, 0 of 58 paired changes, explained as certain by construction |
| 1i | C-9 observability / provenance | PASS | `observability/trace.py:63` TraceBuilder; audit trail |
| 1j | C-10 40+ runs, cost, time, boxplot | PARTIAL | 236 runs (`experiment_runs.csv`, recomputed: 236 rows, 0 errors) + baseline EV-20261119-141842-e46c4a; live cost over 40 runs not measured (stated) |
| 1k | C-11 cloud deployment | PASS (site), FAIL (records) | `curl https://13-204-3-14.sslip.io/` -> 401, remote IP 13.204.3.14, TLS verify 0, `server: Caddy`, `www-authenticate: Basic`. Old host gives no response. No repo file records the new URL, the Elastic IP, live mode or the auto-deploy (B1, B2, B3) |
| 1l | Report sections vs template | PASS | H1 order is the same as `docs/Final_Report_Format.docx` (8 numbered sections from style numbering), then Conclusions, References and Appendix unnumbered (numId 0); cover, declaration and contents present; matrix table has 14 rows (header + 13 activities, as in the template); same page size and margins |
| 1m | 15-slide structure 0-14 | PARTIAL | 15 slides, all with notes, numbered 1-14 after the title; slide 12 has no deployed-site screenshot (H1); slides 6, 8, 12 and 13 have stale deployment text (B1/B2) |
| 2a | Numbers traced | PASS | All report/PPT numbers found in sources, except: (a) seconds converted from ms (E3/E4/E5: recomputed from the CSVs, all match); (b) arXiv/DOI/years (references) and enrollments (TEAM.json); (c) Table 17 p25/p95 differ by 1 to 9 ms from my recomputation (quantile method only). Invented numbers: none found |
| 2b | Offline doubles never called LLM reasoning | PASS | report 1, 5.7, 7.1, 7.7 and slides 0, 5, 7, 9, 10 say "rule-based test doubles"; no slide or section credits them with reasoning |
| 2c | "Only 4 complete live runs" | PASS now, AT RISK | Consistent everywhere. If any claim has been run on the VM in live mode since the switch, that count is stale and the runs must be captured (B2) |
| 2d | Deployment facts current | FAIL | see B1/B2: stale in report 5.10 / 7.9 / Table 20 / Appendix A, slides 6, 8, 12, 13, DEPLOYED.txt:2,5,6, README.md:481-483,495, docs/DEPLOYMENT_GUIDE.md:80, deploy/aws/README.md:1,11,14, docs/AWS_DEPLOYMENT_PLAN.md:35,37, docs/EVIDENCE.md:143, docs/CHECKLIST.md:23, CONTRACT.md:4,23,53, `report_figures.py:174` (baked into `figures/fig_deployment.png`) |
| 2e | CI/CD described accurately | FAIL (absent) | Zero mentions of GitHub Actions / auto_update / timer in the report, PPT, README, DEPLOYMENT_GUIDE or deploy/aws/README. Only `.github/workflows/ci.yml` and `deploy/.env.example:10-14` describe it (B3) |
| 3a | Colour | PASS | `word/document.xml`: only `w:color 000000`, 0 shading, 0 highlight. Heat map figure is blue (`results/make_plots.py:84` `cmap="Blues"`, L3) |
| 3b | Contents page numbers | NOT VERIFIED | static builder estimates, no TOC field, no render (U2) |
| 3c | Machine-written tone | PASS | scan for delve/leverage/robust/seamless/furthermore/moreover/em dash/notably: 0 hits |
| 3d | Names / placeholders | PASS with note | TEAM.json names and enrollments on cover, declaration, matrix, slides 0 and 11; "Prof Pranshu Tiwar." / "Tiwar.Due" spacing as in the template (U3) |
| 4 | Consistency across report / PPT / README / code | FAIL | Ports 8110/8102/8103, versions (google-adk 2.13.0, crewai 1.15.33, mcp 2.3.0, a2a-sdk 1.3.0), 911 MB image, 795/1/155 and 801/155, R1-R11 and the literature (content_shared.py) are consistent. Deployment mode, URL and fixed-IP status are not (B1/B2) |
| 5a | Secrets | PASS | Site password: 0 hits in working tree (tracked + untracked, venvs excluded), 0 in docx/pptx parts, 0 in all git history. `AIza...{35}` outside tests: 0 in tracked files; the one hit in history is the fake `AIzaSyA1...` in a test (commit 4ff2306). AKIA/ASIA: 0. `-----BEGIN PRIVATE KEY-----` only as a test fixture string (`tests/test_prompt_injection.py:193`). No `.env`, `.pem`, `.zip`, `.tgz` tracked. Deployer IPs: 0 hits. The Gemini key cannot be in the repo by construction (`docker-compose.yml` reads `${GOOGLE_API_KEY}` from `deploy/.env` on the VM; `deploy/.env` is not tracked) |
| 5b | No unrequested commits | PASS | HEAD = origin/main = `186f279` (merged PR #2); `git diff origin/main` empty; only `.claude/` and `*.bak-*` untracked |
| 5c | Default behaviour unchanged | PASS | `execution_mode.py:25` offline default; `docker-compose.yml` `${MODE:-offline}`; `deploy/.env.example:8` `MODE=offline`; `dashboard/__main__.py:47` host defaults to 127.0.0.1 |
| 5d | Tests (quick) | PASS | `.venv` compileall src tests: exit 0; `bash -n` on all deploy scripts: OK; `.venv` test_experiments + test_dashboard + test_adjudicator: 49 passed (2.5 s); `.venv-crew` tests/crew: 155 passed, 2 deselected (69 s). Full main suite not re-run (35-75 min); last record 795 passed / 1 failed (existing), `results/test_run.txt` |
| 5e | CI | PASS | `gh run list`: CI success on the PR (3m46s) and on the merge to main `186f279` (3m25s). CI runs compileall, `bash -n`, a secret grep and a Docker build of `Dockerfile.single`; it does NOT run pytest (`ci.yml:2-4`) |
| 6 | Demo runs with nothing local | PASS with risks | EC2 + Caddy HTTPS + basic auth, reachable, fixed IP now. Risks: B2 (live quota), H2 (auto-deploy can restart the app during the assessment) |
| 7 | Data needed by the deliverables, not in git | PARTIAL | Everything under `docs/submission/` (docx, pptx, build scripts, content_shared.py, figures/*.png, results/*.csv/json/png/md, TEAM.json, DEPLOYED.txt) IS tracked and identical to origin/main. Missing: the 4 live-run folders (`runtime/runs/` is absent on this Mac and git-ignored, `.gitignore:27`); any live-mode run record from the VM; screenshots of the deployed site |

## What still needs committing

Right now nothing: `git status` shows only `.claude/` and `*.bak-*` (both deliberately left out) and
`git diff origin/main` is empty. Every results CSV/JSON/PNG, the figures, the build scripts, the docx, the pptx, TEAM.json
and DEPLOYED.txt are already on GitHub at `186f279`.
After the fixes below, these will need committing (when the user asks): DEPLOYED.txt, build_report.py, build_ppt.py,
report_figures.py, figures/fig_deployment.png, the rebuilt docx and pptx, README.md, docs/DEPLOYMENT_GUIDE.md,
deploy/aws/README.md, docs/AWS_DEPLOYMENT_PLAN.md, docs/EVIDENCE.md, docs/CHECKLIST.md, CONTRACT.md, this audit, any new
`results/` file (VM live-run record, screenshots), SYNC_LOG.md. Note: every push to main is picked up by the VM within about
60 s (`install_autoupdate.sh` timer), so push before the assessment window, not during it (H2).

## Open defects, ranked (owner in brackets)

Blockers
- B1 [docs-sync: build_report.py, build_ppt.py, report_figures.py, README, EVIDENCE, CHECKLIST, CONTRACT; deploy-agent:
  DEPLOYED.txt, DEPLOYMENT_GUIDE.md, deploy/aws/README.md, AWS_DEPLOYMENT_PLAN.md] The URL and fixed-IP facts are stale.
  The report appendix (`build_report.py:1158`, which reads the URL from DEPLOYED.txt:6) prints the dead `65-2-175-255` URL.
  "No Elastic IP / URL changes after a stop/start" appears in report 5.10 (`build_report.py:706`) and 7.9 (`:1074`),
  slides 12 and 13 (`build_ppt.py:563,590,611`), `fig_deployment.png` (`report_figures.py:174`), README.md:482,495,
  deploy/aws/README.md:1,11,14, AWS_DEPLOYMENT_PLAN.md:35,37, EVIDENCE.md:143, DEPLOYMENT_GUIDE.md:80, DEPLOYED.txt:2,6.
  Replace these with: Elastic IP 13.204.3.14, URL https://13-204-3-14.sslip.io/, stable across stop/start (`boot.sh` still
  derives the name from IMDS, which now returns the Elastic IP). Keep the 2026-11-19 stop/start test as history ("the IP
  changed before the Elastic IP was attached"). Also: deploy/aws/README.md:14 says an Elastic IP is "free while attached
  to a running instance". AWS has billed every public IPv4, attached Elastic IPs included, at 0.005 USD/h since
  February 2024. `deploy/aws/teardown.sh` does not release the Elastic IP, so after teardown it would keep costing money (add
  release-address or a manual step).
- B2 [user, then deploy-agent + docs-sync] Live mode on the VM is not evidenced, and as configured it will fail during a
  busy demo. (a) Report 5.10/7.9/Table 20, slides 6, 8, 12, 13, DEPLOYED.txt:5, README.md:481, CHECKLIST.md:23 and
  EVIDENCE.md:143 say "offline mode only / live mode not enabled". (b) No repo file shows a live run on the VM. Before any
  deliverable says "live on AWS", save one VM live run record (as was done for `results/aws_run_RUN-dash-6ac716c9bd.json`)
  with its model and request count. If live runs were made on the VM, "only four complete live runs" is now wrong and must be
  re-counted (CONTRACT.md:27). (c) Quota: `docker-compose.yml` passes only EXECUTION_MODE, GOOGLE_API_KEY and
  CLAIMSLEUTH_BLACKBOARD_DB into the container, so the code default `CLAIMSLEUTH_GEMINI_RPD=20` (`gemini_rate.py:15,49`)
  applies whatever the key's real quota is. At 6 to 9 requests per complete run, the examiners get about 2 to 3 claims per
  day (Pacific midnight reset); after that every agent is FAILED and claims are ESCALATEd (fail-closed, but it looks broken).
  Decide before the demo: either a paid key plus passing CLAIMSLEUTH_GEMINI_RPD through compose, or a planned switch back
  to MODE=offline, with the deliverables saying which. (d) The "about 3 s per claim" and the "445 / 266 / 1,142 ms" figures
  are OFFLINE runs on the VM. Report 7.7 "Which latencies to trust" calls them "the deployment measurement ... for the
  running system". With MODE=live that is no longer the running system: label them "VM, offline mode, 2026-11-19".
- B3 [docs-sync / deploy-agent] CI/CD is real but appears in no deliverable or doc. To get credit, add it (report 5.10 +
  Table 7 row, slide 12 or 13, README "Deployment", DEPLOYMENT_GUIDE) using only these facts: GitHub Actions `ci.yml` runs
  on every PR and push to main. It compiles src/tests, parses the shell scripts, greps for committed secrets and builds
  `Dockerfile.single` (no push), and it does not run pytest. Both runs so far passed (3m46s, 3m25s). Deployment is pull-based:
  the systemd timer `claimsleuth-update.timer` runs `deploy/auto_update.sh` 60 s after the previous run finishes. That script
  fetches the public repo, rsyncs the code (never deploy/.env, the volumes or docs), runs `boot.sh`, waits up to about 10 min
  for the app healthcheck and otherwise rolls back to the previous commit. Do NOT write: "only green builds deploy" (the
  VM deploys main whatever the CI result; branch protection could not be confirmed, the API returned 404). Do NOT write "deploys
  within about a minute": only the detection is about 60 s, and the rebuild time on the t4g.small was never measured. Either
  measure one merge-to-healthy time and save it under `results/`, or say "picked up within about a minute".
- U4 [user] The code ZIP does not exist. Everything is committed now, so build it from the repo
  (`git archive --format=zip HEAD`). That excludes `.venv*`, `runtime/`, `.claude/`, `*.bak-*` and `__pycache__` by
  construction. Decide whether to drop or label `docs/MASTER_VIVA_QA_COMPREHENSIVE.md` and the two old design `.docx`
  (CONTRACT.md:47 says they contain unmeasured numbers). Grep the ZIP for the site password before uploading.

High
- H1 [user + ppt-agent] Slide 12 still shows only the 1 November local, pre-simplification screenshots
  (`docs/screenshots/1_..5_`); there is no screenshot of the deployed site. Take 2 or 3 from https://13-204-3-14.sslip.io/
  (login prompt, a finished claim report, in whichever mode the demo will use) and put them in the PPT (and optionally the
  report's screenshots section).
- H2 [user] The auto-deploy is live. Any push to main during the assessment rebuilds and restarts the app on a 2 GB VM. A
  failed build triggers a rollback rebuild of up to about 10 min more. Set `AUTO_UPDATE=off` in `deploy/.env` on the VM for the
  assessment window, or freeze main.
- H3 [deploy-agent] `auto_update.sh` writes the failed commit into `deploy/.deployed_sha` even after rolling back to the
  previous one, so that file then names a commit that is not running. It is intentional (to avoid retrying every minute) but
  misleading. Record the running commit separately (e.g. `.running_sha`) before anyone quotes it.

Medium
- M1 [user] Contents page numbers are estimates and nothing was rendered. Open the docx in Word, check page numbers and
  figure placement, then export the PDF; open the pptx in PowerPoint and check text fit (the slide 12 caption fit warning is still open).
- M2 [user] Copy the four live-run folders (`runtime/runs/RUN-dash-90e0f64064`, `-5c40c24a5a`, `-9f819f9dbc`,
  `-bcdbabaf97`) from the Windows machine into `docs/submission/results/live_runs/`. Report 7.4 says they "can be opened only
  on the machine that holds them". An examiner can therefore not check Table 11.
- M3 [docs-sync] CONTRACT.md itself is stale: line 4 (origin/main @ 4ff2306), lines 23 and 53 (offline, no Elastic IP).
  The agents read it first, so fix it before the rebuild.

Low
- L1 [ppt-agent] Slide 10 speaker notes repeat the laptop/VM latency sentence twice.
- L2 [ppt-agent] Table text on slides 5, 7, 11 and 12 is 11 to 13 pt (brief says at least 14 pt).
- L3 [code-agent] Heat map uses `cmap="Blues"` (`results/make_plots.py:84`); the other report figures are greyscale.
- L4 [report-agent / ppt-agent] No explicit sentence that the agents changed 0 of the 40 benchmark decisions (35 rules, 5
  guardrail); Table 10 implies it.
- L5 [user] Declaration date and signatures are blank (by design); check the instructor's name spelling, which is copied from the template.

## What the user must still do
1. Decide the demo mode (B2c). If live: save one VM live run record and tell docs-sync how many live runs now exist. If
   offline: set MODE=offline on the VM and the docs stay "offline".
2. Have docs-sync and deploy-agent fix B1, B2, B3 and M3, then run `bash docs/submission/sync.sh` (needs LibreOffice for the
   render; without it, open both files in Word and PowerPoint yourself) (M1).
3. Take deployed-site screenshots for slide 12 (H1); copy the 4 live-run folders (M2).
4. Commit and push the fixed files (list above) before the assessment, then set `AUTO_UPDATE=off` on the VM (H2).
5. Build the ZIP with `git archive` and grep it for the password (U4). Hand the login to the examiners separately. Keep
   the instance running. After the course, run teardown AND release the Elastic IP (B1).
6. Confirm the responsibility matrix with the team (it is still "proposed") and sign the declaration (L5).
