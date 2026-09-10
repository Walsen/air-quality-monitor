"""The scheduled entry point for the Exposure_Association derivation (Requirement 32.12).

A ONE-SHOT PROCESS, not a loop and not a thread inside the serving process. Requirement 32.12
says
the derivation runs "on its own schedule", and a separate invocation is what makes that true
rather
than aspirational: a thread in the serving process would compete with request handling for the
same
CPU and connection pool, and the requirement exists precisely to keep a whole-history read away
from
a per-request path. The schedule itself belongs outside this code — cron, an EventBridge rule, a
Kubernetes CronJob — and this module is what such a schedule invokes.

It is IDEMPOTENT by construction, which matters because a scheduler will occasionally deliver
twice:
:meth:`SymptomLogStore.put_learned_thresholds` replaces rather than merges, and the derivation
is a
pure function of stored data, so running it again over unchanged data writes the same values.

The users are the ones WITH DIARIES, not every user with a profile — see
``SymptomLogStore.user_ids_with_entries``. A user with no diary has nothing to derive from, so
including them could only ever return nothing.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from typing import cast

from aqm_ingestion.composition import (
    Runtime,
    StartupError,
    load_runtime,
    report_startup_failure,
)
from aqm_ingestion.config.loader import ConfigError
from aqm_ingestion.domain.profile import UserProfile
from aqm_ingestion.jobs.association import AssociationJob
from aqm_ingestion.observability.logging import configure_logging, get_logger
from aqm_ingestion.ports.clock import Clock, SystemClock
from aqm_ingestion.ports.protocols import (
    ProfileStore,
    ReadingsStore,
    SensorRegistryStore,
    SymptomLogStore,
)
from aqm_ingestion.serving.geo import GeoSelector

_logger = get_logger(__name__)


def build_association_job(runtime: Runtime) -> AssociationJob:
    """Assemble the job from a built runtime's ports.

    Built HERE rather than on the ``Runtime`` itself, deliberately: the runtime is what the
    serving
    and ingestion processes construct, and putting the job there would have every serving
    process
    construct a derivation it must never run. Requirement 32.12's separation is easier to keep
    when
    the object does not exist on that side at all.

    Sites come from the SAME :class:`GeoSelector` the serving path uses. That is the point of
    the
    job taking ``sites_for`` as a callable: a second notion of "the user's sensors" would let
    the
    learned threshold rest on one set of sites while the response that applies it names another.
    A selected site with no fresh reading is still returned by the selector (Requirement 20.9),
    which is what makes it usable for a historical window as well as a current one.
    """
    symptoms = cast("SymptomLogStore", _port(runtime, "symptom_log_store"))
    readings = cast("ReadingsStore", _port(runtime, "readings_store"))
    registry = cast("SensorRegistryStore", _port(runtime, "sensor_registry_store"))
    profiles = cast("ProfileStore", _port(runtime, "profile_store"))

    selector = GeoSelector(
        registry=registry,
        readings=readings,
        clock=runtime.clock,
        settings=runtime.config.selection,
    )

    def sites_for(profile: UserProfile) -> Sequence[str]:
        return tuple(site.site_code for site in selector.select(profile).sites)

    return AssociationJob(
        symptoms=symptoms,
        readings=readings,
        profiles=profiles,
        clock=runtime.clock,
        sites_for=sites_for,
        limits=runtime.config.association,
    )


def run(runtime: Runtime, user_ids: Sequence[str] | None = None) -> int:
    """Derive for every user with a diary, or for a named subset.

    Args:
        runtime: an already-built runtime.
        user_ids: an explicit subset, for an operator re-running one user. When None, every user
            with a diary entry inside the retention window is derived for.

    Returns:
        0 always. A per-user failure is isolated and logged rather than failing the cycle: a
        scheduled job that exits non-zero because one user's data is odd would be retried
        wholesale, re-deriving everyone to fix one — and the isolation is already in
        ``run_for_all``.
    """
    job = build_association_job(runtime)
    symptoms = cast("SymptomLogStore", _port(runtime, "symptom_log_store"))
    targets = (
        tuple(symptoms.user_ids_with_entries()) if user_ids is None else tuple(user_ids)
    )

    if not targets:
        # Not a failure and not silence: an operator needs to tell "ran and found nobody" from
        # "did not run", and a cycle with no diaries yet is the normal early state.
        _logger.info("association_cycle_empty", users=0)
        return 0

    outcomes = job.run_for_all(targets)
    _logger.info(
        "association_cycle_complete",
        users=len(outcomes),
        derived=sum(1 for o in outcomes if o.report is not None),
        thresholds=sum(o.thresholds_written for o in outcomes),
        truncated=sum(1 for o in outcomes if o.readings_truncated),
        skipped=sum(1 for o in outcomes if o.report is None),
    )
    return 0


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    clock: Clock | None = None,
) -> int:
    """Resolve configuration, build the runtime, and run one derivation cycle.

    Any positional arguments are user identities to derive for, which lets an operator re-run a
    single user without waiting for the next cycle. With none, every user with a diary is
    derived
    for.

    Returns:
        0 on success, 1 on any startup failure, with one logged message per fault (§5).
    """
    import os

    environment = dict(os.environ if env is None else env)
    configure_logging(environment.get("AQM_LOG_LEVEL", "info"))

    try:
        runtime = load_runtime(environment, clock or SystemClock())
    except (ConfigError, StartupError) as failure:
        return report_startup_failure(failure)

    requested = tuple(argv or ())
    return run(runtime, requested or None)


def _port(runtime: Runtime, name: str) -> object:
    """Take one port off the runtime, failing loudly if the root did not build it.

    Returns ``object``, so each call site CASTS to the port it wants. That is deliberate: a
    helper
    typed on the Protocol would need ``isinstance`` to earn its annotation, and a Protocol's
    ``isinstance`` accepts any object with matching method NAMES — a check that looks stronger
    than
    it is. The composition root's own factory table crosses the same boundary the same way, and
    the
    guarantee that every name resolves comes from
    ``test_every_registered_adapter_name_can_be_built`` rather than from a runtime check here.
    """
    port = runtime.ports.get(name)
    if port is None:  # pragma: no cover - the composition guard makes this unreachable
        raise StartupError(f"the composition root built no {name}")
    return port


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
