# Supervisor audit: final pre-submission pass (2026-11-19, evening)

Scope: `ClaimSleuth_Final_Report.docx` (built 20:09), `ClaimSleuth_Final_Presentation.pptx` (15 slides, built 20:09), the code and
deployment files (working tree on `main`, HEAD `4ff2306`), `results/*`, `docs/CHECKLIST.md`, `docs/EVIDENCE.md`, `DEPLOYED.txt`.
Method: text and XML of both files extracted with python-docx / python-pptx; every number compared with `results/summary.json`,
`results/*.csv`, `results/baseline_eval_40_report.md`, `results/deploy_test.md`, `docs/EVIDENCE.md`; secrets grepped without
printing; tests re-run where cheap; live site probed with curl and no credentials. No other agent's file was edited.
Not done: no page render (LibreOffice is not installed; a Keynote export attempt failed), so layout, page numbers and slide
text fit are NOT visually verified.

## Verdict per deliverable

| Deliverable | Verdict | Blocking items |
|---|---|---|
| Final report (docx) | PASS WITH FIXES | D2, D4, D5 (user), D7; rest cosmetic |
| Presentation (pptx) | PARTIAL | D3 (literature/requirements differ from the report), D6 (no deployed-site screenshot), D2, D4 |
| Code + ZIP | PARTIAL | D1 (README says no deployment, no comparisons), D8 (ZIP not built; exclusion list) |
| Cloud deployment | PASS | D11 (keep the instance running; URL changes on stop/start) |

## Checks

| # | Check | Result | Evidence |
|---|---|---|---|
| 1a | C-1 blackboard + messaging comparison | PARTIAL | `blackboard/` exists; E4 measured (`results/messaging_comparison.csv`, 3 x 40, 0 failed); pub/sub arm not built, stated honestly in report 7.7 and slide 7 |
| 1b | C-2 two MCP tools | PASS | `mcp_server/server.py:50,227` (`warranty_policy_search`, `claim_history_lookup`) |
| 1c | C-3 Pydantic structured output | PASS | `schemas.py`, `contract.py` |
| 1d | C-4 patterns compared with numbers | PARTIAL | E3 in `results/architecture_comparison.csv` (seq 24.75 s, par 12.20 s, hier 24.25 s median); accuracy identical by construction, said so; CHECKLIST.md:16 itself rates it "Partly met" |
| 1e | C-5 callbacks, monitoring, heat map | PASS | `adk_service/callbacks.py`, `observability/`, `dashboard/static/eval.js` (heat maps), `results/heatmap_agent_latency.png` |
| 1f | C-6 A2A ADK to CrewAI | PASS | `crew_service/a2a_server.py`, `adk_service/forensic_a2a.py` |
| 1g | C-7 only ADK and CrewAI | PASS | two venvs; `requirements/main.txt`, `requirements/crew.txt` |
| 1h | C-8 security, personas, prompt variance | PASS (offline) | `security.py`, guardrails; E5 116 runs, 0 of 58 paired changes, explained as certain by construction |
| 1i | C-9 observability / provenance | PASS | `observability/trace.py`, audit trail, citations |
| 1j | C-10 40+ runs, cost, time, boxplot | PARTIAL | 236 offline runs + baseline EV-20261119-141842-e46c4a; boxplot and heat map exist; live cost over 40 runs not measured (stated) |
| 1k | C-11 cloud deployment | PASS | `curl https://65-2-175-255.sslip.io/` returned 401 from 65.2.175.255 at audit time (no credentials sent); `DEPLOYED.txt` |
| 1l | Report sections vs template | PASS | 8 numbered Heading 1 sections in template order incl. "Requirements for MAS"; Conclusions, References, Appendix as unnumbered Heading 1 (numId 0); cover, declaration, contents present; 13-row matrix with the template's activity names |
| 1m | 15-slide structure (0-14) | PARTIAL | all 15 present with notes; slides 2, 3, 11, 12, 14 partial (see D2, D3, D6) |
| 2 | Truthfulness of numbers | PARTIAL | All E3/E4/E5, Table 12/13/17, cost and test numbers match `summary.json`, `baseline_eval_40_report.md`, `test_run.txt`, EVIDENCE.md. Exceptions: D4, D7, D12, D17. Offline doubles are never called LLM reasoning; live runs are "only four complete" everywhere |
| 3a | Format: colour | PASS | `word/document.xml`: only `w:color 000000` (1,362), no `w:shd`, no highlight; figures greyscale except the logo and the heat map (D16) |
| 3b | Format: contents page numbers | NOT VERIFIED | static list with builder-estimated numbers (Introduction 1 ... Appendix 25); no render (D5) |
| 3c | Machine-written tone | PASS | no stock AI phrases found (scan for delve/seamless/robust/leverage/furthermore/em dashes etc.: none); prose is plain and first-person plural |
| 3d | Placeholders / names | PASS with note | TEAM.json names and enrollments correct on cover, declaration, matrix, slide 0 and 11; no `<...>` template placeholders left; one hedge "if present" (D14) |
| 4 | Consistency report / PPT / README / code | FAIL | README stale (D1); PPT literature, references and requirement IDs differ from the report (D3); ports 8110/8102/8103, versions (google-adk 2.13.0, crewai 1.15.33, mcp 2.3.0, a2a-sdk 1.3.0, pypdf 6.21.4), 911 MB image, 4 live runs, 801/155 and 795/1/155 are consistent |
| 5a | Secrets | PASS | site password (24-char pattern from `~/.claimsleuth-aws/site_password.txt`) found in 0 repo files and 0 docx/pptx parts; no AKIA/ASIA keys; only fake `AIza-test`/`AIzaSyA123` strings in tests; no `.env`, `.pem`, `.zip`, `.tgz` in the repo; deployer IP and security-group id not in repo; instance id appears only in `DEPLOYED.txt` (not a secret) |
| 5b | No unrequested commits | PASS | HEAD still `4ff2306`, tag `submission-2026-11-07`; all new work untracked/modified only |
| 5c | Default behaviour unchanged | PASS | only product change `dashboard/__main__.py` adds opt-in `--host` defaulting to 127.0.0.1 |
| 5d | Tests | PASS (known failure) | re-run now: `.venv-crew` 155 passed, 2 deselected (68 s); `.venv` `test_experiments.py` + `test_dashboard.py` 33 passed; the known existing failure `test_provider_reliability.py::test_the_managers_deadline_keeps_the_agents_record` reproduces. No src/tests file is newer than `results/test_run.txt` |
| 6 | Demo runs with nothing local | PASS with caveats | EC2 t4g.small, Caddy HTTPS + basic auth, offline mode; caveats D11 |

## Open defects, ranked (owner in brackets)

High
- D1 [code-agent / deploy-agent] `README.md:459-464` still lists as "Not built": the messaging comparison, the design-pattern evaluation and "cloud deployment". The README has no Docker/AWS section. It ships in the code ZIP and contradicts the report, the PPT and CHECKLIST.md:13,16,23.
- D2 [user, then report-agent + ppt-agent] Report Section 8 note and slide 11 say the matrix split is "as agreed by the team", but `TEAM.json` `matrix_note` says "PROPOSED split, to be confirmed by the team". Either the team confirms it (then the sentence is true) or the wording must change.
- D3 [ppt-agent] Slide 2 literature table (Hayes-Roth 1985, Nii 1986, ReAct, MetaGPT, Greshake, Zheng) and slide 14 references do not match the report's Section 3 table and reference list (Tractable, Shift, Verisk, Extend, Han and Zhang 2025, Salemi 2025, Terrarium 2025, Nii 1994). Slide 3 uses R1-R8 with different meanings from the report's R1-R11, and its G1-G4 / P1-P4 codes are defined only in the speaker notes, not on any slide. Slide 2 and 14 notes still say "confirm the exact bibliographic details before submission".
- D4 [report-agent, ppt-agent; deploy-agent for the record] Report 7.9 and slide 10 notes explain the 24-27 s Docker figure by "the full test suite running on the same Mac". The unloaded experiments (236 runs) show the same 24 s, so that explanation is not supported. The failing test's own comment (`tests/test_provider_reliability.py:557`: deadline 8 s "past MCP setup") suggests ADK/MCP setup on this Mac takes over 8 s, which is a likely common cause of both the failing test and the 12 s per ADK agent (hypothesis, not verified). Consequence: the E3/E4 "parallel halves wall time" result is dominated by this laptop artefact. Also, the VM split (445 / 266 / 1,142 ms, `RUN-dash-6ac716c9bd`) exists only in CONTRACT.md; EVIDENCE.md:95 has the range only. Save that run record (or the API output) under `results/` and drop the load explanation in 7.9 and slide 10 notes.

Medium
- D5 [user] Contents page numbers are estimates and no page or slide was rendered. Open the docx in Word, check the page numbers and figure placement, and export the PDF; open the pptx in PowerPoint and check text fit (SYNC_LOG reports a fit warning on the slide 12 caption).
- D6 [user + ppt-agent] Slide 12 has only local, pre-simplification screenshots from 1 November plus a deployment diagram; no screenshot of the deployed site. Take 2 or 3 screenshots of the live URL (login prompt, a finished claim report) and put them in.
- D7 [report-agent; code-agent for `results/README.md` limit 2] Report 7.7 says the low vague-persona mean comes from "a few claims with missing evidence ... not an effect of the structure". The data show one run (EVAL-25, hier/standard, 6.1 s). The same case took 24.1 s under evidence-first and 21.8 / 24.1 s in other configs, so the early finish is not reproducible. Only EVAL-24 is consistently short. Reword: one non-reproducible short run, cause unknown.
- D8 [user / deploy-agent] No code ZIP exists yet. `git archive HEAD` would miss all the new untracked files (Dockerfile.single, docker-compose.yml, deploy/, experiments.py, tests/test_experiments.py, docs/submission/). Exclude `.venv*`, `runtime/`, `*.bak-*`, `**/__pycache__`, `.claude/`, `docs/submission/render/`, and either exclude or clearly label `docs/MASTER_VIVA_QA_COMPREHENSIVE.md` and the two old `.docx` design files, which CONTRACT.md says contain unmeasured claims. Grep the finished ZIP for the site password before uploading.
- D9 [user] There is no portable live-run evidence: `runtime/runs/` is absent on this Mac. Copy the four live run folders from the Windows machine into `docs/submission/` (PROFESSOR_REVIEW item 6, viva question 25).
- D10 [report-agent / ppt-agent] Nowhere does it say plainly that the agents changed 0 of 40 benchmark decisions (35 rules, 5 guardrail), and there is no rules-only ablation (PROFESSOR_REVIEW item 2). The report half-covers this in 7.3 and 7.7. Add one explicit sentence on slide 10 or 13.
- D11 [user] The deployment has no Elastic IP. The URL in the report appendix, the PPT and DEPLOYED.txt is valid only until the next stop/start. Keep the instance running through the assessment (or rebuild both files after any restart), and hand the login to the examiners separately.

Low
- D12 [report-agent] Direct-passing median is 24.11 s in Table 15 but 24.12 s in the text below it (source 24,115.0 ms).
- D13 [report-agent] The Figure 1 legend says MCP calls are in "Figure 2"; the MCP figure is Figure 3.
- D14 [report-agent] In Table 21 the Deployment row says "deployment files and docs/DEPLOYMENT_GUIDE.md, if present". Name `Dockerfile.single`, `docker-compose.yml`, `deploy/` instead.
- D15 [report-agent / ppt-agent] Run accounting is not explained. 236 = E3 120 + E4 direct 40 + 76 new E5 runs, because E5's 40 standard dataset runs are the hier_blackboard rows. Report 7.7 and slide 9 list 120 / 120 / 116.
- D16 [code-agent / report-agent] Figure 8 heat map uses a blue colour map while the other figures are greyscale. Its caption does not say it shows only the standard prompt structure.
- D17 [report-agent] In 7.9, "0.005 USD per hour for the public IPv4 address and the disk" is wrong. DEPLOYED.txt bills the disk separately.
- D18 [report-agent] Table 6 says "all services bind to 127.0.0.1". In the container the dashboard binds 0.0.0.0:7860 on the compose network (`Dockerfile.single:45,49`) and is reachable only through Caddy. Say so.
- D19 [deploy-agent] `results/deploy_test.md:3` still says "No cloud account used; nothing deployed". It is true for that local test but reads as stale. Add a pointer to DEPLOYED.txt.
- D20 [ppt-agent] Table text on slides 5, 7, 9, 10, 11 and 12 is 11-13 pt, below the 14 pt minimum in the brief.
- D21 [user] The instructor name "Prof Pranshu Tiwar." and the missing space in "Tiwar.Due" are copied verbatim from the template. Check the spelling; fill the declaration date and signatures.
- D22 [report-agent] Appendix A durations (35 / 9 min) are the Windows README figures. On macOS the main suite took 75 min. Label them Windows.

## What the user must still do
1. Confirm the responsibility matrix with the team (D2), and fill the declaration date and signatures (D21).
2. Take screenshots of the deployed site for slide 12 (D6). Copy the four live-run folders from the Windows machine (D9).
3. After the agents fix D1, D3, D4, D7 and the low items, run `bash docs/submission/sync.sh`, open the docx and pptx in Word and PowerPoint, check page numbers and fit, and export the PDF (D5).
4. Build the code ZIP with the exclusions in D8, then grep it for secrets.
5. Keep the EC2 instance running until the assessment is over and give the login to the examiners out of band (D11). Run `deploy/aws/teardown.sh` afterwards.
