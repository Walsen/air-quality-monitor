"""The ports: Protocols the application depends on, never concrete implementations.

No Bedrock, AgentCore or httpx type appears in any signature. That is what lets the whole suite
run with no AWS credentials and no network beyond localhost (Requirement 26.5).
"""
