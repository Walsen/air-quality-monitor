"""Web Chatbot service: a browser chat UI in front of the AI Advisor.

This service holds no advisory logic. It serves a static chat page and a single
`/chat` endpoint that forwards the user's utterance to the advisor runtime on
Bedrock AgentCore and renders the structured Advisory_Response back to the
browser. It imports no other service's code — the advisor is reached only over
the network.
"""
