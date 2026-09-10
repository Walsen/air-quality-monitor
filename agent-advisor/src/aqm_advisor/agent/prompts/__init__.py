"""Prompt text as package DATA, not as literals in code.

Req 31.4 wants the prompt reviewable, diffable and testable as data. A `.md` file in a package
is all three; a string built inside a function is none of them, and the prompt is where most of
this service's safety behaviour is actually asked for.
"""
