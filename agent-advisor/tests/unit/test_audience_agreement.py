"""Req 32.14: the inbound authorizer's audience must be Service 2's audience (task 17.4).

**WHAT THIS TEST HAD TO BECOME, AND WHY.** The obvious test compares two configured values. It
cannot
work: the advisor's audience and Service 2's are resolved from the environment in two separately
deployed processes, so an offline test has no runtime value to compare and asserting the
defaults
match would prove nothing about a deployment.

What IS assertable offline — and is strictly stronger — is that BOTH SERVICES READ THE AUDIENCE
FROM
THE SAME ENVIRONMENT VARIABLE. Then one value configures both and they cannot drift apart, which
is
the property Req 32.14 actually wants. Comparing values would catch drift after it happened;
sharing
the variable makes drift impossible to express.

Before this, the advisor read `AQM_ADVISOR_JWT_ALLOWED_AUDIENCE` while Service 2 read
`AQM_COGNITO_CLIENT_ID`. Two independent names for one Cognito app client, with nothing linking
them —
exactly the silent divergence Req 32.14 describes, where "every retrieval fails authorization at
Service 2 rather than here, which is the hardest place to attribute it".

Service 2's side is read FROM DISK rather than imported: the engineering practices forbid
importing
across service directories until a shared contract package is specced, and the advisor's own
adapter
tests already read Service 2's `models.py` from disk the same way. Reading beats hard-coding the
name
here, because a rename in Service 2 must fail this test rather than pass it.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from aqm_advisor.config.loader import _SCALARS

_REPO = pathlib.Path(__file__).resolve().parents[3]
_SERVICE2_COMPOSITION = (
    _REPO / "data-processing" / "src" / "aqm_ingestion" / "composition.py"
)


def _service2_audience_variable() -> str:
    """The env var Service 2's Cognito verifier takes its audience from.

    Parsed from the AST rather than grepped, so a mention in a comment or a docstring cannot be
    mistaken for the wiring. Looks for the `client_id=` keyword on the call that builds the
    verifier:
    for a Cognito JWT the accepted audience IS the app client id, which is why that one value is
    both
    Req 32.7's "permitted client identifier" and its "permitted audience".
    """
    tree = ast.parse(_SERVICE2_COMPOSITION.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg != "client_id":
                continue
            for inner in ast.walk(keyword.value):
                if (
                    isinstance(inner, ast.Constant)
                    and isinstance(inner.value, str)
                    and inner.value.startswith("AQM_")
                    and "COGNITO" in inner.value
                ):
                    return inner.value
    pytest.fail(
        f"could not find Service 2's Cognito audience variable in {_SERVICE2_COMPOSITION}; "
        "if its wiring moved, this test must be retargeted rather than deleted"
    )


def test_service_2s_audience_variable_is_discoverable() -> None:
    # Non-vacuity. If the parse silently found nothing, every assertion below would be comparing
    # against an empty string and would pass while proving nothing.
    assert _service2_audience_variable() == "AQM_COGNITO_CLIENT_ID"


def test_the_advisor_reads_its_audience_from_service_2s_variable() -> None:
    # THE Req 32.14 assertion. One variable names the Cognito app client, and both services read
    # it,
    # so the two authorizers cannot be configured with different audiences.
    advisor_variable, _default = _SCALARS["jwt_allowed_audience"]
    assert advisor_variable == _service2_audience_variable()


def test_the_audience_variable_is_a_deliberate_exception_to_the_advisor_prefix() -> None:
    # Every other advisor setting is `AQM_ADVISOR_*`. This one deliberately is not, because it
    # does
    # not describe the advisor: it names a Cognito app client that two services share. Pinned so
    # the
    # exception reads as a decision rather than an oversight, and so a well-meaning tidy-up that
    # "fixes" the prefix fails here with the reason attached.
    advisor_variable, _default = _SCALARS["jwt_allowed_audience"]
    assert not advisor_variable.startswith("AQM_ADVISOR_"), (
        "the audience must stay on the shared Cognito variable; an AQM_ADVISOR_ name "
        "would let the two services' audiences drift, which Req 32.14 forbids"
    )


def test_every_other_advisor_setting_keeps_the_prefix() -> None:
    # The other side of that exception: exactly one setting is exempt. Without this, the test
    # above
    # would license dropping the prefix anywhere.
    exempt = {"jwt_allowed_audience"}
    offenders = {
        key: variable
        for key, (variable, _default) in _SCALARS.items()
        if key not in exempt and not variable.startswith("AQM_ADVISOR_")
    }
    assert not offenders, offenders
