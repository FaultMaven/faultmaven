"""Agent API Layer.

The module exposes no HTTP routes. The agent-execution endpoints
(``POST /cases/{id}/sessions/{sid}/execute``) were removed with the
orchestration service they drove; investigation turns run through the Case
module's turn endpoint and the milestone engine.
"""

__all__: list[str] = []
