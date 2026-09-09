"""Ingestion and Serving Service (Service 2).

Ingests Sensor_Data_Records from the simulator or the live feed, validates,
deduplicates, calibrates and converts them, computes sub-indices, NowCast and an
overall AQI, stores the result, and serves per-user views over an authenticated
HTTP API.

Layering (hexagonal, enforced by the design and by tests):

    contract/       the record models, Serializer and Parser — this service's OWN
                    independent copy, importing nothing from another service
    domain/         pure logic: calibration, unit conversion, AQI, personalization
    ports/          the protocols the domain depends on (stores, feeds, clock, auth)
    adapters/       concrete implementations of those ports (memory, DynamoDB, S3,
                    MQTT, HTTP feed, forecast, auth)
    ingest/         the ingest pipeline and its entry points
    serving/        the HTTP API and response assembly
    config/         configuration resolution and validation
    observability/  structured logging and metrics

Two rules hold throughout: no module in ``domain/`` imports from ``adapters/``,
and no module anywhere calls ``datetime.now()`` — time arrives through an
injected clock, so every behaviour is deterministic and offline-testable.
"""
