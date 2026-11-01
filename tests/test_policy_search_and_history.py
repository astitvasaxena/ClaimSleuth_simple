"""Unit tests for the functions behind the two MCP tools (protocol tests are in test_mcp_server.py)."""
import pytest
from pydantic import ValidationError

from claimsleuth.dataset import HISTORY, POLICIES
from claimsleuth.history import query_history
from claimsleuth.policy_search import search_policies
from claimsleuth.schemas import ClaimHistoryLookupInput, PolicySearchInput


def _search(dataset, **kw):
    return search_policies(PolicySearchInput(**kw), dataset[0])


def _history(dataset, **kw):
    return query_history(ClaimHistoryLookupInput(**kw), dataset[1])


def test_search_by_keywords_ranks_the_matching_policy_first(dataset):
    out = _search(dataset, search_query="water damage laptop")
    assert out.matches[0].policy_id == "POL-LAPTOP-STD-02" and out.matches[0].score == 1.0
    sources = {t.source for t in out.matches[0].matched_terms}
    assert "policies.json#/POL-LAPTOP-STD-02/exclusions/0" in sources


def test_search_by_policy_id_returns_the_stored_terms(dataset):
    out = _search(dataset, policy_id="POL-TECH-CARE-01")
    assert out.total_matches == 1 and out.matches[0].score is None
    stored = POLICIES["POL-TECH-CARE-01"]
    terms = out.matches[0].terms
    assert (str(terms.deductible), terms.exclusions) == (stored["deductible"], stored["exclusions"])


def test_search_term_keyword_within_one_policy(dataset):
    out = _search(dataset, policy_id="POL-APPLIANCE-PLUS-03", search_query="deductible")
    assert [(t.field, t.value) for t in out.matches[0].matched_terms] == [("deductible", "5000.00")]


@pytest.mark.parametrize("kw", [dict(search_query="spaceship"), dict(policy_id="POL-UNKNOWN-99"),
                                dict(policy_id="POL-TECH-CARE-01", search_query="refrigerator")])
def test_search_no_matches(dataset, kw):
    out = _search(dataset, **kw)
    assert out.total_matches == 0 and out.matches == [] and out.sources == []


def test_search_limit(dataset):
    assert len(_search(dataset, search_query="deductible", limit=2).matches) == 2
    assert _search(dataset, search_query="deductible", limit=2).total_matches == 3


@pytest.mark.parametrize("kw", [dict(), dict(policy_id="bad id"), dict(search_query="x"),
                                dict(search_query="water", limit=11), dict(search_query="water", extra=1)])
def test_search_rejects_invalid_input(kw):
    with pytest.raises(ValidationError):
        PolicySearchInput(**kw)


def test_search_rejects_query_without_searchable_words(dataset):
    with pytest.raises(ValueError, match="no searchable words"):
        _search(dataset, search_query="the of and")


def test_history_by_serial_is_case_insensitive(dataset):
    out = _history(dataset, serial_number="nmb-x15-551230")
    assert [r.claim_id for r in out.records] == ["HST-2025-1102"]
    assert out.record_sources == ["sqlite:claimsleuth.db#claim_history/claim_id=HST-2025-1102"]


def test_history_filters_combine_with_and(dataset):
    assert _history(dataset, serial_number="NMB-X15-551230", claimant_id="USR-3305").total_matches == 1
    assert _history(dataset, serial_number="NMB-X15-551230", claimant_id="USR-7450").total_matches == 0


def test_history_by_claim_id_and_claimant(dataset):
    assert _history(dataset, claim_id="HST-2024-0718").records[0].outcome == "REJECTED"
    assert _history(dataset, claimant_id="USR-6610").records[0].amount is not None


def test_history_no_records(dataset):
    out = _history(dataset, claim_id="CS-007")
    assert (out.total_matches, out.returned, out.records, out.truncated) == (0, 0, [], False)


def test_history_limit_and_truncation(dataset):
    serial = HISTORY[0].serial_number
    out = _history(dataset, serial_number=serial, limit=1)
    assert out.returned == 1 and out.truncated is (out.total_matches > 1)


@pytest.mark.parametrize("kw", [dict(), dict(serial_number="x' OR '1'='1"), dict(claimant_id="usr 1"),
                                dict(claimant_id="USR-1", limit=0), dict(claimant_id="USR-1", limit=51)])
def test_history_rejects_invalid_input(kw):
    with pytest.raises(ValidationError):
        ClaimHistoryLookupInput(**kw)


def test_history_missing_database_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        query_history(ClaimHistoryLookupInput(claimant_id="USR-1"), tmp_path / "absent.db")
    assert not (tmp_path / "absent.db").exists()      # the lookup must not create an empty database
