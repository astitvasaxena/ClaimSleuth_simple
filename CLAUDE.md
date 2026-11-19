# ClaimSleuth: project rules for Claude
- Course deliverables (report .docx, PPT, code zip, cloud deployment) live in `docs/submission/`. Read `docs/submission/CONTRACT.md` before touching them.
- Documentation is automatic: a hook queues every edit under `src/`, `tests/`, `deploy/`, `requirements/`, Dockerfiles in `docs/submission/CHANGES_PENDING.md`. Before finishing any turn that changed product files, run the `docs-sync` agent (a Stop hook enforces this). Never edit `ClaimSleuth_Final_Report.docx` or the `.pptx` by hand: change the build scripts.
- `supervisor` audits before anything is called submission-ready.
- Never state a number that is not in `docs/EVIDENCE.md`, `docs/submission/results/` or a command output. Offline test doubles are never described as LLM reasoning.
- No git add/commit/push unless asked. No secrets in files; never read `.env`.
