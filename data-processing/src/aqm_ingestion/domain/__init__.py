"""Pure domain logic.

Nothing here imports from ``adapters/``, and nothing calls ``datetime.now()`` —
time and every external boundary arrive through ``ports/``, which is what makes
the domain deterministic and testable with no network.
"""
