"""Adapters: one real and one local implementation per port.

The local implementations are not test doubles bolted on afterwards — they are what makes the
offline suite possible, so they ship in the package rather than in tests/.
"""
