"""Shared blackboard (SQLite): the run's shared state and the channel for intermediate context between the ADK
agents and the CrewAI A2A service. See docs/BLACKBOARD_GUIDE.md.

Enabled when CLAIMSLEUTH_BLACKBOARD_DB names a database file (the dashboard does this by default); "off" or "0"
disables it, which selects the documented direct mode (ADK session state and in-memory results, labelled
context_sharing="direct"). (The ADK plugin, adk_plugin.py, needs google-adk and is imported only by the ADK runner.)
"""
from claimsleuth.blackboard.auth import BlackboardAuthError, Writer, load_writer, provision_keys
from claimsleuth.blackboard.controller import (AgentParticipant, RunController, context_block, context_facts,
                                               report_monitoring_failure, security_event)
from claimsleuth.blackboard.models import CONTEXT_READERS, BoardEntry, ContextRef, RunState, document_fingerprint
from claimsleuth.blackboard.monitor import run_monitor
from claimsleuth.blackboard.store import (ENV_VAR, Blackboard, BlackboardError, BlackboardRejected,
                                          BlackboardSecurityError, configured_path, provision)

__all__ = ["AgentParticipant", "Blackboard", "BlackboardAuthError", "BlackboardError", "BlackboardRejected",
           "BlackboardSecurityError", "BoardEntry", "CONTEXT_READERS", "ContextRef", "ENV_VAR", "RunController", "RunState", "Writer",
           "configured_path", "context_block", "context_facts", "document_fingerprint",
           "load_writer", "provision", "provision_keys", "report_monitoring_failure", "run_monitor", "security_event"]
