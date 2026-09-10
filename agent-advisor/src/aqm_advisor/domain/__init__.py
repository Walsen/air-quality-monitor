"""Pure advisory domain: red-flag recognition, grounding, forbidden claims, actions.

Depends on NOTHING outward. No import from adapters/ or agentcore/, no wall-clock read, no
random, and no whole-config parameter. Enforced by the AST checks of task 2.5, each carrying a
self-check proving the detector can fail.
"""
