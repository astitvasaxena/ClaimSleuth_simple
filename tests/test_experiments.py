"""The E3/E4/E5 experiment harness (evaluation/experiments.py): pure logic and the temporary substitutions.
No service is started and no model is called; the full runs are done by `python -m claimsleuth.evaluation.experiments`."""
import asyncio
import os

from claimsleuth.adk_service import agents as A
from claimsleuth.adk_service import runner as R
from claimsleuth.claim_text import claim_message
from claimsleuth.datastore import load_claim
from claimsleuth.evaluation import dataset as ds
from claimsleuth.evaluation import experiments as X
from claimsleuth.orchestration import manager as M
from claimsleuth.packet import Packet


def _row(cfg="hier_blackboard", structure="standard", persona="honest", ok=True, wall=1000.0, phase="eval40", case="EVAL-01"):
    return {"config": cfg, "structure": structure, "repeat": 0, "case_id": case, "source": "eval40", "persona": persona,
            "category": "valid", "expected_decision": "APPROVE", "run_error": None, "wall_ms": wall,
            "final_decision": "APPROVE" if ok else "ESCALATE", "correct": ok, "decided_by": "adjudicator",
            "requires_human_approval": False, "evidence_ms": 10.0, "investigator_ms": 20.0, "forensic_ms": 30.0,
            "adk_ms": 40.0, "crew_ms": 50.0, "agents_run": 3, "llm_calls": 9, "agent_tool_calls": 4,
            "mcp_calls_logged": 4, "a2a_messages": 2, "blackboard_reads": 2, "investigator_saw_evidence": True,
            "forensic_saw_evidence": True, "context_chars_shared": 200, "adk_crew_overlap_ms": 5.0,
            "context_sharing": "blackboard", "packet_chars": None, "standard_packet_chars": 900, "instruction_chars": 4000,
            "model": "test-double", "phase": phase}


def test_persona_mapping_covers_the_dataset():
    cases = ds.load().cases
    personas = {X.persona_of(c) for c in cases}
    assert personas == {"honest", "vague", "abusive", "injection"}
    assert X.persona_of(next(c for c in cases if c.source == "existing:CS-001-VAG")) == "vague"
    assert len(X.eval_subjects()) == 40 and len(X.sample_subjects()) == 18


def test_stats_quantiles():
    s = X._stats([1.0, 2.0, 3.0, 4.0, None])
    assert s["n"] == 4 and s["median"] == 2.5 and s["min"] == 1.0 and s["max"] == 4.0
    assert X._stats([]) == {"n": 0}


def test_summary_views_and_flip_count():
    rows = ([_row(cfg=c) for c in X.ARCH_CONFIGS]
            + [_row(structure="evidence_first_react"), _row(structure="evidence_first_react", persona="vague", ok=False)]
            + [_row(persona="vague")])
    v = X.views(rows)
    assert {r["config"] for r in v["e3"]} == set(X.E3_CONFIGS)
    assert {r["messaging"] for r in v["e4"]} == set(X.E4_LABELS.values())
    out = X.summarise({"rows": rows, "started_at": "a", "finished_at": "b"})
    assert out["total_runs"] == len(rows) and out["e3_architecture"]["seq_direct"]["runs"] == 1
    assert out["e5_persona_structure"]["paired_comparisons"] >= 1
    assert "offline" in out["mode"]


def test_variant_patches_are_restored():
    originals = (R.build_agents, R.claim_message, A.EVIDENCE_INSTRUCTION, M.request_forensics, M.run_adk_investigation_async)
    for cfg in X.ARCH_CONFIGS:
        sink = {}
        with X.variant(cfg, "evidence_first_react", sink, asyncio.Event()):
            assert R.claim_message is not originals[1]
            assert "ReAct" in A.EVIDENCE_INSTRUCTION
            assert (R.build_agents is not originals[0]) == (cfg == "par_direct")
            assert (M.request_forensics is not originals[3]) == (cfg == "seq_direct")
        assert (R.build_agents, R.claim_message, A.EVIDENCE_INSTRUCTION, M.request_forensics,
                M.run_adk_investigation_async) == originals


def test_evidence_first_packet_is_reordered_but_parses_identically(dataset):
    data_dir, _ = dataset
    claim, docs = load_claim("CS-001", data_dir)
    sink = {}
    text = X._evidence_first_message(claim, docs, "RUN-T", sink)
    standard = claim_message(claim, docs, "RUN-T")
    assert text != standard and sorted(text.split("\n\n")) == sorted(standard.split("\n\n"))
    assert text.index("<untrusted source=\"claim_form\">") > text.index(docs[0].doc_id + "#L1")
    a, b = Packet(standard), Packet(text)
    assert (a.form, a.lines, a.fields, a.kinds) == (b.form, b.lines, b.fields, b.kinds)
    assert sink["packet_chars"] == len(text) and sink["standard_packet_chars"] == len(standard)


def test_parallel_variant_builds_a_parallel_agent_without_evidence_hand_off():
    from claimsleuth.adk_service.callbacks import ExecutionRecorder
    from claimsleuth.adk_service.test_double import offline_models
    agent = X._parallel_agents(offline_models(), "http://127.0.0.1:1/mcp", ExecutionRecorder("RUN-T"))
    assert type(agent).__name__ == "ParallelAgent"
    inv = next(a for a in agent.sub_agents if a.name == A.INVESTIGATOR_AGENT)
    assert "{evidence_report?}" not in inv.instruction and "run in parallel" in inv.instruction


def test_guard_forces_offline(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "x")
    monkeypatch.setenv("EXECUTION_MODE", "live")
    monkeypatch.setenv("CLAIMSLEUTH_BLACKBOARD_DB", "x")
    X._guard_offline()
    assert os.environ["EXECUTION_MODE"] == "offline" and "GOOGLE_API_KEY" not in os.environ
    assert "CLAIMSLEUTH_BLACKBOARD_DB" not in os.environ


def test_row_from_failed_run_is_recorded_not_scored_correct():
    subject = X.Subject("EVAL-01", "eval40", "honest", "valid", "APPROVE")
    row = X.row_from_bundle(None, subject, "seq_direct", "standard", 12.0, "ManagerError: x", {}, 0)
    assert row["correct"] is False and row["final_decision"] is None and row["run_error"]
