# ClaimSleuth agent evaluation - EV-20261119-141842-e46c4a

- Dataset: eval-v1 (40 synthetic cases, 8 per category)
- Evaluation date: 2026-11-19T14:18:42.247448+00:00 to 2026-11-19T14:34:58.159407+00:00
- Execution mode: **OFFLINE** (labelled offline test doubles, not LLM reasoning)
- Cases executed: 40 of 40 requested
- Combined system: 40 successful, 0 failed executions
- Gemini API requests: 0 (offline (no requests; ledger unchanged))

## Ground-truth methodology

Ground truth is fixed per case by the scenario's design and stored with the dataset
(data/evaluation/eval_dataset_v1.json); it is never computed by ClaimSleuth's rules or agents:
- expected decision: the outcome a correct warranty process should reach;
- policy violation: the documents show the claim is outside the policy (expired or excluded);
- missing evidence: required documents or fields that are absent;
- anomalies: forensic signals planted in the case;
- expected facts: the values written into the documents (ground truth by construction).

Each case runs once through the integrated workflow (deterministic baseline, Google ADK Evidence + Investigator
agents, the CrewAI Forensic Agent over A2A, MCP tools and the deterministic adjudicator). Each agent is scored on its
own report, for its own task; the Investigator runs as deployed and receives the Evidence report through ADK session
state. A failed or incomplete execution is counted under reliability and excluded from accuracy metrics, never
scored as right or wrong. Metrics that cannot be computed are reported as N/A, never as 0.

## Comparative performance (shared task: claim decision)

| Approach | Accuracy | Precision (macro) | Recall (macro) | F1 (macro) | Completion | Output validity |
|---|---|---|---|---|---|---|
| Evidence Agent | 65.0% | 59.1% | 65.6% | 57.6% | 100.0% | 100.0% |
| Investigator Agent | 92.5% | 93.2% | 95.3% | 93.5% | 100.0% | 100.0% |
| Forensic Agent | 75.0% | 61.1% | 71.9% | 63.7% | 100.0% | 100.0% |
| Combined Multi-Agent System | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |

Single agents are scored on the decision they recommend (advisory); the combined system on its adjudicated final decision. Agent-specific task metrics are reported separately.

## Agent-specific metrics

**Evidence Agent** - Missing-evidence detection (positive = required evidence missing): accuracy 100.0%, precision 100.0%, recall 100.0%, F1 100.0%, false-positive rate 0.0% (n=40); citation validity 100.0% (600/600).

**Investigator Agent** - Policy-violation detection (positive = NOT_ELIGIBLE): accuracy 100.0%, precision 100.0%, recall 100.0%, F1 100.0%, false-positive rate 0.0% (n=40); citation validity 100.0% (34/34).

**Forensic Agent** - Anomaly detection (positive = forensic anomaly present): accuracy 95.0%, precision 88.9%, recall 100.0%, F1 94.1%, false-positive rate 8.3% (n=40); citation validity 100.0% (304/304).

Evidence fact-extraction accuracy: 86.6% (252/291 facts).

Investigator history-anomaly detection: recall 75.0%, precision 100.0%.

Forensic per-anomaly recall: serial_mismatch 100.0% (5/5); history_other_claimant 100.0% (3/3); repeat_claims 100.0% (1/1); chronology 100.0% (2/2); identity_mismatch 100.0% (1/1); prompt_injection 100.0% (4/4); data_access_request 100.0% (1/1)

Combined final decision: accuracy 100.0%, macro F1 100.0%, weighted F1 100.0%; anomaly escalation recall 100.0%, false-positive rate 0.0%.

## Scenario performance

| Scenario | Evidence Agent: fact-extraction accuracy | Investigator Agent: policy-violation detection accuracy | Forensic Agent: anomaly detection accuracy | Combined Multi-Agent System: final-decision accuracy |
|---|---|---|---|---|
| Valid claims | 87.5% (n=8) | 100.0% (n=8) | 87.5% (n=8) | 100.0% (n=8) |
| Missing or insufficient evidence | 80.0% (n=8) | 100.0% (n=8) | 100.0% (n=8) | 100.0% (n=8) |
| Policy eligibility violations | 87.5% (n=8) | 100.0% (n=8) | 87.5% (n=8) | 100.0% (n=8) |
| Suspicious claims, serial mismatches and history anomalies | 87.5% (n=8) | 100.0% (n=8) | 100.0% (n=8) | 100.0% (n=8) |
| Conflicting evidence and prompt-injection attempts | 87.5% (n=8) | 100.0% (n=8) | 100.0% (n=8) | 100.0% (n=8) |

## Execution reliability

| Approach | Successful | Failed | Incomplete | Output failures | Avg latency (ms) |
|---|---|---|---|---|---|
| Evidence Agent | 40 | 0 | 0 | 0 | 12,070.3 |
| Investigator Agent | 40 | 0 | 0 | 0 | 12,074.1 |
| Forensic Agent | 40 | 0 | 0 | 0 | 544.8 |
| Combined Multi-Agent System | 40 | 0 | 0 | 0 | 24,279.3 |

## Latency distribution (ms)

| Approach | n | Q1 | Median | Q3 | Whiskers | Outliers |
|---|---|---|---|---|---|---|
| Evidence Agent | 40 | 11983 | 12051 | 12178 | 11798-12348 | 0 |
| Investigator Agent | 40 | 12007 | 12060 | 12135 | 11884-12300 | 1 |
| Forensic Agent | 40 | 529 | 535 | 544 | 520-565 | 2 |
| Combined Multi-Agent System | 40 | 24083 | 24226 | 24360 | 23918-24716 | 1 |

Gemini API requests per execution: Offline evaluation: the test doubles make no Gemini requests (0 per execution, confirmed by the request ledger), so there is no distribution to plot. Live observations are required.

Token usage per execution: No token usage was reported in this evaluation (offline test doubles report none), so no token box plot is drawn.

## Confusion matrix - combined final decision (rows: expected, columns: predicted)

| | APPROVE | REJECT | REQUEST_EVIDENCE | ESCALATE |
|---|---|---|---|---|
| APPROVE | 8 | 0 | 0 | 0 |
| REJECT | 0 | 8 | 0 | 0 |
| REQUEST_EVIDENCE | 0 | 0 | 8 | 0 |
| ESCALATE | 0 | 0 | 0 | 16 |

## Usage

- Gemini API requests: 0 (offline (no requests; ledger unchanged))
- MCP tool calls: 240 (mean per case 6.0)
- Tokens: N/A
- Estimated cost: N/A: no verified per-token price is configured, so no cost is estimated.

## Limitations

- The dataset is synthetic (40 cases); the results do not measure real-world fraud-detection performance.
- 18 cases reuse scenarios the system was developed against, so those are in-distribution; the 22 new cases were
  written by the same developers who wrote the deterministic rules. A high combined-system score therefore shows the
  rules work as designed on these scenario types, not that they generalise to unseen claims.
- Offline evaluation uses the labelled test doubles, deterministic rule-based stand-ins for the language model:
  offline numbers describe the pipeline with those stand-ins, not LLM reasoning.
- Several task predictions are derived from structured report fields with documented rules (for example, the
  Forensic Agent counts as detecting an anomaly when a serial or chronology check is INCONSISTENT, a history pattern
  is reported, or a MEDIUM/HIGH serial, date, history or suspicious-detail finding is present).
- When the ADK workflow does not finish, neither ADK report is released, so the Evidence Agent can be counted
  incomplete because of an Investigator failure.
- Cost is not estimated: no verified per-token price is configured.

