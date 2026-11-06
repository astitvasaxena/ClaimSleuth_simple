"""The page's information hierarchy: the investigation first, the overviews and Security & Trust visible, every detailed
record kept in Advanced Diagnostics. The page itself is exercised in a real browser; this guards its structure."""
import re

from claimsleuth import config
from claimsleuth.dashboard.app import create_app

STATIC = config.PROJECT_ROOT / "src" / "claimsleuth" / "dashboard" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
JS = (STATIC / "app.js").read_text(encoding="utf-8")


def test_every_element_the_script_addresses_exists():
    known = set(re.findall(r'id="([\w-]+)"', HTML)) | set(re.findall(r'id="([\w-]+)"', JS))
    assert set(re.findall(r'\$\("([\w-]+)"\)', JS)) <= known


def test_the_investigation_comes_before_the_technical_detail():
    order = ['id="timeline"', 'id="results-banner"', 'id="alerts"', 'id="agents-body"', 'id="report"', 'id="hitl"', 'id="overview-body"',
             'id="coordination-body"', 'id="integrity-details"', 'id="advanced"']
    positions = [HTML.index(marker) for marker in order]
    assert positions == sorted(positions)


def test_only_the_detailed_records_are_collapsed():
    advanced = HTML[HTML.index('id="advanced"'):HTML.index("</main>")]
    for body in ("details-body", "trace-body", "bb-body"):
        assert f'id="{body}"' in advanced
    for summary in ("agents-body", "report", "hitl", "overview-body", "coordination-body", "integrity-body", "injection-body"):
        assert f'id="{summary}"' not in advanced
    assert "<details" in advanced and " open" not in re.findall(r"<details[^>]*>", advanced)[0]


def test_no_detailed_record_was_dropped_from_the_page():
    for record in ("Timeline", "Task lifecycle", "Tool calls", "A2A communication", "Blackboard provenance", "Errors, timeouts and blocked tasks",
                   "Model usage and cost", "All recorded events", "Monitoring collection errors", "Recent events", "Monitoring failures and findings",
                   "Provider and agent failures", "Automatic retries", "Whole investigations", "Attack-by-attack results", "Attempt-by-attempt results",
                   "Events from real investigations", "v.audit_trail"):
        assert record in JS, record
    assert "Audit Trail" in HTML


def test_warnings_do_not_wait_behind_an_expandable_section():
    alerts = JS[JS.index("function bbProblems"):JS.index("function renderReview")]
    for problem in ("authentication problems", "monitoring_failures", "decision_matches_report", "refused read", "evidence_integrity", "failures"):
        assert problem in alerts
    assert "renderAlerts();" in JS


def test_controlled_tests_stay_apart_from_real_investigations():
    assert JS.count("Controlled adversarial test") >= 2 and JS.count("Real claim investigations") >= 2
    assert "not evidence of how a live Gemini model behaves" in HTML


def test_the_api_routes_are_unchanged(tmp_path):
    from claimsleuth.dashboard.workspace import Workspace
    routes = {getattr(r, "path", None) for r in create_app(Workspace(tmp_path / "w", config.DATA_DIR), runtime_dir=tmp_path / "r").routes}
    assert routes >= {"/", "/api/status", "/api/samples", "/api/samples/{name}", "/api/upload", "/api/runs", "/api/jobs/{run_id}",
                      "/api/runs/{run_id}", "/api/runs/{run_id}/review", "/api/runs/{run_id}/blackboard", "/api/runs/{run_id}/trace",
                      "/api/monitoring/agents", "/api/evidence-integrity", "/api/evidence-integrity/run", "/api/prompt-injection",
                      "/api/prompt-injection/run", "/api/evaluation", "/api/evaluation/estimate"}


def test_the_evaluation_view_is_kept():
    assert 'id="view-evaluation"' in HTML and "/static/eval.js" in HTML
