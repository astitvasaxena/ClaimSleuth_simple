"""ADK plugin: each ADK agent's lifecycle on the blackboard, and the Evidence -> Investigator context through it.

Uses ADK's supported plugin mechanism (App(plugins=[...]); before/after_agent_callback run for every agent), so the
agents, their instructions and their own callbacks (adk_service/callbacks.py) are untouched.

Lifecycle: each ADK agent's own start and end.
Shared context (blackboard mode, an EvidenceContext is given):
  after the Evidence Agent    its report is validated (schema, claim id, citations, MCP references) and published to
                              report:evidence_agent at once, then the orchestrator is notified with the ContextRef;
  before the Investigator     the report is consumed from the blackboard (verified, receipted) and the verified copy,
                              wrapped as untrusted data, is written to the session-state key its instruction template
                              already reads ({evidence_report?}). Whatever the Evidence Agent left in session state is
                              replaced, never used. Without a verified report the Investigator is not started.
Supervision: the orchestration manager is told as each agent starts and finishes and when context is consumed, and
it must release the Investigator (authorise) before the Investigator may consume its context and run.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

from claimsleuth.adk_service.callbacks import cancelling
from claimsleuth.blackboard.controller import AgentParticipant, context_block
from claimsleuth.blackboard.models import ContextRef
from claimsleuth.schemas import EvidenceReport

EVIDENCE, INVESTIGATOR = "evidence_agent", "investigator_agent"
EVIDENCE_STATE_KEY = "evidence_report"          # the Evidence Agent's output_key, read by the Investigator's template


class EvidenceContext:
    """The Evidence Agent's report as shared context for one ADK run (blackboard mode).

    validate(raw state value) -> (EvidenceReport or None, problems); notify(ref or None, error or None) is called
    exactly once, as soon as the outcome of publishing is known; timing() -> the Evidence Agent's run timing.
    Supervision by the orchestration manager (both optional): authorise(agent) -> None to release the agent, or the
    reason it may not start; observe(agent, event, data) receives agent_started / agent_finished /
    context_consumed as they happen."""

    def __init__(self, participant: AgentParticipant,
                 validate: Callable[[Any], tuple[EvidenceReport | None, list[str]]],
                 notify: Callable[[ContextRef | None, str | None], None] | None = None,
                 timing: Callable[[], dict[str, Any]] | None = None,
                 authorise: Callable[[str], str | None] | None = None,
                 observe: Callable[[str, str, dict[str, Any]], None] | None = None) -> None:
        self.participant, self.validate, self.notify, self.timing = participant, validate, notify, timing
        self.authorise, self._observe = authorise, observe
        self.ref: ContextRef | None = None
        self.report: EvidenceReport | None = None       # the published (validated) report
        self.error: str | None = None
        self.supplied: str | None = None                # exactly what the Investigator's instruction received
        self._notified = False

    def publish(self, raw: Any) -> None:
        report, problems = self.validate(raw)
        if report is None or problems:
            self.error = "the Evidence report was not published to the blackboard: " + "; ".join(problems or ["invalid"])
        else:
            ref = self.participant.publish_report(EVIDENCE, report, self.timing() if self.timing else None)
            if ref is None:
                self.error = f"publishing the Evidence report to the blackboard failed: {self.participant.last_failure}"
            else:
                self.ref, self.report = ref, report
        self._notify()

    def finish(self) -> None:
        """End of the ADK run: make sure the orchestrator is told, even if the Evidence Agent never finished."""
        if not self._notified:
            self.error = self.error or "the Evidence Agent ended without a report"
            self._notify()

    def for_investigator(self) -> str | None:
        """The verified Evidence report, consumed from the blackboard, as the text the Investigator receives."""
        if self.ref is None:
            self.error = self.error or "no Evidence report was published to the blackboard"
            return None
        if self.authorise is not None:
            refusal = self._guard(lambda: self.authorise(INVESTIGATOR), "the manager could not be asked")
            if refusal:
                self.error = f"the manager did not release the Investigator: {refusal}"
                return None
        report = self.participant.consume_context(INVESTIGATOR, self.ref)
        if report is None:
            self.error = f"the Evidence report could not be consumed from the blackboard: {self.participant.last_failure}"
            return None
        self.supplied = context_block(report, self.ref)
        receipt = self.participant.consumed[-1]
        self.observe(INVESTIGATOR, "context_consumed", {"ref": self.ref, "receipt_run_seq": receipt.receipt_run_seq})
        return self.supplied

    def observe(self, agent: str, event: str, data: dict[str, Any] | None = None) -> None:
        if self._observe is not None:
            self._guard(lambda: self._observe(agent, event, data or {}), None)

    def _guard(self, action, on_error):
        try:
            return action()
        except Exception as exc:              # a supervisor problem must not crash the ADK run
            self.participant.fail("manager callback", f"{type(exc).__name__}: {exc}")
            return on_error

    def _notify(self) -> None:
        if self._notified:
            return
        self._notified = True
        if self.notify is not None:
            try:
                self.notify(self.ref, self.error)
            except Exception as exc:          # an observer problem must not break the ADK run
                self.participant.fail("notify evidence", f"{type(exc).__name__}: {exc}")


class BlackboardLifecyclePlugin(BasePlugin):
    def __init__(self, participant: AgentParticipant, context: EvidenceContext | None = None) -> None:
        super().__init__(name="claimsleuth_blackboard_lifecycle")
        self.participant, self.context = participant, context

    async def before_agent_callback(self, *, agent, callback_context):
        if agent.name not in self.participant.agents:
            return None
        if self.context is not None and agent.name == INVESTIGATOR:
            text = self.context.for_investigator()
            if text is None:      # fail closed: ends the invocation; the Investigator does not run
                return types.Content(role="model", parts=[types.Part(
                    text=f"{INVESTIGATOR} was not started: {self.context.error}")])
            callback_context.state[EVIDENCE_STATE_KEY] = text
        self.participant.agent_started(agent.name, source="adk plugin before_agent_callback",
                                       invocation_id=callback_context.invocation_id)
        if self.context is not None:
            self.context.observe(agent.name, "agent_started")
        return None

    async def after_agent_callback(self, *, agent, callback_context):
        if agent.name not in self.participant.agents:
            return None
        self.participant.agent_finished(agent.name, source="adk plugin after_agent_callback",
                                        invocation_id=callback_context.invocation_id)
        if self.context is not None:
            if cancelling():      # ADK's cleanup during a timeout: the agent did not finish, no report is expected
                self.context.observe(agent.name, "timed_out")
                return None
            self.context.observe(agent.name, "agent_finished")
            if agent.name == EVIDENCE:
                self.context.publish(callback_context.state.get(EVIDENCE_STATE_KEY))
        return None
