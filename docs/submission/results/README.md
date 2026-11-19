# Results folder (code-agent, 2026-11-19)

All numbers here were measured on a macOS machine, fresh venvs (Python 3.12.2), OFFLINE: the agents are labelled rule-based
test doubles, not an LLM. Everything around them (ADK Runner, MCP server, CrewAI, A2A, blackboard, adjudicator) is real.
No Gemini request was sent (ledger delta 0). All data is synthetic.

## Files
| File | What it is |
|---|---|
| `test_run.txt` | Test counts: .venv 795 passed, 1 failed (pre-existing test, not edited), 5 skipped; .venv-crew 155 passed; new tests 8 passed |
| `baseline_eval_40_*` | The existing 40-case offline evaluation (EV-20261119-141842-e46c4a): results.json, cases.csv, metrics.json, report.md. 40/40 expected final decisions |
| `architecture_comparison.csv` (+ `_runs.csv`) | E3: sequential-only vs parallel-only vs hierarchical manager, 40 cases each; aggregate and per-run |
| `messaging_comparison.csv` (+ `_runs.csv`) | E4: blackboard vs direct message passing vs no sharing, 40 cases each |
| `persona_prompt_runs.csv` | E5: personas x prompt structure, 116 runs |
| `experiment_runs.csv` | all 236 runs (one row per run, the source of everything else) |
| `experiments_summary.json` | aggregates per config / persona / structure |
| `summary.json` | every number the report and PPT need (built by `build_summary.py`) |
| `boxplot_latency.png`, `heatmap_agent_latency.png` | plots (built by `make_plots.py`, needs matplotlib from the tools venv) |

Code: `src/claimsleuth/evaluation/experiments.py` (run: `.venv/bin/python -m claimsleuth.evaluation.experiments run --out docs/submission/results`),
tests `tests/test_experiments.py`.

## What each experiment does
Variants are temporary in-process substitutions around the unchanged product code (restored after each run).
- hier_blackboard: the product default (manager, Evidence first, then Investigator and Forensic in parallel, signed blackboard).
- seq_direct: Evidence -> Investigator -> Forensic strictly in series, Evidence handed to the Investigator through ADK session state.
- par_direct: Evidence, Investigator and Forensic start together; no agent sees another's output.
- p2p_direct: Evidence -> Investigator in one ADK sequence, Forensic in parallel (the product's earlier direct mode).
E3 = seq_direct, par_direct, hier_blackboard. E4 = hier_blackboard (blackboard), p2p_direct (direct message passing), par_direct (no sharing). Each config ran once per case and both views read the same runs.
E5 = the 40 dataset cases + 18 sample claims (honest 38, vague 6, abusive 6, injection 8 per structure) x {standard, evidence_first_react} on the default wiring = 116 runs.

## Headline numbers (n = 40 per config, median wall time)
| Config | Correct | Median ms | Mean ms | Stdev ms | Context chars shared | Investigator saw Evidence | Forensic saw Evidence |
|---|---|---|---|---|---|---|---|
| sequential-only | 40/40 | 24747 | 24267 | 2862 | 2370 | 40 | 0 |
| parallel-only / no sharing | 40/40 | 12204 | 11986 | 1407 | 0 | 0 | 0 |
| hierarchical + blackboard | 40/40 | 24252 | 23416 | 3975 | 4740 | 40 | 40 |
| direct message passing | 40/40 | 24115 | 23863 | 1964 | 2370 | 40 | 0 |
Every config: 3 agents, 9 model calls, 6 MCP calls logged, 2 A2A messages per claim on average. Only the blackboard configs record blackboard reads (2 per run).
E5: 0 of 58 paired runs changed decision between structures; all 116 runs matched ground truth.

## Limits (state them wherever these numbers are used)
1. Accuracy is identical by construction: the doubles read the claim packet, not other agents' reports or prompt wording. The experiments show cost, time and context passed, not reasoning quality. No live accuracy exists.
2. Offline latency is about 12 s per ADK agent and about 0.5 s for the Forensic Agent in this stack (cause not investigated); the few runs near 3-7 s are missing-evidence claims where the agents finish early. It is not Gemini latency. Parallel-only is about half as fast because the two ADK agents overlap, not because of anything an LLM does.
3. E5 prompt structure: evidence-first reorders the real ADK packet (documents first, claim form last) and adds a ReAct instruction; the doubles ignore both, so no behavioural variance is claimed (0 decision changes). Latency variance between structures is noise. The CrewAI agent always receives the standard packet (separate process).
4. Parallel-only/no-sharing is the "no sharing" arm of E4. A true pub/sub broadcast arm was not built.
5. Cost: offline cost is 0 (no model billed, 0 Gemini requests). `observability/pricing.py` ships no prices; a live cost needs a verified price table and token counts. Live 4-run record: 6-9 requests per claim (9, 6, 7, 7); 40 live runs would need 240-360 requests (about 400 at the code's estimate of 10), against a free tier of 20 requests/day/model. No live token or rupee figure exists.
6. Test suite: one existing test fails on this machine (`test_the_managers_deadline_keeps_the_agents_record`); it was not edited. The full suite was not re-run after adding the 8 new tests (75 min); the new file passes alone.
