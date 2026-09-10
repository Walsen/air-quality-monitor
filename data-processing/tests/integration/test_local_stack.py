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
    """
    _require_localstack()
    _create_tables_and_bucket()

    from tests.contracts import test_port_contracts as suite

    cloud_ids = sorted(
        {
            name
            for table in (
                suite._READINGS_ADAPTERS,
                suite._REGISTRY_ADAPTERS,
                suite._PROFILE_ADAPTERS,
                suite._ARCHIVE_ADAPTERS,
            )
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
