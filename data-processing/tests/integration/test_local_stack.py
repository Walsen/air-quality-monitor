"""Container-fenced integration checks (task 28.2).

Requirements 28.5, 28.6, 4.1, 4.6, 14.9, 15.10, 16.5, 17.11.

EVERY TEST HERE IS MARKED ``integration`` so the offline suite excludes it, and NONE depends on
a
cloud credential. That is Requirement 28.6's exact shape and the dev-environment steering's one
fenced exception: needing a container ENGINE is not licence to need an ACCOUNT. The emulator's
key
pair is the literal ``test``, which is why these checks can be strict about never reading the
developer's own credentials.

WHAT THIS FILE DOES NOT DUPLICATE. The DynamoDB and S3 adapters' BEHAVIOUR is already specified
by
the shared port contract suite (task 27.1), whose cloud parameters skip when no endpoint is
reachable and run when one is. So the job here is not to restate those assertions — it is to
make
the endpoint exist and then prove the suite's cloud half actually RAN, which is the one thing
the
offline suite structurally cannot tell us. Re-asserting adapter behaviour here would create the
second, drifting copy that parametrising one suite was meant to avoid.
"""

from __future__ import annotations

import datetime as dt
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE_FILE = _SERVICE_ROOT / "docker-compose.yml"
_ENDPOINT = os.environ.get("AQM_AWS_ENDPOINT_URL", "http://127.0.0.1:4566")
_BROKER_HOST = os.environ.get("AQM_MQTT_HOST", "127.0.0.1")
_BROKER_PORT = int(os.environ.get("AQM_MQTT_PORT", "1883"))
_T0 = dt.datetime(2026, 3, 1, 12, 0, tzinfo=dt.UTC)

_DUMMY_REGION = "eu-west-2"
_DUMMY_KEY = "test"
"""The emulator's literal credentials — deliberately not read from the environment.

Reading them from the environment is how a fenced check quietly acquires a dependency on a real
account: the variable is present on a developer's machine and absent in the fenced job, so the
test
passes locally against their real cloud and fails in CI for reasons nobody can reproduce.
"""


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """Whether a TCP port accepts a connection."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="module", autouse=True)
def _dummy_aws_environment() -> None:
    """Point boto3 at the emulator with literal dummy credentials.

    Set for the whole module rather than per test so no adapter can accidentally fall back to a
    real profile: boto3 searches the environment, then ~/.aws, so leaving these unset is what
    would let a check reach an account.
    """
    os.environ["AWS_ACCESS_KEY_ID"] = _DUMMY_KEY
    os.environ["AWS_SECRET_ACCESS_KEY"] = _DUMMY_KEY
    os.environ["AWS_DEFAULT_REGION"] = _DUMMY_REGION
    os.environ.pop("AWS_PROFILE", None)
    os.environ.pop("AWS_SESSION_TOKEN", None)


def _require_localstack() -> None:
    """Skip unless the store emulator is reachable."""
    host = _ENDPOINT.split("//", 1)[-1].split(":")[0]
    port = int(_ENDPOINT.rsplit(":", 1)[-1])
    if not _port_open(host, port):
        pytest.skip(f"no store emulator at {_ENDPOINT}; run `just up-ingestion` first")


def _require_broker() -> None:
    """Skip unless the local broker is reachable."""
    if not _port_open(_BROKER_HOST, _BROKER_PORT):
        pytest.skip(
            f"no broker at {_BROKER_HOST}:{_BROKER_PORT}; run `just up-ingestion` first"
        )


# --------------------------------------------------------------------------------------
# The store emulation (Requirements 14.9, 15.10, 16.5, 17.11)
# --------------------------------------------------------------------------------------


def _create_tables_and_bucket() -> None:
    """Provision the schema the adapters expect, keyed as Requirement 14.9 specifies."""
    import boto3
    from botocore.exceptions import ClientError

    dynamodb = boto3.client("dynamodb", endpoint_url=_ENDPOINT)
    tables = {
        # The composite key IS Requirement 14.9's layout: one site-species series per partition,
        # interval start as the sort key, so a window query is a range scan not a table scan.
        "aqm-readings": [("pk", "HASH"), ("sk", "RANGE")],
        "aqm-registry": [("site_code", "HASH")],
        "aqm-profiles": [("user_id", "HASH")],
        # Requirement 31's diary. The composite key is what implements 31.7 — one entry per user
        # per calendar date — and it is also what lets forget_user sweep the reserved
        # learned-threshold item along with the entries it was derived from.
        "aqm-symptoms": [("user_id", "HASH"), ("entry_date", "RANGE")],
    }
    for name, schema in tables.items():
        try:
            dynamodb.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": n, "KeyType": t} for n, t in schema],
                AttributeDefinitions=[
                    {"AttributeName": n, "AttributeType": "S"} for n, _ in schema
                ],
                BillingMode="PAY_PER_REQUEST",
            )
        except ClientError as error:
            if error.response["Error"]["Code"] != "ResourceInUseException":
                raise

    s3 = boto3.client("s3", endpoint_url=_ENDPOINT)
    try:
        s3.create_bucket(
            Bucket="aqm-raw",
            CreateBucketConfiguration={"LocationConstraint": _DUMMY_REGION},
        )
    except ClientError as error:
        already = ("BucketAlreadyOwnedByYou", "BucketAlreadyExists")
        if error.response["Error"]["Code"] not in already:
            raise

    for name in tables:
        dynamodb.get_waiter("table_exists").wait(TableName=name)


def test_the_shared_contract_suite_runs_its_cloud_half_against_the_emulator() -> None:
    """THE CENTRAL CHECK OF THIS FILE: prove the cloud parameters actually ran.

    The offline suite reports them as SKIPPED, and a skip is indistinguishable from a pass in a
    summary line — so without this the cloud adapters could be entirely broken and every run
    would
    still look green. This re-invokes the shared suite with the endpoint set and asserts a real
    number of cloud parameters PASSED, which is the assertion the offline run cannot make.

    THE SELECTOR IS DERIVED FROM THE SUITE'S OWN PARAMETER TABLES, not written by hand. My first
    version passed ``-k cloud`` on the assumption that was the parameter id; the real ids are
    ``dynamodb`` and ``s3``, so it deselected all 79 tests and the check failed for a reason
    that
    had nothing to do with the adapters. Reading the ids from the tables means renaming a
    parameter
    cannot silently empty this selection.

    THE TABLES ARE NOW DISCOVERED RATHER THAN LISTED, which closes a second hole in the same
    idea. The list used to be four names written out here, so adding a fifth parameter table —
    the SymptomLogStore's — left its cloud adapter uncovered by this check while every run still
    looked green. Deriving the tables by introspection means a table added later is swept in by
    default, the same "covered by default" shape the profile leak sweep and Property 39's
    provenance set use.
    """
    _require_localstack()
    _create_tables_and_bucket()

    from tests.contracts import test_port_contracts as suite

    adapter_tables = [
        value
        for name, value in vars(suite).items()
        if name.endswith("_ADAPTERS") and isinstance(value, tuple)
    ]
    assert len(adapter_tables) >= 5, (
        f"expected every *_ADAPTERS parameter table to be discovered, found "
        f"{len(adapter_tables)}"
    )
    cloud_ids = sorted(
        {
            name
            for table in adapter_tables
            for name, _factory in table
            if name != "memory"
        }
    )
    assert cloud_ids, "the suite declares no cloud adapters, so this check would be vacuous"
    selector = " or ".join(cloud_ids)

    completed = subprocess.run(
        [
            "python",
            "-m",
            "pytest",
            "tests/contracts/test_port_contracts.py",
            "-q",
            "-k",
            selector,
            "--no-header",
        ],
        cwd=_SERVICE_ROOT,
        env={**os.environ, "AQM_AWS_ENDPOINT_URL": _ENDPOINT},
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    output = completed.stdout + completed.stderr

    assert completed.returncode == 0, f"the cloud contract parameters failed:\n{output}"
    # A run that collected nothing also returns zero, so the count is asserted rather than the
    # exit status alone — the same "an empty result is not a pass" guard the log sweeps needed.
    assert " passed" in output, f"no cloud parameters ran:\n{output}"
    assert "skipped" not in output, (
        f"cloud parameters skipped even with an endpoint present:\n{output}"
    )


def test_a_reading_survives_a_round_trip_through_the_real_store() -> None:
    """One end-to-end write and read, proving the schema above matches the adapter's key layout.

    Distinct from the contract suite's assertions: this one exists to catch a MISMATCH between
    the
    table definition in this file and the keys the adapter writes, which the contract suite
    cannot
    see because it takes the schema as given.
    """
    _require_localstack()
    _create_tables_and_bucket()

    from aqm_ingestion.adapters.dynamodb import DynamoDbReadingsStore
    from aqm_ingestion.ports.clock import FixedClock
    from tests.contracts.test_port_contracts import _reading

    store = DynamoDbReadingsStore(
        table_name="aqm-readings", clock=FixedClock(_T0), endpoint_url=_ENDPOINT
    )
    reading = _reading("INTEG1")
    store.put(reading)

    assert store.get(reading.key) == reading


def test_the_symptom_log_key_layout_matches_the_adapter_and_fences_the_derivation() -> None:
    """Requirement 31.7's composite key, and the reserved sort key, against a real table.

    Distinct from the contract suite's assertions for the same reason the readings round trip
    is: the suite takes the schema as GIVEN, so it cannot see a mismatch between the table
    definition in this file and the keys the adapter writes.

    That matters more here than anywhere else, because the Learned_Thresholds share this table
    under the reserved sort key ``#learned``. The claim is that ``#`` (0x23) sorts before every
    ISO date (``0`` is 0x30), so a date-range query cannot reach the derivation while
    ``forget_user``'s sort-key-unconstrained query can. That is an assertion about how a REAL
    DynamoDB range query orders keys, and only a real table can settle it.
    """
    _require_localstack()
    _create_tables_and_bucket()

    from aqm_ingestion.adapters.dynamodb import DynamoDbSymptomLogStore
    from aqm_ingestion.domain.association import LearnedThreshold
    from aqm_ingestion.domain.symptoms import build_symptom_entry
    from aqm_ingestion.ports.clock import FixedClock

    store = DynamoDbSymptomLogStore(
        table_name="aqm-symptoms", clock=FixedClock(_T0), endpoint_url=_ENDPOINT
    )
    store.forget_user("integ-user")

    on = _T0.date()
    entry = build_symptom_entry(
        {
            "user_id": "integ-user",
            "entry_date": on,
            "severity": 4,
            "markers": ["wheeze"],
            "reliever_used": True,
            "note": "round trip",
        },
        now=_T0,
    )
    store.put(entry)
    store.put_learned_thresholds(
        "integ-user",
        (LearnedThreshold(species="PM25", sub_index=88, lag_days=3, observations=20),),
    )

    held = store.query_window("integ-user", on - dt.timedelta(days=400), on)
    assert len(held) == 1, "the reserved learned-threshold key leaked into a range query"
    assert held[0].note == "round trip"
    assert store.learned_thresholds("integ-user")["PM25"].sub_index == 88

    # The count excludes the reserved item; the sweep still removes it.
    assert store.forget_user("integ-user") == 1
    assert store.learned_thresholds("integ-user") == {}


def test_the_archive_round_trips_bytes_through_the_real_bucket() -> None:
    """Requirement 16.5's key layout against a real object store."""
    _require_localstack()
    _create_tables_and_bucket()

    from aqm_ingestion.adapters.s3 import S3RawArchive
    from aqm_ingestion.ports.protocols import ArchiveMeta

    archive = S3RawArchive(bucket="aqm-raw", endpoint_url=_ENDPOINT)
    # Deliberately unparseable: Requirement 16.1 archives before anything derives from the
    # bytes,
    # so the payloads worth keeping most are the ones that would fail a parse.
    payload = b"{not valid json at all"
    meta = ArchiveMeta(ingested_at=_T0, transport="mqtt", source="integration")

    identifier = archive.write(payload, meta)

    assert archive.read(identifier) == payload


# --------------------------------------------------------------------------------------
# The broker (Requirements 4.1, 4.6)
# --------------------------------------------------------------------------------------


def test_the_transport_connects_subscribes_and_delivers_a_message() -> None:
    """Broker connect-subscribe-ingest against a real broker.

    This is the check task 27.4 deliberately left here: its own tests could assert the adapter's
    SHAPE offline but not that a real broker accepts the subscription and delivers a payload.
    """
    _require_broker()

    import paho.mqtt.client as paho
    from paho.mqtt.enums import CallbackAPIVersion

    from aqm_ingestion.adapters.mqtt import DEFAULT_TOPIC_FILTER, SUBSCRIBE_QOS

    received: list[tuple[str, bytes]] = []

    consumer = paho.Client(
        client_id="aqm-integration-consumer",
        callback_api_version=CallbackAPIVersion.VERSION2,
    )
    consumer.on_message = lambda _c, _u, message: received.append(
        (message.topic, bytes(message.payload))
    )
    consumer.connect(_BROKER_HOST, _BROKER_PORT, keepalive=30)
    consumer.subscribe(DEFAULT_TOPIC_FILTER, qos=SUBSCRIBE_QOS)
    consumer.loop_start()

    publisher = paho.Client(
        client_id="aqm-integration-publisher",
        callback_api_version=CallbackAPIVersion.VERSION2,
    )
    publisher.connect(_BROKER_HOST, _BROKER_PORT, keepalive=30)
    publisher.loop_start()

    payload = b'{"SiteCode":"INTEG1","Species":"PM25"}'
    try:
        sent = publisher.publish("aqm/sensors/INTEG1/data", payload, qos=SUBSCRIBE_QOS)
        sent.wait_for_publish(timeout=10)
        deadline = time.monotonic() + 10
        while not received and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        publisher.loop_stop()
        publisher.disconnect()
        consumer.loop_stop()
        consumer.disconnect()

    assert received == [("aqm/sensors/INTEG1/data", payload)]


def test_the_default_topic_filter_matches_a_real_site_topic() -> None:
    """The wildcard really matches what a publisher sends, end to end through the broker.

    Asserted against a live broker because topic matching is the BROKER's rule, not ours: the
    anchoring argument recorded at task 16.1 (a multi-level wildcard would let `A/B` read as one
    site) is only proven by a broker actually applying it.
    """
    _require_broker()

    import paho.mqtt.client as paho
    from paho.mqtt.enums import CallbackAPIVersion

    from aqm_ingestion.adapters.mqtt import DEFAULT_TOPIC_FILTER, SUBSCRIBE_QOS

    matched: list[str] = []
    consumer = paho.Client(
        client_id="aqm-integration-filter",
        callback_api_version=CallbackAPIVersion.VERSION2,
    )
    consumer.on_message = lambda _c, _u, message: matched.append(message.topic)
    consumer.connect(_BROKER_HOST, _BROKER_PORT, keepalive=30)
    consumer.subscribe(DEFAULT_TOPIC_FILTER, qos=SUBSCRIBE_QOS)
    consumer.loop_start()

    publisher = paho.Client(
        client_id="aqm-integration-filter-pub",
        callback_api_version=CallbackAPIVersion.VERSION2,
    )
    publisher.connect(_BROKER_HOST, _BROKER_PORT, keepalive=30)
    publisher.loop_start()

    try:
        # Should match: one level in the wildcard position.
        publisher.publish("aqm/sensors/AQM1/data", b"{}", qos=1).wait_for_publish(timeout=10)
        # Should NOT match: an extra level past `data`.
        publisher.publish("aqm/sensors/AQM1/data/extra", b"{}", qos=1).wait_for_publish(
            timeout=10
        )
        deadline = time.monotonic() + 5
        while not matched and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.5)
    finally:
        publisher.loop_stop()
        publisher.disconnect()
        consumer.loop_stop()
        consumer.disconnect()

    assert matched == ["aqm/sensors/AQM1/data"]


# --------------------------------------------------------------------------------------
# The Compose smoke check (Requirement 28.8)
# --------------------------------------------------------------------------------------


def test_the_compose_definition_is_valid() -> None:
    """`docker compose config` parses and normalises the stack.

    A definition that a packaging test parses as YAML can still be rejected by Compose itself —
    an unknown key, an invalid healthcheck shape, a bad depends_on condition — so the engine's
    own
    validator is the authority here.
    """
    docker = os.environ.get("DOCKER", "docker")
    probe = subprocess.run(
        [docker, "info"], capture_output=True, text=True, timeout=60, check=False
    )
    if probe.returncode != 0:
        pytest.skip("no container engine available")

    completed = subprocess.run(
        [docker, "compose", "-f", str(_COMPOSE_FILE), "config"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, f"compose rejected the definition:\n{completed.stderr}"
    # Prove the normalised output really describes the stack, so a silently empty config cannot
    # pass: Compose exits zero for a file defining no services at all.
    for service in ("ingestion", "mosquitto", "localstack"):
        assert service in completed.stdout

# --------------------------------------------------------------------------------------
# Personal diary memory (Phase D, tasks 11-13)
#
# WHAT THIS BLOCK ADDS AND WHY IT IS HERE. The offline job and serving tests
# (test_association_job, test_serving_diary, test_escalation) already pin the BEHAVIOUR against
# in-memory stores. What they take as given is that the profile, diary, association and erasure
# reads and writes traverse the REAL DynamoDB key layout — the profile partition key, the diary
# composite key, the reserved ``#learned`` sort key, the readings ``SITE#..#SP#..`` composite
# key. Only a real table settles that the derivation's handoff to the serving path works over
# that layout end to end. So these checks exercise profile+diary together, the association over
# a persisted exposure history, and a total erasure — none of which the round trips above this
# line touch — and each proves a cross-user isolation the offline suite can only assert against
# a dict.
# --------------------------------------------------------------------------------------

_USER_A = "diary-user-a"
_USER_B = "diary-user-b"
_TABLE_ID = "epa-2024-05-06"


def _profile_fields(user_id: str, **overrides: object) -> dict[str, object]:
    """A lawful profile payload for ``build_profile``, homed at the demo Cochabamba site.

    The home coordinate matches ``seed_readings.DEMO_SITES`` (near ``-17.394, -66.157``) so the
    same ``GeoSelector`` the serving path uses resolves the seeded sites for this user — which
    is what lets the association in task 12 reach the seeded exposure history.
    """
    from aqm_ingestion.domain.profile import (
        RECOGNIZED_CONSENT_VERSIONS,
        Condition,
        SensitivityLevel,
    )

    base: dict[str, object] = {
        "user_id": user_id,
        "condition": Condition.ASTHMA,
        "sensitivity_level": SensitivityLevel.STANDARD,
        "locations": [{"name": "home", "latitude": -17.394, "longitude": -66.157}],
        "consent": {
            "version": next(iter(sorted(RECOGNIZED_CONSENT_VERSIONS))),
            "given_at": _T0,
        },
        "created_at": _T0,
        "updated_at": _T0,
    }
    return base | overrides


def _diary_fields(on: dt.date, severity: int, **overrides: object) -> dict[str, object]:
    """A lawful diary payload for ``build_symptom_entry``."""
    base: dict[str, object] = {
        "entry_date": on,
        "severity": severity,
        "markers": ["cough", "wheeze"],
        "reliever_used": True,
    }
    return base | overrides


def test_a_profile_and_diary_round_trip_and_isolate_by_user() -> None:
    """Profile + diary together over the real store, and Property 1 isolation (task 11).

    Requirements 3.2, 3.3, 4.2, 9.2. Distinct from the readings/diary round trips above: this is
    the FIRST check to exercise a profile and a diary through their adapters at once, the
    same-date REPLACE (Req 3.3 / 31.7) through the real composite key, and that one user's reads
    never see another's (Req 9.2, Property 1) — which a dict-backed store cannot prove about the
    real ``user_id`` partition key.
    """
    _require_localstack()
    _create_tables_and_bucket()

    from aqm_ingestion.adapters.dynamodb import (
        DynamoDbProfileStore,
        DynamoDbSymptomLogStore,
    )
    from aqm_ingestion.domain.profile import build_profile
    from aqm_ingestion.domain.symptoms import build_symptom_entry
    from aqm_ingestion.ports.clock import FixedClock

    clock = FixedClock(_T0)
    profiles = DynamoDbProfileStore(table_name="aqm-profiles", endpoint_url=_ENDPOINT)
    diary = DynamoDbSymptomLogStore(
        table_name="aqm-symptoms", clock=clock, endpoint_url=_ENDPOINT
    )
    # A clean slate for the two users this check owns, so a prior run's rows cannot mask a bug.
    for user in (_USER_A, _USER_B):
        profiles.delete(user)
        diary.forget_user(user)

    on = _T0.date()

    # --- user A: profile + diary written and read back ---
    profile_a = build_profile(_profile_fields(_USER_A))
    profiles.put(profile_a)
    entry_a = build_symptom_entry(
        _diary_fields(on, severity=3, note="round trip a"), now=_T0
    )
    diary.put(entry_a)

    read_profile_a = profiles.get(_USER_A)
    assert read_profile_a is not None
    assert read_profile_a.user_id == _USER_A
    assert read_profile_a.condition == profile_a.condition
    assert read_profile_a.sensitivity_level == profile_a.sensitivity_level
    assert read_profile_a.locations == profile_a.locations

    held_a = diary.query_window(_USER_A, on, on)
    assert len(held_a) == 1
    assert held_a[0].severity == 3
    assert held_a[0].note == "round trip a"

    # --- Req 3.3 / 31.7: a second write for the SAME date replaces, not accumulates ---
    diary.put(build_symptom_entry(_diary_fields(on, severity=5, note="replaced"), now=_T0))
    replaced = diary.query_window(_USER_A, on, on)
    assert len(replaced) == 1, "a second entry for the same date must replace the first"
    assert replaced[0].severity == 5
    assert replaced[0].note == "replaced"

    # --- user B: written, and each user's reads see only their own (Property 1) ---
    profiles.put(build_profile(_profile_fields(_USER_B)))
    diary.put(build_symptom_entry(_diary_fields(on, severity=2, note="b only"), now=_T0))

    b_profile = profiles.get(_USER_B)
    assert b_profile is not None and b_profile.user_id == _USER_B

    a_after_b = diary.query_window(_USER_A, on, on)
    assert len(a_after_b) == 1 and a_after_b[0].note == "replaced", (
        "user B's write must not appear in user A's diary"
    )
    b_diary = diary.query_window(_USER_B, on, on)
    assert len(b_diary) == 1 and b_diary[0].note == "b only", (
        "user A's writes must not appear in user B's diary"
    )


def test_the_association_derives_a_threshold_the_serving_path_then_applies() -> None:
    """The association end to end over the real store, and its serving-path handoff (task 12).

    Requirements 4.3, 4.4, 4.5. Seeds a persisted exposure history through the registry +
    readings adapters (``seed_exposure_history``), writes a diary that correlates with an
    ELEVATED exposure on the seeded sites, runs the ``AssociationJob`` assembled exactly as
    ``build_association_job`` does (same ``GeoSelector`` for ``sites_for``), and asserts a
    Learned_Threshold is written and
    then READ back through the serving handoff — ``learned_thresholds`` and
    ``resolve_escalation`` returning ``ThresholdSource.LEARNED``. A history-less user gets no
    threshold and stays at ``SENSITIVITY_LEVEL`` (Req 4.5 fallback, Property 4 isolation).

    The point of doing this over LocalStack rather than in-memory: it proves the derivation's
    reads and writes traverse the REAL key layout — the ``#learned`` reserved sort key and the
    readings composite key — which the offline job tests take as given.
    """
    _require_localstack()
    _create_tables_and_bucket()

    from aqm_ingestion.adapters.dynamodb import (
        DynamoDbProfileStore,
        DynamoDbReadingsStore,
        DynamoDbSensorRegistryStore,
        DynamoDbSymptomLogStore,
    )
    from aqm_ingestion.domain.aqi.breakpoints import BreakpointTableRegistry
    from aqm_ingestion.domain.association import AssociationLimits
    from aqm_ingestion.domain.escalation import ThresholdSource, resolve_escalation
    from aqm_ingestion.domain.models import (
        CalibratedReading,
        Confidence,
        DedupKey,
        QualityFlag,
    )
    from aqm_ingestion.domain.profile import UserProfile, build_profile
    from aqm_ingestion.domain.symptoms import build_symptom_entry
    from aqm_ingestion.jobs.association import AssociationJob
    from aqm_ingestion.jobs.seed_readings import DEMO_SITES, seed_exposure_history
    from aqm_ingestion.ports.clock import FixedClock
    from aqm_ingestion.serving.geo import GeoSelector, SelectionSettings

    clock = FixedClock(_T0)
    registry = DynamoDbSensorRegistryStore(table_name="aqm-registry", endpoint_url=_ENDPOINT)
    readings = DynamoDbReadingsStore(
        table_name="aqm-readings", clock=clock, endpoint_url=_ENDPOINT
    )
    profiles = DynamoDbProfileStore(table_name="aqm-profiles", endpoint_url=_ENDPOINT)
    diary = DynamoDbSymptomLogStore(
        table_name="aqm-symptoms", clock=clock, endpoint_url=_ENDPOINT
    )

    historied = "assoc-user-history"
    historyless = "assoc-user-empty"
    for user in (historied, historyless):
        profiles.delete(user)
        diary.forget_user(user)

    # A persisted exposure history for the demo sites, written through the real store ports.
    seed_exposure_history(registry=registry, readings=readings, clock=clock, seed=7)

    # Correlate the diary with exposure by writing an elevated PM25 reading on each diary day,
    # tracking the day's severity. The association takes each day's MAXIMUM Sub_Index, so these
    # dominate the seeded background and give a clean same-day relationship to correlate on.
    demo_site = DEMO_SITES[0].site_code
    days = 20
    today = _T0.date()
    for offset in range(days):
        on = today - dt.timedelta(days=offset)
        severity = 1 + (offset % 5)
        diary.put(
            build_symptom_entry(
                {
                    "user_id": historied,
                    **_diary_fields(on, severity=severity, markers=[], reliever_used=False),
                },
                now=_T0,
            )
        )
        sub_index = 60 + 30 * (offset % 5)
        at = dt.datetime.combine(on, dt.time(9), tzinfo=dt.UTC)
        readings.put(
            CalibratedReading(
                key=DedupKey(
                    site_code=demo_site,
                    species="PM25",
                    interval_start=at,
                    duration="PT1H",
                ),
                reported_value=float(sub_index),
                corrected_value=float(sub_index),
                units="ug.m-3",
                quality_flag=QualityFlag.CALIBRATED,
                confidence=Confidence.HIGH,
                calibration_strategy="rh_linear",
                breakpoint_table=_TABLE_ID,
                ratification_status="R",
                ingested_at=at,
                archive_id=f"assoc-{demo_site}-PM25-{on.isoformat()}",
                sub_index=sub_index,
                band="Moderate",
            )
        )

    profiles.put(build_profile(_profile_fields(historied)))
    profiles.put(build_profile(_profile_fields(historyless)))

    # The job, assembled exactly as build_association_job does: sites come from the SAME
    # GeoSelector the serving path uses, so the learned threshold rests on the sites a response
    # would name. A wide radius so the Cochabamba home resolves the seeded sites.
    selector = GeoSelector(
        registry=registry,
        readings=readings,
        clock=clock,
        settings=SelectionSettings(radius_km=50.0),
    )

    def sites_for(profile: UserProfile) -> tuple[str, ...]:
        return tuple(site.site_code for site in selector.select(profile).sites)

    # Guard the premise: if the selector resolves no seeded site for the home, the association
    # could write nothing for a reason that has nothing to do with the derivation.
    profile_historied = profiles.get(historied)
    assert profile_historied is not None
    assert sites_for(profile_historied) != (), (
        "the demo home must resolve a seeded site, or the correlation has no exposure"
    )

    job = AssociationJob(
        symptoms=diary,
        readings=readings,
        profiles=profiles,
        clock=clock,
        sites_for=sites_for,
        limits=AssociationLimits(lags=(0,), min_observations=5, min_strength=0.1),
    )
    outcome = job.run_for(historied)
    assert outcome.thresholds_written >= 1, (
        "the correlated diary should clear the association bar"
    )

    # --- Req 4.4: read the derivation back through the serving handoff ---
    learned = diary.learned_thresholds(historied)
    assert "PM25" in learned, (
        "the Learned_Threshold must survive the reserved #learned sort key"
    )
    derived = learned["PM25"]

    registry_tables = BreakpointTableRegistry.with_defaults()
    effective = resolve_escalation(
        profile_historied,
        species="PM25",
        registry=registry_tables,
        table_id=_TABLE_ID,
        learned=learned,
    )
    assert effective.source is ThresholdSource.LEARNED
    assert effective.sub_index == derived.sub_index

    # --- Req 4.5 / Property 4: a history-less user gets no threshold and stays at the level ---
    empty_outcome = job.run_for(historyless)
    assert empty_outcome.thresholds_written == 0
    assert diary.learned_thresholds(historyless) == {}
    fallback = resolve_escalation(
        profiles.get(historyless),
        species="PM25",
        registry=registry_tables,
        table_id=_TABLE_ID,
        learned=diary.learned_thresholds(historyless),
    )
    assert fallback.source is ThresholdSource.SENSITIVITY_LEVEL
    # The derivation for one user must not have reached the other.
    assert diary.learned_thresholds(historyless) == {}


def test_forgetting_a_user_erases_profile_diary_and_thresholds() -> None:
    """Total erasure over the real store, and Property 5 isolation (task 13).

    Requirements 6.1, 6.2, 6.3, 6.4, 5.6. Writes a profile, a diary and a Learned_Threshold for
    a user, forgets them the way the serving erasure path does (``DynamoDbProfileStore.delete``
    + ``DynamoDbSymptomLogStore.forget_user``, which sweeps the reserved ``#learned`` item with
    the entries), and asserts all three are gone and a later profile read serves the DEFAULT
    (None, so the serving path falls back). One user's erasure leaves another's data intact
    (Property 5).
    """
    _require_localstack()
    _create_tables_and_bucket()

    from aqm_ingestion.adapters.dynamodb import (
        DynamoDbProfileStore,
        DynamoDbSymptomLogStore,
    )
    from aqm_ingestion.domain.association import LearnedThreshold
    from aqm_ingestion.domain.profile import build_profile
    from aqm_ingestion.domain.symptoms import build_symptom_entry
    from aqm_ingestion.ports.clock import FixedClock

    clock = FixedClock(_T0)
    profiles = DynamoDbProfileStore(table_name="aqm-profiles", endpoint_url=_ENDPOINT)
    diary = DynamoDbSymptomLogStore(
        table_name="aqm-symptoms", clock=clock, endpoint_url=_ENDPOINT
    )

    forgotten = "erase-user-a"
    kept = "erase-user-b"
    for user in (forgotten, kept):
        profiles.delete(user)
        diary.forget_user(user)

    on = _T0.date()
    learned = (LearnedThreshold(species="PM25", sub_index=88, lag_days=3, observations=20),)

    # Both users get a profile, a diary entry, and a learned threshold.
    for user in (forgotten, kept):
        profiles.put(build_profile(_profile_fields(user)))
        diary.put(
            build_symptom_entry(
                {"user_id": user, **_diary_fields(on, severity=4)}, now=_T0
            )
        )
        diary.put_learned_thresholds(user, learned)

    # --- forget the way the serving erasure path does: delete profile, forget the diary ---
    profiles.delete(forgotten)
    diary.forget_user(forgotten)

    # --- Req 6.1/6.2/6.3: the profile, diary, and threshold are all gone ---
    assert profiles.get(forgotten) is None
    assert diary.query_window(forgotten, on, on) == ()
    assert diary.learned_thresholds(forgotten) == {}

    # --- Req 6.4: a later read serves the default — get returns None, so the fallback applies.
    assert profiles.get(forgotten) is None

    # --- Property 5 / Req 5.6: the other user's data is untouched ---
    kept_profile = profiles.get(kept)
    assert kept_profile is not None and kept_profile.user_id == kept
    kept_diary = diary.query_window(kept, on, on)
    assert len(kept_diary) == 1 and kept_diary[0].severity == 4
    assert diary.learned_thresholds(kept)["PM25"].sub_index == 88
