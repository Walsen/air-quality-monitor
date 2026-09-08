"""Config resolution precedence property test (task 6.5).

Feature: sensor-simulator-service, Property 41 — Configuration resolution
precedence. Validates Requirements 15.1, 15.3, 15.11: environment beats file
beats documented default, and a supplied profile override is retained.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from aqm_simulator.config.loader import resolve_config

_DEFAULT_SWARM = 50


@given(
    env_val=st.none() | st.integers(min_value=1, max_value=500),
    file_val=st.none() | st.integers(min_value=1, max_value=500),
)
def test_property_41_precedence(env_val: int | None, file_val: int | None) -> None:
    """Feature: sensor-simulator-service, Property 41.

    For any combination of an env value, a file value, and the default, the
    resolved swarm_size is the env value if present, else the file value if
    present, else the documented default.
    """
    env = {} if env_val is None else {"AQM_SWARM_SIZE": str(env_val)}
    file_data = None if file_val is None else {"swarm_size": file_val}

    cfg = resolve_config(env=env, file_data=file_data)

    if env_val is not None:
        expected = env_val
    elif file_val is not None:
        expected = file_val
    else:
        expected = _DEFAULT_SWARM
    assert cfg.swarm_size == expected


@given(
    override=st.dictionaries(
        keys=st.sampled_from(["sponsor_name", "sensor_contract", "timezone"]),
        values=st.text(min_size=1, max_size=12),
        max_size=3,
    )
)
def test_property_41_profile_override_retained(override: dict[str, str]) -> None:
    """Feature: sensor-simulator-service, Property 41 (profile override)."""
    cfg = resolve_config(env={}, file_data={"profile_overrides": override})
    # The override is retained verbatim; unsupplied profile values are untouched
    # (applied against the named profile at swarm-build time).
    assert cfg.profile_overrides == override
    assert cfg.profile_name == "cochabamba"
