"""Agent evaluation: dataset, ground truth, metrics, scoring of failures, aggregation, exports, dashboard API and
live-run refusals. Everything here is offline; no test can send a Gemini request (the ledger is a temp file and is
checked to stay empty)."""
import asyncio
import json
import socket
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from claimsleuth import execution_mode, gemini_rate
from claimsleuth.evaluation import dataset as ds
from claimsleuth.evaluation import metrics as M
from claimsleuth.evaluation import report
from claimsleuth.evaluation.runner import EvaluationError, run_evaluation, select_cases
from claimsleuth.evaluation.scoring import score_case
from claimsleuth.schemas import ClaimRunBundle, ExecutionStatus

ROOT = Path(__file__).resolve().parents[1]
CREW_PYTHON = ROOT / ".venv-crew" / "Scripts" / "python.exe"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite3"
    monkeypatch.setenv("CLAIMSLEUTH_GEMINI_LEDGER", str(path))
    for name in ("CLAIMSLEUTH_GEMINI_RPM", "CLAIMSLEUTH_GEMINI_RPD", "EXECUTION_MODE"):
        monkeypatch.delenv(name, raising=False)
    return path


# --------------------------------------------------------------------------- dataset and ground truth

def test_stored_dataset_is_valid_and_matches_the_generator():
    stored = ds.load()                                   # Pydantic + integrity checks
    assert ds.DATASET_FILE.read_text(encoding="utf-8") == ds.to_json(ds.build())
    assert stored.synthetic is True and stored.version == "eval-v1" and len(stored.cases) == 40
    assert len({c.case_id for c in stored.cases}) == 40
    assert {k: sum(c.ground_truth.category == k for c in stored.cases) for k in ds.CATEGORIES} == dict.fromkeys(ds.CATEGORIES, 8)
    assert {c.ground_truth.expected_decision.value for c in stored.cases} == {"APPROVE", "REJECT", "REQUEST_EVIDENCE", "ESCALATE"}


def test_ground_truth_is_consistent_with_each_scenario():
    for c in ds.load().cases:
        gt, kinds = c.ground_truth, {d.doc_id for d in c.documents}
        assert gt.rationale
        if gt.category == "valid":
            assert gt.expected_decision.value == "APPROVE" and not gt.anomalies and not gt.missing_evidence
        if gt.category == "missing_evidence":
            assert gt.missing_evidence and gt.expected_decision.value == "REQUEST_EVIDENCE"
            assert all(m not in kinds for m in gt.missing_evidence if m in ("invoice", "repair_report"))
        if gt.category == "policy_violation":
            assert gt.policy_violation and gt.expected_decision.value == "REJECT"
        if gt.category in ("suspicious", "conflict_injection"):
            assert gt.anomalies and gt.expected_decision.value == "ESCALATE"
        for fact in gt.expected_facts:                   # facts are what the documents say (by construction)
            doc = next(d for d in c.documents if d.doc_id == fact.doc_id)
            if fact.field != "repair_cost":
                assert fact.value in doc.text


def test_ids_are_neutral_and_ground_truth_never_reaches_the_agents(tmp_path):
    dataset = ds.load()
    out = ds.materialize(dataset, tmp_path / "data")
    texts = [p.read_text(encoding="utf-8") for p in out.rglob("*") if p.is_file() and p.suffix in (".json", ".txt")]
    joined = "\n".join(texts)
    for leak in ("expected_decision", "ground_truth", "anomalies", "policy_violation", "-INJ", "-VAG", "-ABU", "CS-0"):
        assert leak not in joined
    assert sorted(p.name for p in (out / "claims").iterdir()) == [f"EVAL-{i:02d}" for i in range(1, 41)]
    with pytest.raises(ValueError):
        bad = dataset.model_copy(update={"cases": dataset.cases[:39]})
        ds.validate(bad)


def test_stratified_selection_takes_every_category():
    picked = select_cases(ds.load(), limit=5)
    assert len({c.ground_truth.category for c in picked}) == 5
    with pytest.raises(EvaluationError, match="unknown case"):
        select_cases(ds.load(), case_ids=["EVAL-99"])


# --------------------------------------------------------------------------- metrics

def test_binary_metrics_and_missing_predictions():
    pairs = [(True, True), (True, False), (False, True), (False, False), (True, None)]
    m = M.binary_metrics(pairs)
    assert (m["tp"], m["fp"], m["fn"], m["tn"], m["n"], m["unscored"]) == (1, 1, 1, 1, 4, 1)
    assert m["accuracy"] == m["precision"] == m["recall"] == m["f1"] == m["false_positive_rate"] == 0.5
    none_positive = M.binary_metrics([(False, False), (False, False)])
    assert none_positive["recall"] is None and none_positive["precision"] is None and none_positive["accuracy"] == 1.0
    assert M.binary_metrics([(True, None)])["accuracy"] is None          # N/A, never 0


def test_multiclass_metrics_follow_scikit_learn_conventions():
    # y_true = A A B C, y_pred = A B B B  (values checked against sklearn.metrics with zero_division=0)
    m = M.multiclass_metrics([("A", "A"), ("A", "B"), ("B", "B"), ("C", "B")], ["A", "B", "C", "D"])
    assert m["accuracy"] == 0.5
    assert m["macro"]["precision"]["value"] == pytest.approx(4 / 9) and m["macro"]["recall"]["value"] == 0.5
    assert m["macro"]["f1"]["value"] == pytest.approx(0.388889, abs=1e-6)
    assert m["weighted"]["precision"] == pytest.approx(0.583333, abs=1e-6)
    assert m["weighted"]["f1"] == pytest.approx(0.458333, abs=1e-6)
    assert m["per_class"]["D"]["precision"] is None and m["macro"]["f1"]["classes"] == ["A", "B", "C"]
    assert m["confusion"]["matrix"] == [[1, 1, 0, 0], [0, 1, 0, 0], [0, 1, 0, 0], [0, 0, 0, 0]]
    empty = M.multiclass_metrics([("A", None)], ["A", "B"])
    assert empty["accuracy"] is None and empty["macro"]["f1"]["value"] is None and empty["unscored"] == 1


def test_confusion_matrix_rejects_unknown_labels():
    with pytest.raises(ValueError):
        M.confusion_matrix([("A", "Z")], ["A", "B"])


def test_box_statistics_and_insufficient_data():
    s = M.box_stats([1, 2, 3, 4, 5, 6, 7, 8, 9, 100])
    assert (s["q1"], s["median"], s["q3"]) == (3.25, 5.5, 7.75)          # numpy 'linear' quantiles
    assert s["outliers"] == [100] and s["whisker_high"] == 9 and s["whisker_low"] == 1
    few = M.box_stats([10, 20, None])
    assert few["sufficient"] is False and few["n"] == 2 and "more runs are required" in few["note"]


# --------------------------------------------------------------------------- real offline evaluation (5 cases)

@pytest.fixture(scope="module")
def offline_eval(tmp_path_factory):
    if not CREW_PYTHON.exists():
        pytest.skip(".venv-crew is needed for the CrewAI service")
    mp = pytest.MonkeyPatch()
    tmp = tmp_path_factory.mktemp("eval")
    mp.setenv("CLAIMSLEUTH_GEMINI_LEDGER", str(tmp / "ledger.sqlite3"))
    mp.setenv("CLAIMSLEUTH_BLACKBOARD_DB", "off")
    mp.delenv("EXECUTION_MODE", raising=False)
    mp.delenv("GOOGLE_API_KEY", raising=False)
    result = asyncio.run(run_evaluation("offline", limit=5, root=tmp / "evaluation",
                                        mcp_port=free_port(), crew_port=free_port()))
    yield result, tmp
    mp.undo()


def test_offline_evaluation_runs_every_approach_without_any_gemini_request(offline_eval):
    result, tmp = offline_eval
    meta, rows = result["meta"], result["rows"]
    assert meta["mode"] == "offline" and meta["cases_executed"] == 5 and meta["ledger_requests_added"] == 0
    assert gemini_rate.total_requests(tmp / "ledger.sqlite3") == 0
    work = tmp / "evaluation" / meta["evaluation_id"]
    for run in (work / "runs").iterdir():
        bundle = ClaimRunBundle.model_validate_json((run / "bundle.json").read_text(encoding="utf-8"))
        assert bundle.adk.model == "test-double" and bundle.crewai.model == "test-double"
        assert bundle.workflow_metadata["crewai"].llm_mode == "test-double"
    for r in rows:
        assert all(r[a]["status"] == "completed" for a in ("evidence", "investigator", "forensic", "combined"))
        assert r["combined"]["api_requests"] == 0 and r["combined"]["mcp_calls"] > 0
    for name in ("results.json", "cases.csv", "metrics.json", "report.md"):
        assert (work / name).exists()


def test_failed_and_incomplete_executions_are_not_scored_as_predictions(offline_eval):
    result, tmp = offline_eval
    work = tmp / "evaluation" / result["meta"]["evaluation_id"]
    case = next(c for c in ds.load().cases if c.case_id == result["rows"][0]["case_id"])
    bundle = ClaimRunBundle.model_validate_json(
        (work / "runs" / result["rows"][0]["run_id"] / "bundle.json").read_text(encoding="utf-8"))
    runs = [r.model_copy(update={"errors": ["investigator_agent: model call failed: 503 UNAVAILABLE"]})
            if r.agent == "investigator_agent" else r for r in bundle.adk.agent_runs]
    adk = bundle.adk.model_copy(update={"status": ExecutionStatus.FAILED, "report": None, "evidence_report": None,
                                        "agent_runs": runs, "errors": ["investigator_agent produced no structured report"]})
    row = score_case(case, bundle.model_copy(update={"adk": adk}), 1000.0, [], live=False)
    assert row["evidence"]["status"] == "incomplete" and row["investigator"]["status"] == "failed"
    assert "recommendation" not in row["evidence"] and "policy_violation_detected" not in row["investigator"]
    crashed = score_case(case, None, None, [], live=False, run_error="LiveModeError: refused")
    assert all(crashed[a]["status"] == "failed" for a in ("evidence", "investigator", "forensic", "combined"))
    agg = report.aggregate([row, crashed], {"mode": "offline"})
    rel = agg["approaches"]["investigator"]["reliability"]
    assert (rel["successful"], rel["failed"], rel["incomplete"]) == (0, 2, 0)
    assert agg["approaches"]["investigator"]["task_metrics"]["n"] == 0
    assert agg["approaches"]["investigator"]["task_metrics"]["accuracy"] is None      # N/A, not 0
    assert agg["approaches"]["evidence"]["reliability"]["incomplete"] == 1


def test_aggregate_builds_heatmaps_boxplots_and_confusion_matrices(offline_eval):
    agg = offline_eval[0]["aggregate"]
    hm1 = agg["heatmap_comparative"]
    assert [r["row"] for r in hm1["rows"]] == list(report.TITLES.values())
    assert [c[0] for c in hm1["columns"]] == ["accuracy", "precision", "recall", "f1", "completion", "validity"]
    assert len(agg["heatmap_scenario"]["rows"]) == 5 and all(len(r["cells"]) == 4 for r in agg["heatmap_scenario"]["rows"])
    assert agg["heatmap_reliability"]["rows"][3]["values"]["successful"] == 5
    assert agg["boxplots"]["latency_all"]["series"]["Combined Multi-Agent System"]["sufficient"] is True
    assert agg["boxplots"]["api_requests"]["series"] == {} and "no Gemini requests" in agg["boxplots"]["api_requests"]["note"]
    assert agg["boxplots"]["tokens"]["series"] == {}                 # offline test doubles report no tokens
    assert agg["confusion"]["combined"]["labels"] == ["APPROVE", "REJECT", "REQUEST_EVIDENCE", "ESCALATE"]
    assert sum(map(sum, agg["confusion"]["combined"]["matrix"])) == 5
    assert agg["usage"]["estimated_cost"] is None and agg["usage"]["tokens_total"] is None


def test_exports_mark_missing_values_as_na(offline_eval):
    result = offline_eval[0]
    csv_text = report.to_csv(result["rows"])
    assert csv_text.splitlines()[0].startswith("case_id,category,source,expected_decision")
    assert len(csv_text.splitlines()) == 6 and "N/A" in csv_text                   # e.g. tokens
    md = report.to_markdown(result)
    for section in ("Ground-truth methodology", "Comparative performance", "Scenario performance",
                    "Execution reliability", "Limitations", "Dataset: eval-v1", "Execution mode: **OFFLINE**"):
        assert section in md
    assert json.loads(report.to_json(result))["meta"]["mode"] == "offline"


# --------------------------------------------------------------------------- live refusals (no request is sent)

def test_live_evaluation_needs_confirmation_and_budget(ledger, monkeypatch, tmp_path):
    monkeypatch.setattr(execution_mode, "load_env", lambda: None)
    monkeypatch.setenv("GOOGLE_API_KEY", "dummy-key-for-tests")
    with pytest.raises(EvaluationError, match="explicit confirmation"):
        asyncio.run(run_evaluation("live", limit=1, root=tmp_path))
    monkeypatch.setenv("CLAIMSLEUTH_GEMINI_RPD", "15")
    with pytest.raises(EvaluationError, match="not enough local daily Gemini budget"):
        asyncio.run(run_evaluation("live", limit=2, confirm_live=True, root=tmp_path))   # 2 x 10 > 15
    assert gemini_rate.total_requests(ledger) == 0 and not any(tmp_path.glob("EV-*"))


def test_live_evaluation_without_a_key_is_refused(ledger, monkeypatch, tmp_path):
    monkeypatch.setattr(execution_mode, "load_env", lambda: None)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(EvaluationError, match="GOOGLE_API_KEY"):
        asyncio.run(run_evaluation("live", limit=1, confirm_live=True, root=tmp_path))


# --------------------------------------------------------------------------- dashboard API

def test_dashboard_evaluation_api(offline_eval, ledger, tmp_path, monkeypatch):
    from claimsleuth.dashboard.app import create_app
    from claimsleuth.dashboard.services import ServiceConfig
    from claimsleuth.dashboard.workspace import Workspace
    monkeypatch.setattr(execution_mode, "load_env", lambda: None)
    result, tmp = offline_eval
    eval_root = tmp / "evaluation"
    with TestClient(create_app(Workspace(tmp_path / "ws"), ServiceConfig(mcp_port=1, crew_port=2), tmp_path / "rt",
                               live=False, evaluation_root=eval_root)) as client:
        home = client.get("/api/evaluation").json()
        assert home["dataset"]["cases"] == 40 and home["evaluations"][0]["evaluation_id"] == result["meta"]["evaluation_id"]
        eid = result["meta"]["evaluation_id"]
        got = client.get(f"/api/evaluation/results/{eid}").json()
        assert got["meta"]["cases_executed"] == 5 and "heatmap_comparative" in got["aggregate"]
        assert client.get(f"/api/evaluation/results/{eid}/export.csv").text.startswith("case_id,")
        assert client.get(f"/api/evaluation/results/{eid}/export.md").text.startswith("# ClaimSleuth agent evaluation")
        assert client.get(f"/api/evaluation/results/{eid}/export.json").json()["meta"]["evaluation_id"] == eid
        assert client.get("/api/evaluation/results/EV-..%2F..%2Fsecrets").status_code == 404
        assert client.get(f"/api/evaluation/results/{eid}/export.exe").status_code == 404
        est = client.get("/api/evaluation/estimate?cases=2").json()
        assert est["requests_estimate"] == 20 and est["cases"] == 2
        bad = client.post("/api/evaluation/run", json={"mode": "live", "cases": 1, "confirmation": "yes"})
        assert bad.status_code == 400 and "confirmation phrase" in bad.json()["error"]
        too_many = client.post("/api/evaluation/run", json={"mode": "live", "cases": 9,
                                                            "confirmation": "RUN LIVE EVALUATION"})
        assert too_many.status_code == 400 and "limited" in too_many.json()["error"]
    assert gemini_rate.total_requests(ledger) == 0
