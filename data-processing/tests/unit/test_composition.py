"""Tests for the composition root and the adapter selection (task 29.1).

Requirements 26.1, 26.5, 27.1, 18.1, 18.7, 18.8.

THE GAP THIS TASK EXISTS TO CLOSE, found by reading the loader against the adapter tree: task
26's
loader PERMITS ``authenticator=cognito`` and lists it as a registered adapter, but no
Cognito adapter
existed — and PyJWT, pinned at task 27.1 for precisely this, was unused by any module. A
composition
root that resolves adapters BY NAME turns that from an invisible gap into an import error, which
is
the point of building one: the configuration surface and the implementations have to agree.

WHAT THE COMPOSITION ROOT IS FOR (§1 Dependency Inversion, and Requirement 27.1): the Clock and
every port are injected at ONE place. Nothing below it constructs an adapter or reads the wall
clock,
so the whole service can be stood up twice from the same configuration — which is what task
29.2's
determinism check needs and what no amount of per-module discipline would guarantee.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, cast

import pytest

from aqm_ingestion.config.loader import ServiceConfig
from aqm_ingestion.observability.logging import configure_logging
from aqm_ingestion.ports.clock import FixedClock

_T0 = dt.datetime(2026, 3, 1, 12, 0, tzinfo=dt.UTC)


def _events(captured: str) -> list[dict[str, Any]]:
    return [
        cast("dict[str, Any]", json.loads(line))
        for line in captured.strip().splitlines()
        if line
    ]


def _offline_env() -> dict[str, str]:
    """An environment selecting only local adapters, so no test reaches a network."""
    return {
        "AQM_ENABLE_SERVING": "true",
        "AQM_ENABLE_PUSH": "false",
        "AQM_ENABLE_PULL": "false",
    }


def _config(**overrides: str) -> ServiceConfig:
    from aqm_ingestion.config.loader import resolve_and_validate

    env = _offline_env()
    env.update(overrides)
    return resolve_and_validate(env, {}, credential_exists=lambda _path: True)


# --------------------------------------------------------------------------------------
# Adapter selection by name (Requirement 26.5, task 29.1)
# --------------------------------------------------------------------------------------


def test_every_registered_adapter_name_can_be_built() -> None:
    """The loader's registry and the composition root's factories must agree EXACTLY.

    Derived from the loader's own table rather than a list written here, so registering a name
    without providing a factory fails HERE instead of at the first deployment that selects it.
    That is the check that would have caught `cognito` being permitted with no implementation.
    """
    from aqm_ingestion.composition import ADAPTER_FACTORIES
    from aqm_ingestion.config.loader import _REGISTERED_ADAPTERS

    for port, names in _REGISTERED_ADAPTERS.items():
        assert port in ADAPTER_FACTORIES, f"no factories for port {port}"
        assert set(ADAPTER_FACTORIES[port]) == set(names), (
            f"{port}: loader permits {sorted(names)} but the root builds "
            f"{sorted(ADAPTER_FACTORIES[port])}"
        )


def test_adapter_selection_is_open_closed() -> None:
    """§1: a new adapter is a REGISTRY ENTRY, not another branch.

    Asserted the way task 7 asserted it for calibration strategies: the resolver's AST must not
    compare an adapter name literal, or "selected by name" would be a chain of ifs wearing a
    registry's clothes.
    """
    import ast
    import inspect

    from aqm_ingestion import composition

    tree = ast.parse(inspect.getsource(composition.build_runtime))
    compared: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for side in [node.left, *node.comparators]:
                if isinstance(side, ast.Constant) and isinstance(side.value, str):
                    compared.append(side.value)

    known = {name for names in _all_adapter_names() for name in names}
    leaked = sorted(set(compared) & known)
    assert not leaked, f"build_runtime compares adapter names directly: {leaked}"


def _all_adapter_names() -> list[tuple[str, ...]]:
    from aqm_ingestion.config.loader import _REGISTERED_ADAPTERS

    return list(_REGISTERED_ADAPTERS.values())


def test_the_open_closed_check_is_not_vacuous() -> None:
    # The guard task 18 needed: if getsource returned nothing the AST walk would pass trivially.
    import inspect

    from aqm_ingestion import composition

    assert "def build_runtime" in inspect.getsource(composition.build_runtime)


# --------------------------------------------------------------------------------------
# The runtime (Requirements 26.1, 26.5, 27.1)
# --------------------------------------------------------------------------------------


def test_the_runtime_builds_with_only_local_adapters() -> None:
    from aqm_ingestion.composition import build_runtime

    runtime = build_runtime(_config(), clock=FixedClock(_T0))

    assert runtime.app is not None
    assert runtime.pipeline is not None


def test_the_clock_is_injected_everywhere_not_read() -> None:
    """Requirement 27.1: the instant comes exclusively from the injected Clock.

    Asserted observably rather than by inspection: the runtime is built with a FIXED clock far
    from the present, and the value it reports must be that instant. A component that read the
    wall clock would report today.
    """
    from aqm_ingestion.composition import build_runtime

    runtime = build_runtime(_config(), clock=FixedClock(_T0))

    assert runtime.clock.now() == _T0


def test_the_resolved_configuration_is_logged_exactly_once(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Req 26.1: log the resolved NON-SECRET configuration ONCE at startup. Once, because a
    # per-component log would repeat it and bury the startup summary.
    from aqm_ingestion.composition import build_runtime

    configure_logging("info")
    build_runtime(_config(), clock=FixedClock(_T0))

    summaries = [e for e in _events(capsys.readouterr().out) if e["event"] == "config_resolved"]
    assert len(summaries) == 1


def test_the_startup_log_carries_no_credential_path(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Req 26.11 via ServiceConfig.redacted(): a path is not a secret but it IS a map to one, so
    # the summary reports WHETHER a credential resolved.
    from aqm_ingestion.composition import build_runtime

    configure_logging("info")
    build_runtime(
        _config(AQM_FEED_CREDENTIAL_PATH="/run/secrets/feed-key"), clock=FixedClock(_T0)
    )

    assert "/run/secrets/feed-key" not in capsys.readouterr().out


def test_only_the_enabled_interfaces_are_built() -> None:
    # Req 26.10: the three switches select what is wired. A serving-only deployment must not
    # construct an MQTT subscription, which would need a broker that is not there.
    from aqm_ingestion.composition import build_runtime

    serving_only = build_runtime(_config(), clock=FixedClock(_T0))
    assert serving_only.app is not None
    assert serving_only.mqtt_entry is None
    assert serving_only.feed_entry is None

    push_only = build_runtime(
        _config(AQM_ENABLE_SERVING="false", AQM_ENABLE_PUSH="true"), clock=FixedClock(_T0)
    )
    assert push_only.app is None
    assert push_only.mqtt_entry is not None


def test_nothing_is_constructed_before_the_configuration_validates() -> None:
    """Requirement 26.5: validate before opening a listener, subscribing, or a store call.

    A rejected configuration must raise from RESOLUTION, so there is no half-built runtime to
    clean up — the fail-fast rule §5 states as "never half-start with a partly valid config".
    """
    from aqm_ingestion.config.loader import ConfigError

    with pytest.raises(ConfigError):
        _config(AQM_ADAPTER_READINGS_STORE="not-a-registered-adapter")


def test_main_exits_non_zero_on_a_rejected_configuration(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # §5: fail fast, exit non-zero, one message per invalid value, never half-start.
    from aqm_ingestion.composition import main

    code = main(
        env={**_offline_env(), "AQM_ADAPTER_READINGS_STORE": "nope"},
        clock=FixedClock(_T0),
        serve=False,
    )

    assert code == 1
    events = _events(capsys.readouterr().out)
    errors = [e for e in events if e["level"] in ("error", "critical")]
    assert errors, "a startup failure must be logged (§6)"


def test_main_returns_zero_when_wiring_succeeds_without_serving() -> None:
    # serve=False so the composition root is testable WITHOUT binding a port, the split Service
    # 1
    # settled on at its own task 20.6.
    from aqm_ingestion.composition import main

    assert main(env=_offline_env(), clock=FixedClock(_T0), serve=False) == 0


# --------------------------------------------------------------------------------------
# The Cognito authenticator (Requirements 18.1, 18.7, 18.8)
# --------------------------------------------------------------------------------------


def test_the_cognito_verifier_satisfies_the_authenticator_port() -> None:
    from aqm_ingestion.adapters.auth import CognitoAuthenticator
    from aqm_ingestion.ports.protocols import Authenticator

    verifier = CognitoAuthenticator(
        user_pool_id="eu-west-2_example",
        client_id="example-client",
        region="eu-west-2",
        key_resolver=lambda _token: None,
    )
    assert isinstance(verifier, Authenticator)


def test_the_verified_identity_carries_only_the_user_id() -> None:
    """Requirement 18.7: no claim beyond the identity reaches a response, log, or audit.

    THE CLAIM SET IS VERIFIED INSIDE THE ADAPTER AND DISCARDED. Requirement 18.1 says the gate
    establishes "the identity AND the claim set", which reads like both should travel — but 18.7
    forbids any claim beyond the identity leaving, so the only shape satisfying both is to check
    the claims where they arrive and hand on the identity alone. Asserted structurally: the
    dataclass has exactly one field.
    """
    import dataclasses

    from aqm_ingestion.ports.protocols import VerifiedIdentity

    fields = {field.name for field in dataclasses.fields(VerifiedIdentity)}
    assert fields == {"user_id"}


def test_a_token_for_the_wrong_audience_is_refused() -> None:
    """Req 18.3's named condition, and the category task 23 added for it.

    THE EXPIRY MUST BE IN THE FUTURE for this test to test what it says. My first version
    stamped
    ``exp`` from the fixed test instant, which is in the past relative to a real run — PyJWT
    checks
    expiry BEFORE audience, so the token was refused as EXPIRED and the assertion failed. The
    ordering is a property of the verifier, not something to work around: to observe the
    audience
    rule the token has to be valid in every other respect.
    """
    import jwt

    from aqm_ingestion.adapters.auth import CognitoAuthenticator
    from aqm_ingestion.ports.protocols import AuthRejectedError, RejectionCategory

    secret = "test-signing-key-not-a-real-credential"
    token = jwt.encode(
        {
            "sub": "user-1",
            "aud": "a-different-client",
            "iss": "https://cognito-idp.eu-west-2.amazonaws.com/eu-west-2_example",
            "token_use": "id",
            "exp": int((dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).timestamp()),
        },
        secret,
        algorithm="HS256",
    )

    verifier = CognitoAuthenticator(
        user_pool_id="eu-west-2_example",
        client_id="example-client",
        region="eu-west-2",
        key_resolver=lambda _token: secret,
        algorithms=("HS256",),
    )

    with pytest.raises(AuthRejectedError) as raised:
        verifier.verify(token)
    assert raised.value.category is RejectionCategory.WRONG_AUDIENCE


def test_an_expired_token_is_refused_as_expired() -> None:
    # The counterpart, so the ordering noted above is pinned rather than merely worked around:
    # a token that IS expired reports EXPIRED, which is Req 18.3's other named condition.
    import jwt

    from aqm_ingestion.adapters.auth import CognitoAuthenticator
    from aqm_ingestion.ports.protocols import AuthRejectedError, RejectionCategory

    secret = "test-signing-key-not-a-real-credential"
    token = jwt.encode(
        {
            "sub": "user-1",
            "aud": "example-client",
            "iss": "https://cognito-idp.eu-west-2.amazonaws.com/eu-west-2_example",
            "token_use": "id",
            "exp": int((dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).timestamp()),
        },
        secret,
        algorithm="HS256",
    )

    verifier = CognitoAuthenticator(
        user_pool_id="eu-west-2_example",
        client_id="example-client",
        region="eu-west-2",
        key_resolver=lambda _token: secret,
        algorithms=("HS256",),
    )

    with pytest.raises(AuthRejectedError) as raised:
        verifier.verify(token)
    assert raised.value.category is RejectionCategory.EXPIRED


def test_an_access_token_is_refused_even_though_it_verifies() -> None:
    # Cognito issues access and identity tokens from the SAME pool with the same signature and
    # issuer, so only `token_use` tells them apart. Requirement 18.2 authenticates a USER, so an
    # access token must be refused — and without this check it would sail through every other
    # rule.
    import jwt

    from aqm_ingestion.adapters.auth import CognitoAuthenticator
    from aqm_ingestion.ports.protocols import AuthRejectedError

    secret = "test-signing-key-not-a-real-credential"
    token = jwt.encode(
        {
            "sub": "user-1",
            "aud": "example-client",
            "iss": "https://cognito-idp.eu-west-2.amazonaws.com/eu-west-2_example",
            "token_use": "access",
            "exp": int((dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).timestamp()),
        },
        secret,
        algorithm="HS256",
    )

    verifier = CognitoAuthenticator(
        user_pool_id="eu-west-2_example",
        client_id="example-client",
        region="eu-west-2",
        key_resolver=lambda _token: secret,
        algorithms=("HS256",),
    )

    with pytest.raises(AuthRejectedError):
        verifier.verify(token)


def test_a_valid_token_yields_the_subject_as_the_identity() -> None:
    import jwt

    from aqm_ingestion.adapters.auth import CognitoAuthenticator

    secret = "test-signing-key-not-a-real-credential"
    token = jwt.encode(
        {
            "sub": "user-1",
            "aud": "example-client",
            "iss": "https://cognito-idp.eu-west-2.amazonaws.com/eu-west-2_example",
            "token_use": "id",
            "exp": int((dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).timestamp()),
        },
        secret,
        algorithm="HS256",
    )

    verifier = CognitoAuthenticator(
        user_pool_id="eu-west-2_example",
        client_id="example-client",
        region="eu-west-2",
        key_resolver=lambda _token: secret,
        algorithms=("HS256",),
    )

    assert verifier.verify(token).user_id == "user-1"


def test_a_rejection_never_carries_the_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # §7 and Req 18.7: a bearer token IS the credential, so neither the log nor the raised error
    # may echo it. Asserted over the whole emitted JSON, the surface task 27.4 learned to use.
    from aqm_ingestion.adapters.auth import CognitoAuthenticator
    from aqm_ingestion.ports.protocols import AuthRejectedError

    configure_logging("debug")
    token = "eyJhbGciOiJIUzI1NiJ9.dGhpcy1pcy1ub3QtYS1yZWFsLXRva2Vu.signature"
    verifier = CognitoAuthenticator(
        user_pool_id="eu-west-2_example",
        client_id="example-client",
        region="eu-west-2",
        key_resolver=lambda _token: "irrelevant",
        algorithms=("HS256",),
    )

    with pytest.raises(AuthRejectedError) as raised:
        verifier.verify(token)

    captured = capsys.readouterr().out
    assert token not in captured
    assert token not in str(raised.value)
    # Non-vacuity: the rejection must actually have been logged (§6).
    assert any(e["event"] == "auth_rejected" for e in _events(captured))
