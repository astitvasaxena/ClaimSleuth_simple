"""ADK plugin that publishes each ADK agent's own start and end on the blackboard (main environment only).

Uses ADK's supported plugin mechanism (App(plugins=[...]); before/after_agent_callback run for every agent), so
the agents, their instructions and their existing callbacks (adk_service/callbacks.py) are untouched.
Both hooks return None, which tells ADK to continue normally: the plugin observes, it never changes execution.
"""
from __future__ import annotations

from google.adk.plugins.base_plugin import BasePlugin

from claimsleuth.blackboard.controller import AgentParticipant


class BlackboardLifecyclePlugin(BasePlugin):
    def __init__(self, participant: AgentParticipant) -> None:
        super().__init__(name="claimsleuth_blackboard_lifecycle")
        self.participant = participant

    async def before_agent_callback(self, *, agent, callback_context):
        if agent.name in self.participant.agents:
            self.participant.agent_started(agent.name, source="adk plugin before_agent_callback",
                                           invocation_id=callback_context.invocation_id)
        return None

    async def after_agent_callback(self, *, agent, callback_context):
        if agent.name in self.participant.agents:
            self.participant.agent_finished(agent.name, source="adk plugin after_agent_callback",
                                            invocation_id=callback_context.invocation_id)
        return None
