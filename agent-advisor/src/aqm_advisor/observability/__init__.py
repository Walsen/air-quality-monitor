"""Structured JSON logging with central redaction, plus the OpenTelemetry wiring.

Redaction is configured ONCE in the formatter. Requirement 19 forbids a condition, a coordinate,
a medication name or any utterance substring reaching a log, and a per-call-site discipline
cannot deliver that.
"""
