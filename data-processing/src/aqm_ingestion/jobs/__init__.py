"""Scheduled work that is neither ingestion nor serving.

A job here runs on its own schedule and reads and writes through the same ports the rest of the
service uses. Nothing on the serving path imports this package: Requirement 32.12 exists
precisely
to keep a whole-history derivation off a per-request path, and an import from ``serving/`` into
here would be the first step to breaking that.
"""
