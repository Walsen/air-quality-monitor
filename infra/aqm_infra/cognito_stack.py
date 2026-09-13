"""The Cognito user pool that gives every diary user their own verified identity.

Feature: personal-diary-memory (Requirements 1.1, 1.5).

This stack provisions ONLY the pool and its app client; the pool is the spine of
per-user identity across all three services. The Cognito subject claim it mints
is what keys each user's profile and diary in DynamoDB, so a login always resolves
to the same storage key and one user can never read another's data.

Two choices are deliberate and worth stating, because they are what make the demo
work and are the kind of thing a well-meaning edit could reverse:

* ``USER_PASSWORD_AUTH`` (``ALLOW_USER_PASSWORD_AUTH``) is enabled so the chatbot
  can sign a demo user in with a username and password via ``InitiateAuth`` and
  hand the resulting JWT to the browser. Refresh is enabled alongside it so a
  session can be renewed without re-prompting.
* The app client is a PUBLIC client with NO generated secret. It runs in the
  browser, which cannot keep a secret; a client secret would both break the
  browser ``USER_PASSWORD_AUTH`` flow and leak a credential. The chatbot's shared
  access key remains the coarse outer gate — the JWT is the identity, not a
  secret held by the client.

Demo users are created out-of-band (a documented ``just`` recipe using the AWS
CLI), never committed, so no credential lands in the repository.

Synthesis performs no account lookup: the pool id and the account/region come
from the stack's explicit ``env`` and from CloudFormation tokens, so this stack
synthesizes offline for the credential-free CI suite.
"""

from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import (
    CfnOutput,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_cognito as cognito
from constructs import Construct


class CognitoStack(Stack):
    """A user pool and a public browser app client for per-user diary sign-in."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        env: cdk.Environment | None = None,
        description: str | None = None,
    ) -> None:
        """Provision the user pool, its public app client, and the config outputs.

        Args:
            scope: the CDK app or parent construct.
            construct_id: the stack's logical id.
            env: the target account and region, set EXPLICITLY by the caller so
                synthesis performs no account lookup.
            description: an optional CloudFormation stack description.
        """
        super().__init__(scope, construct_id, env=env, description=description)

        # POC removal policy: the pool can be torn down cleanly with the stack.
        # Demo users are recreated out-of-band, so nothing durable is lost.
        user_pool = cognito.UserPool(
            self,
            "UserPool",
            sign_in_aliases=cognito.SignInAliases(username=True, email=True),
            self_sign_up_enabled=False,  # demo users are admin-created, no public sign-up
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Public browser client: USER_PASSWORD_AUTH + refresh, and NO secret.
        app_client = user_pool.add_client(
            "AppClient",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_password=True),
            # user_password=True enables ALLOW_USER_PASSWORD_AUTH; CDK adds
            # ALLOW_REFRESH_TOKEN_AUTH for every client so a session can renew.
        )

        # The issuer URL the ingestion authenticator and the advisor match on,
        # in the exact shape the Cognito JWT verifier expects. self.region and
        # the pool id are CloudFormation tokens, so this renders to a Fn::Join at
        # synth without any account lookup.
        issuer_url = f"https://cognito-idp.{self.region}.amazonaws.com/{user_pool.user_pool_id}"

        # The three values every downstream consumer reads.
        CfnOutput(self, "UserPoolId", value=user_pool.user_pool_id)
        CfnOutput(self, "AppClientId", value=app_client.user_pool_client_id)
        CfnOutput(self, "IssuerUrl", value=issuer_url)


__all__ = ["CognitoStack"]
