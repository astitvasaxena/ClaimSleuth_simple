"""Observability: the unified run trace (trace.py), measured metrics (metrics.py) and verified-only pricing (pricing.py).

The trace is assembled when a run is collected, from the events each component recorded as they happened: the
manager's plan, the ADK callbacks, the CrewAI event bus, the MCP server's log, the A2A messages and the blackboard.
It observes only: nothing here can change a task, a report, a verification or a decision.
"""
