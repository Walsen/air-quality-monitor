"""Regression guard: the RS256 signature backend PyJWT uses at runtime is present.

WHY THIS TEST EXISTS. The deployed serving Lambda rejected every valid Cognito ID token as
``invalid_signature`` while the same resolver + ``jwt.decode`` path succeeded in the dev venv.
Root cause was NOT the verification code — it was the bundle. Cognito signs its ID tokens with
RS256, and PyJWT can only verify RS256 through the ``cryptography`` backend. That backend is
PyJWT's optional ``crypto`` extra; the pin ``pyjwt==2.13.0`` omitted it, so ``cryptography`` was
absent from the production (``--no-dev``) dependency closure and never shipped in the Lambda.
The dev venv happened to have ``cryptography`` transitively, so every existing auth test passed.

WHY THE EXISTING TESTS MISSED IT. The authenticator tests inject a fake ``key_resolver`` and
sign with HS256 (a symmetric algorithm needing no crypto backend), so they never exercised the
real RS256 verification path. This test closes that gap: it generates an RSA keypair, signs a
JWT with RS256, and verifies it — the exact operation that fails when the backend is missing.

WHAT THIS TEST DOES AND DOES NOT PROVE. It proves the code path works when the backend is there.
It CANNOT assert the bundle's contents from inside a unit test — the production image ships only
``--no-dev`` dependencies, so the guard's value is paired with the manifest change (``pyjwt`` ->
``pyjwt[crypto]``) that puts ``cryptography`` into that ``--no-dev`` closure. Test + manifest
together are the fix: the manifest gets the backend into the bundle, this test proves RS256
verification succeeds once it is there and fails loudly if it ever goes missing again.

Needs no AWS and no network: pure local keygen, sign, and verify.
"""

from __future__ import annotations

import datetime as dt

import jwt


def test_rs256_verification_succeeds_when_the_crypto_backend_is_present() -> None:
    # Generate an RSA keypair in-process. Importing `cryptography` here is deliberate: it is the
    # very backend PyJWT reaches for to sign and verify RS256, so if it were absent this line —
    # and PyJWT's RS256 path below — would fail rather than silently degrade.
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    claims = {
        "sub": "user-1",
        "aud": "example-client",
        "iss": "https://cognito-idp.eu-west-2.amazonaws.com/eu-west-2_example",
        "token_use": "id",
        "exp": int((dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).timestamp()),
    }

    # Signing with RS256 requires the crypto backend; PyJWT raises without it.
    token = jwt.encode(claims, private_pem, algorithm="RS256")

    # Verifying with RS256 is the operation the Lambda performs against Cognito's tokens. With
    # the backend absent PyJWT raises here; with it present the round-trip succeeds.
    decoded = jwt.decode(
        token,
        public_pem,
        algorithms=["RS256"],
        audience="example-client",
        issuer="https://cognito-idp.eu-west-2.amazonaws.com/eu-west-2_example",
    )

    assert decoded["sub"] == "user-1"
    assert decoded["token_use"] == "id"


def test_rs256_is_a_registered_pyjwt_algorithm() -> None:
    # A second, cheaper angle on the same invariant: PyJWT only registers RS256 in its available
    # algorithm set when a crypto backend is importable. If cryptography were missing, RS256
    # would be absent here — so this fails fast for the same root cause without doing keygen.
    assert "RS256" in jwt.algorithms.get_default_algorithms()
