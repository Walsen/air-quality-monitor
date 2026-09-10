"""Tests for the scheduled association entry point (Requirement 32.12).

The entry point is where the derivation stops being code and starts being behaviour, so these
tests
are about the wiring rather than the arithmetic: which users get derived for, where the sites
come
from, that a startup failure exits non-zero, and that the serving process does NOT build the
job.

That last one is the requirement's whole point. Requirement 32.12 keeps a whole-history read off
a
per-request path, and the cheapest way to keep that true is for the object not to exist on that
side
at all — so it is asserted over the `Runtime`'s own field set rather than trusted.
"""

from __future__ import annotations

import datetime as dt

import pytest

from aqm_ingestion.composition import Runtime, build_runtime, load_runtime
from aqm_ingestion.config.loader import ConfigError, resolve_and_validate
from aqm_ingestion.domain.symptoms import build_symptom_entry
from aqm_ingestion.jobs.entrypoint import build_association_job, main, run
from aqm_ingestion.ports.clock import FixedClock

_NOW = dt.datetime(2026, 7, 1, 12, tzinfo=dt.UTC)
_ENV = {"AQM_LOG_LEVEL": "info"}


def _runtime() -> Runtime:
    config = resolve_and_validate(env=_ENV, file_data={})
    return build_runtime(config, FixedClock(_NOW))


def _seed_diary(runtime: Runtime, user_id: str, days: int = 20) -> None:
    store = runtime.ports["symptom_log_store"]
    for offset in range(days):
        store.put(  # type: ignore[attr-defined]
            build_symptom_entry(
                {
                    "user_id": user_id,
                    "entry_date": _NOW.date() - dt.timedelta(days=offset),
                    "severity": 1 + (offset % 5),
                    "markers": ["cough"],
                    "reliever_used": False,
                },
                now=_NOW,
            )
        )


# --- Req 32.12: the job is NOT part of the serving runtime ---------------

def test_the_runtime_does_not_carry_the_association_job() -> None:
    # The cheapest way to keep a whole-history derivation off the serving path is for the object
    # not to exist there. Asserted over the field set so a later convenience addition fails
    # here.
    fields = set(Runtime.__dataclass_fields__)
    for forbidden in ("association_job", "association", "job", "jobs"):
        assert forbidden not in fields


def test_the_serving_package_does_not_import_the_jobs_package() -> None:
    # The jobs package docstring states this rule; this is the check that it holds. AST-based,
    # because a text search trips on the docstring that documents it — which has happened three
    # times in this repository.
    import ast
    import pathlib

    import aqm_ingestion.serving as serving

    root = pathlib.Path(next(iter(serving.__path__)))
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(name.startswith("aqm_ingestion.jobs") for name in names):
                offenders.append(path.name)
    assert offenders == [], f"serving/ imports the jobs package: {offenders}"


# --- which users get derived for ----------------------------------------

def test_a_cycle_derives_for_every_user_with_a_diary() -> None:
    runtime = _runtime()
    _seed_diary(runtime, "user-a")
    _seed_diary(runtime, "user-b")
    assert run(runtime) == 0


def test_a_cycle_with_no_diaries_is_a_success_not_a_failure() -> None:
    # The normal early state. A non-zero exit would make a scheduler retry a cycle that had
    # nothing to do.
    assert run(_runtime()) == 0


def test_a_cycle_reports_finding_nobody_rather_than_staying_silent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from aqm_ingestion.observability.logging import configure_logging

    configure_logging("info")
    run(_runtime())
    assert "association_cycle_empty" in capsys.readouterr().out, (
        "an operator must be able to tell 'ran and found nobody' from 'did not run'"
    )


def test_an_operator_can_re_run_a_single_user() -> None:
    runtime = _runtime()
    _seed_diary(runtime, "user-a")
    _seed_diary(runtime, "user-b")
    assert run(runtime, ["user-a"]) == 0


def test_only_users_with_entries_are_enumerated() -> None:
    runtime = _runtime()
    _seed_diary(runtime, "user-a")
    store = runtime.ports["symptom_log_store"]
    assert tuple(store.user_ids_with_entries()) == ("user-a",)  # type: ignore[attr-defined]


def test_the_enumeration_is_sorted_for_a_defined_batch_order() -> None:
    runtime = _runtime()
    for user in ("user-c", "user-a", "user-b"):
        _seed_diary(runtime, user, days=1)
    store = runtime.ports["symptom_log_store"]
    assert list(store.user_ids_with_entries()) == [  # type: ignore[attr-defined]
        "user-a",
        "user-b",
        "user-c",
    ]


def test_a_user_whose_entries_have_all_aged_out_is_not_enumerated() -> None:
    # Retention-filtered like every other read (Req 31.8), so the job is not handed a user it
    # can
    # only find nothing for.
    config = resolve_and_validate(
        env={"AQM_SYMPTOM_RETENTION_DAYS": "10"}, file_data={}
    )
    runtime = build_runtime(config, FixedClock(_NOW))
    store = runtime.ports["symptom_log_store"]
    store.put(  # type: ignore[attr-defined]
        build_symptom_entry(
            {
                "user_id": "ancient",
                "entry_date": _NOW.date() - dt.timedelta(days=40),
                "severity": 3,
                "markers": [],
                "reliever_used": False,
            },
            now=_NOW,
        )
    )
    assert tuple(store.user_ids_with_entries()) == ()  # type: ignore[attr-defined]


# --- the sites come from the serving path's own selector -----------------

def test_the_job_uses_the_same_selector_the_serving_path_uses() -> None:
    # A second notion of "the user's sensors" would let a learned threshold rest on one set of
    # sites while the response applying it names another. Asserted structurally: the builder
    # must
    # construct a GeoSelector, so the two cannot be independently defined.
    import ast
    import pathlib

    import aqm_ingestion.jobs.entrypoint as module

    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
    called = {
        ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)
    }
    assert "GeoSelector" in called


def test_the_job_is_built_with_the_configured_association_limits() -> None:
    config = resolve_and_validate(
        env={"AQM_ASSOCIATION_MIN_OBSERVATIONS": "21"}, file_data={}
    )
    runtime = build_runtime(config, FixedClock(_NOW))
    job = build_association_job(runtime)
    assert job._limits.min_observations == 21


def test_the_job_is_built_from_the_runtimes_own_ports() -> None:
    runtime = _runtime()
    job = build_association_job(runtime)
    assert job._symptoms is runtime.ports["symptom_log_store"]
    assert job._readings is runtime.ports["readings_store"]
    assert job._profiles is runtime.ports["profile_store"]


# --- Req 26 / §5: startup failures ---------------------------------------

def test_a_rejected_configuration_exits_non_zero_without_running() -> None:
    assert main(env={"AQM_ASSOCIATION_MIN_OBSERVATIONS": "1"}, clock=FixedClock(_NOW)) == 1


def test_a_rejected_configuration_logs_one_message_per_problem(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from aqm_ingestion.observability.logging import configure_logging

    configure_logging("info")
    main(
        env={
            "AQM_ASSOCIATION_MIN_OBSERVATIONS": "1",
            "AQM_ASSOCIATION_THRESHOLD_FLOOR": "999",
        },
        clock=FixedClock(_NOW),
    )
    rejected = [
        line
        for line in capsys.readouterr().out.splitlines()
        if "config_rejected" in line
    ]
    assert len(rejected) >= 2, "Req 26.3 wants one message per invalid value"


def test_a_valid_configuration_runs_a_cycle_and_exits_zero() -> None:
    assert main(env=_ENV, clock=FixedClock(_NOW)) == 0


def test_the_entry_point_shares_the_composition_roots_resolve_path() -> None:
    # Two copies of "resolve, then build" would eventually differ in which failures they report,
    # and the drifted one would be the one an operator hit. So the entry point calls the
    # extracted
    # loader rather than repeating it.
    import ast
    import pathlib

    import aqm_ingestion.jobs.entrypoint as module

    tree = ast.parse(pathlib.Path(module.__file__).read_text(encoding="utf-8"))
    called = {
        ast.unparse(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)
    }
    assert "load_runtime" in called
    assert "resolve_and_validate" not in called, (
        "the entry point must not resolve configuration itself"
    )


def test_load_runtime_raises_rather_than_returning_a_half_built_runtime() -> None:
    # Req 26.5 puts validation before construction: nothing is built when a value is rejected.
    with pytest.raises(ConfigError):
        load_runtime({"AQM_ASSOCIATION_MIN_STRENGTH": "5"}, FixedClock(_NOW))
