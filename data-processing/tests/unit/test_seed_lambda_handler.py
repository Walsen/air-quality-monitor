"""Tests for the seed job's AWS Lambda handler (Feature: demo-data-refresh, Task 1).

The handler is the thin adapter the EventBridge SCHEDULED refresh invokes. It owns
no seeding logic — that is `jobs/seed_readings.py`, already tested — so these tests
pin only the handler's contract:

* it runs one seeding pass and reports ok, writing the demo readings;
* running it twice is idempotent (the seed's Req 10.4 property, end to end through
  the handler): the site count is unchanged and only the advancing hourly tail
  differs, so the stored reading count does not grow;
* it never raises out of the Lambda boundary (§5: a startup-shaped failure is
  logged and returned as ok=False, never thrown, which a scheduler would retry
  wholesale).

Everything runs against the in-memory adapters a default config builds, so the
suite needs no AWS. The scheduled event payload is irrelevant to a whole-catalogue
seed, so the handler ignores it.
"""

from __future__ import annotations

import datetime as dt

from aqm_ingestion.composition import Runtime, build_runtime
from aqm_ingestion.config.loader import resolve_and_validate
from aqm_ingestion.jobs import seed_lambda_handler as sh
from aqm_ingestion.ports.clock import FixedClock

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)


def _runtime(clock: FixedClock | None = None) -> Runtime:
    config = resolve_and_validate(env={"AQM_LOG_LEVEL": "info"}, file_data={})
    return build_runtime(config, clock or FixedClock(_NOW))


def _stored_reading_count(runtime: Runtime) -> int:
    readings = runtime.ports["readings_store"]
    # The in-memory store keys readings by DedupKey; its size is the row count.
    return len(readings._readings)  # type: ignore[attr-defined]


def test_the_handler_runs_a_seeding_pass_and_returns_ok() -> None:
    # The happy path: the handler seeds the demo catalogue and reports ok, without
    # raising. A current reading now exists for the demo sites.
    runtime = _runtime()
    result = sh.handler({"source": "aws.events"}, None, runtime=runtime)
    assert result["ok"] is True
    assert _stored_reading_count(runtime) > 0


def test_the_handler_is_idempotent_across_runs() -> None:
    # Req 2.1 end to end: a second refresh at the SAME instant re-puts identical
    # readings (Dedup_Key resolves to self) and re-upserts identical metadata, so
    # the stored reading count does not grow. The advancing tail is what changes
    # between real (later) runs; at a fixed clock the count is stable.
    runtime = _runtime()
    sh.handler({}, None, runtime=runtime)
    after_first = _stored_reading_count(runtime)
    sh.handler({}, None, runtime=runtime)
    after_second = _stored_reading_count(runtime)
    assert after_second == after_first, (after_first, after_second)


def test_a_later_run_advances_the_current_tail() -> None:
    # Req 1.1: the point of the refresh — a run at a later instant writes a reading
    # at the new current hour, so `current` stays fresh. Two runtimes sharing one
    # store, at clocks an hour apart, leave a reading at each current hour.
    from aqm_ingestion.adapters.memory import (
        InMemoryReadingsStore,
        InMemorySensorRegistryStore,
    )
    from aqm_ingestion.jobs.seed_readings import seed_exposure_history

    earlier = FixedClock(_NOW)
    later = FixedClock(_NOW + dt.timedelta(hours=1))
    registry = InMemorySensorRegistryStore()
    readings = InMemoryReadingsStore(clock=earlier)
    seed_exposure_history(registry=registry, readings=readings, clock=earlier, seed=1)
    before = len(readings._readings)
    seed_exposure_history(registry=registry, readings=readings, clock=later, seed=1)
    after = len(readings._readings)
    # The later run adds exactly the new current-hour interval per site/species that
    # the earlier run did not cover; it never rewrites history downward.
    assert after > before, (before, after)


def test_the_handler_never_raises_out_of_the_boundary() -> None:
    # §5 top-level boundary: even a startup-shaped failure is caught, logged, and
    # returned as ok=False — never thrown, which a scheduler would retry wholesale.
    class _Boom:
        @property
        def ports(self) -> object:
            raise RuntimeError("runtime unusable")

    result = sh.handler({}, None, runtime=_Boom())
    assert result["ok"] is False
    assert "error" in result
