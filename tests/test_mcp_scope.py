"""claim_history_lookup investigation scope, over the real MCP protocol (server subprocess + official client)."""
import asyncio

import pytest
from mcp import Client

from test_mcp_server import RunningServer


@pytest.fixture(scope="module")
def server(dataset, tmp_path_factory):
    srv = RunningServer(*dataset, tmp_path_factory.mktemp("mcp_scope"))
    yield srv
    srv.stop()


def call(url, args, claim_id="CS-005"):
    meta = {"claimsleuth/run_id": "RUN-scope-test"}
    if claim_id:
        meta |= {"claimsleuth/claim_id": claim_id, "claimsleuth/caller": "dashboard:security_lab"}

    async def go():
        async with Client(url) as client:
            return await client.call_tool("claim_history_lookup", args, meta=meta)
    return asyncio.run(go())


@pytest.mark.parametrize("args", [{"claimant_id": "USR-6610"}, {"serial_number": "frl-wm-302118"},
                                  {"claim_id": "HST-2025-0310"}])
def test_identifiers_of_the_claim_are_allowed(server, args):
    result = call(server.url, args)
    assert not result.is_error and [r["claim_id"] for r in result.structured_content["records"]] == ["HST-2025-0310"]


@pytest.mark.parametrize("args, message", [
    ({"claimant_id": "USR-4011"}, "claimant_id USR-4011 is not the claimant of claim CS-005"),
    ({"serial_number": "NMB-X15-551230"}, "does not appear on claim CS-005"),
    ({"claim_id": "HST-2024-0718"}, "not linked to claim CS-005"),
])
def test_other_customers_history_is_refused(server, args, message):
    result = call(server.url, args)
    assert result.is_error and "Out of scope" in result.content[0].text and message in result.content[0].text


def test_unknown_investigation_is_refused(server):
    result = call(server.url, {"claimant_id": "USR-6610"}, claim_id="CS-999")
    assert result.is_error and "not an investigation known to this server" in result.content[0].text


def test_both_serials_of_a_conflicting_claim_are_in_scope(server):
    # CS-003: the invoice and the repair report name different serials; both are part of the investigation.
    for serial in ("FRL-RF-77100-A", "FRL-RF-99400-B"):
        assert not call(server.url, {"serial_number": serial}, claim_id="CS-003").is_error


def test_refusals_are_logged(server):
    call(server.url, {"claimant_id": "USR-4011"})
    refused = [r for r in server.log_records() if r["run_id"] == "RUN-scope-test" and r["status"] == "error"]
    assert refused and refused[-1]["claim_id"] == "CS-005" and "Out of scope" in refused[-1]["error"]


def test_operator_lookup_without_claim_id_is_unscoped(server):
    """Documented limitation: calls with no claim id (operator scripts) are not scoped. Agent calls always carry one."""
    assert not call(server.url, {"claimant_id": "USR-4011"}, claim_id=None).is_error
