"""Authenticator adapter verifying the caller's credential.

The claim set is verified inside the adapter and discarded there, so only the pseudonymous
identity crosses the port boundary (Requirements 18.1, 18.7).
"""

from aqm_ingestion.adapters.auth.cognito import (
    DEFAULT_ALGORITHMS,
    EXPECTED_TOKEN_USE,
    CognitoAuthenticator,
)

__all__ = ["DEFAULT_ALGORITHMS", "EXPECTED_TOKEN_USE", "CognitoAuthenticator"]
