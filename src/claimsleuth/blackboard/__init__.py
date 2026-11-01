"""Shared coordination blackboard (SQLite), used by the ADK side and the CrewAI A2A service. See docs/BLACKBOARD_GUIDE.md.

Enabled only when CLAIMSLEUTH_BLACKBOARD_DB names a database file ("off" or "0" disables it). When it is not
enabled, nothing in the investigation changes. (The ADK plugin, adk_plugin.py, needs google-adk and is imported
only by the ADK runner.)
"""
from claimsleuth.blackboard.auth import BlackboardAuthError, Writer, load_writer, provision_keys
from claimsleuth.blackboard.controller import AgentParticipant, RunController, report_monitoring_failure
from claimsleuth.blackboard.models import BoardEntry, RunState, document_fingerprint
from claimsleuth.blackboard.monitor import run_monitor
from claimsleuth.blackboard.store import (ENV_VAR, Blackboard, BlackboardError, BlackboardRejected,
                                          configured_path, provision)

__all__ = ["AgentParticipant", "Blackboard", "BlackboardAuthError", "BlackboardError", "BlackboardRejected",
           "BoardEntry", "ENV_VAR", "RunController", "RunState", "Writer", "configured_path", "document_fingerprint",
           "load_writer", "provision", "provision_keys", "report_monitoring_failure", "run_monitor"]
