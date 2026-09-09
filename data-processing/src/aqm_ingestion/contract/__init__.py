"""The record contract: models, Serializer and Parser.

This service keeps its OWN independent copy of the contract and imports nothing
from another service directory, so contract drift is caught by this package's own
round-trip tests rather than hidden behind a shared import.
"""
