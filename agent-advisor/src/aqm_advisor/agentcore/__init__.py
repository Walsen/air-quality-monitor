"""The AgentCore deployment boundary — the ONLY package importing bedrock_agentcore.

Nothing under domain/ or agent/ imports this package (Requirement 32.2), so the advisory turn
stays testable without the runtime. app.run() serves /invocations and /ping with no AWS, which
is what lets the offline suite assert the deployment contract (Requirement 26.5a).
"""
