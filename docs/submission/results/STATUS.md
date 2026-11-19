# STATUS (code-agent)
- 2026-11-19 17:03:52 .venv (python 3.12.2) READY: requirements/main.txt + pip install -e . ok, pip check clean.
- 2026-11-19 17:03:52 .venv-crew: install still running (pip, crew.txt). Tests, baseline eval and experiments wait for it.
- Experiment harness written: src/claimsleuth/evaluation/experiments.py (offline only). Not yet run.
- 2026-11-19 17:05:59 .venv-crew READY (pip check clean). Running test suites.
- 2026-11-19 18:22:06 TESTS DONE: .venv suite 795 passed, 1 failed, 5 skipped, 1 deselected (4537 s, macOS, python 3.12.2); the 1 failure is pre-existing tests/test_provider_reliability.py::test_the_managers_deadline_keeps_the_agents_record (reproduces alone; not edited). .venv-crew: 155 passed, 2 deselected (79 s).
- 2026-11-19 18:22:06 Starting experiments E3/E4/E5 (offline, ~236 runs, ~45 min). Baseline 40-case eval queued after.
- 2026-11-19 18:43:53 experiments running: 59 of 236 runs done (about 20 s per run offline; ETA ~75 min).
- 2026-11-19 19:48:35 EXPERIMENTS DONE: 236 runs, 0 failed (E3/E4/E5 csv + experiments_summary.json in results/). Next: plots, baseline 40-case eval, summary.json.
- 2026-11-19 20:05:28 ALL DONE. summary.json, README.md, CSVs, plots, baseline_eval_40_*, test_run.txt are final in results/. Baseline 40-case eval EV-20261119-141842-e46c4a: 40/40, 0 Gemini requests.
