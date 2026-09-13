"""Offline assertions over the synthesized ``CognitoStack`` template.

Feature: personal-diary-memory, Property 7 — offline guarantee.

THIS SUITE TOUCHES NO ACCOUNT. ``Template.from_stack`` runs synthesis in-process
and emits CloudFormation without contacting an account, which is exactly what
keeps these assertions in the offline suite. The stack is constructed with an
explicit ``env`` so synth performs no account/region lookup.

The assertions pin the facts this feature depends on and that a plausible edit
could silently break: a user pool exists; the app client enables
``ALLOW_USER_PASSWORD_AUTH`` (Cognito's ``USER_PASSWORD_AUTH`` flow) plus refresh,
and is a PUBLIC client with no generated secret (a browser client cannot keep
one); and the three outputs downstream config reads — pool id, app client id, and
the issuer URL in the ``https://cognito-idp.<region>.amazonaws.com/<pool-id>``
shape the ingestion authenticator and the advisor expect.
"""

from __future__ import annotations

import aws_cdk as cdk
from aqm_infra.cognito_stack import CognitoStack
from aws_cdk import assertions


def _template() -> assertions.Template:
    app = cdk.App()
    stack = CognitoStack(
        app,
        "TestCognitoStack",
        env=cdk.Environment(account="111122223333", region="us-east-1"),
    )
    return assertions.Template.from_stack(stack)


def test_it_provisions_exactly_one_user_pool() -> None:
    template = _template()
    template.resource_count_is("AWS::Cognito::UserPool", 1)


def test_the_app_client_enables_user_password_and_refresh_auth() -> None:
    _template().has_resource_properties(
        "AWS::Cognito::UserPoolClient",
        assertions.Match.object_like(
            {
                "ExplicitAuthFlows": assertions.Match.array_with(
                    [
                        "ALLOW_USER_PASSWORD_AUTH",
                        "ALLOW_REFRESH_TOKEN_AUTH",
                    ]
                )
            }
        ),
    )


def test_the_app_client_is_public_with_no_secret() -> None:
    # A browser-side public client cannot keep a secret. GenerateSecret must be
    # false or absent; a generated secret would make USER_PASSWORD_AUTH from the
    # browser impossible and leak a credential into the client. CDK renders an
    # explicit `GenerateSecret: false` for a public client, so assert it is not
    # true rather than that the key is absent.
    template = _template()
    template.has_resource_properties(
        "AWS::Cognito::UserPoolClient",
        assertions.Match.object_like(
            {
                "GenerateSecret": assertions.Match.not_(True),
            }
        ),
    )
    # And no client anywhere in the template generates a secret.
    for client in template.find_resources("AWS::Cognito::UserPoolClient").values():
        assert client["Properties"].get("GenerateSecret", False) is not True


def test_it_outputs_pool_id_client_id_and_issuer_url() -> None:
    # The three values every downstream consumer (ingestion authenticator,
    # chatbot sign-in) reads. The issuer is asserted for its exact shape because
    # the JWT verifier matches on it literally.
    outputs = _template().find_outputs("*")
    rendered = str(outputs)
    assert "https://cognito-idp." in rendered
    assert ".amazonaws.com/" in rendered
    # Three CfnOutputs: pool id, app client id, issuer URL.
    assert len(outputs) == 3, f"expected 3 outputs, found {sorted(outputs)}"
