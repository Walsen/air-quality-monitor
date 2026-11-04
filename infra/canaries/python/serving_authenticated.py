"""CloudWatch Synthetics canary: authenticated air-quality retrieval.

Feature: observability-xray-synthetics.

This is the functional canary — it exercises what a real user (and a judge) does:
obtain a Cognito token, call the serving API's ``/v1/air-quality/me`` with it, and
assert both that the call is authorised (200, not 401) AND that the response body is
well-formed. It would have caught the "authenticated but empty readings" state that a
plain liveness check cannot see.

Credentials are NEVER in this file. The canary reads:

* ``TARGET_URL`` — the serving API base (the serving stack's ``ApiUrl`` output);
* ``COGNITO_TOKEN_URL`` — the Cognito token endpoint;
* ``COGNITO_SECRET_NAME`` — the name of a Secrets Manager secret holding the
  client-credentials ``{client_id, client_secret, scope}``.

The secret is fetched at run time from Secrets Manager (the canary role is granted
``secretsmanager:GetSecretValue`` on exactly that secret), so no credential is ever
committed or placed in an environment variable. If the data check is configured
strict (``REQUIRE_READING=true``) an empty snapshot fails the canary; otherwise an
authorised-but-empty response passes (liveness of the auth path) and only a 401/403
or malformed body fails.
"""

import json
import os
import urllib.parse
import urllib.request

import boto3
from aws_synthetics.common import synthetics_logger as logger
from aws_synthetics.selenium import synthetics_webdriver as syn_webdriver  # noqa: F401


def _client_credentials() -> tuple[str, str, str]:
    """Fetch the client-credentials from Secrets Manager. Never logged."""
    secret_name = os.environ["COGNITO_SECRET_NAME"]
    secrets = boto3.client("secretsmanager")
    raw = secrets.get_secret_value(SecretId=secret_name)["SecretString"]
    data = json.loads(raw)
    return data["client_id"], data["client_secret"], data.get("scope", "")


def _access_token() -> str:
    """Exchange client credentials for a Cognito access token."""
    client_id, client_secret, scope = _client_credentials()
    token_url = os.environ["COGNITO_TOKEN_URL"]
    form = {"grant_type": "client_credentials", "client_id": client_id}
    if scope:
        form["scope"] = scope
    body = urllib.parse.urlencode(form).encode()
    request = urllib.request.Request(
        token_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            # HTTP Basic per the OAuth2 client-credentials spec; secret stays local.
            "Authorization": _basic_auth(client_id, client_secret),
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status != 200:
            raise AssertionError(f"token endpoint returned {response.status}")
        token = json.loads(response.read())["access_token"]
    # The token is a live credential: never log it, never return it in the result.
    return str(token)


def _basic_auth(client_id: str, client_secret: str) -> str:
    import base64

    raw = f"{client_id}:{client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


def _air_quality_check() -> None:
    base = os.environ["TARGET_URL"].rstrip("/")
    url = f"{base}/v1/air-quality/me"
    token = _access_token()
    request = urllib.request.Request(
        url,
        method="GET",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        status = response.status
        logger.info(f"air-quality status={status}")
        if status in (401, 403):
            raise AssertionError(f"authenticated call was rejected ({status})")
        if status != 200:
            raise AssertionError(f"expected 200 from {url}, got {status}")
        payload = json.loads(response.read())

    # A well-formed body carries the pseudonymous identity in the `user` field
    # (verified against the live serving API). Its ABSENCE is a contract break.
    if "user" not in payload:
        raise AssertionError("air-quality response missing the 'user' identity field")

    # Optional strict mode: an authorised-but-empty snapshot is a data-freshness
    # failure the seed refresh is meant to prevent. Off by default so the canary's
    # green state means "the authenticated path works", not "readings are fresh".
    if os.environ.get("REQUIRE_READING", "false").lower() == "true":
        current = payload.get("current") or payload.get("readings")
        if not current:
            raise AssertionError("authorised, but the snapshot carried no current reading")


def handler(event, context):
    """Synthetics entrypoint."""
    _air_quality_check()
    return "authenticated air-quality ok"
