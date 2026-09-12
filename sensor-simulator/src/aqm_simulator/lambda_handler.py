"""AWS Lambda entrypoint for the REST/pull interface (POC deployment).

This module adds NO application logic. It runs the simulator's own startup path
(`build_runtime`, which validates config and the API key exactly as the CLI and
the container do) and wraps the resulting FastAPI app with Mangum so API Gateway
can invoke it. Anything the REST interface does — auth, filtering, retention — is
the application's, unchanged.

Scope: POC-ADDENDUM.md under .kiro/specs/simulator-deployment. REST only; the MQTT
path is not deployed here.
"""

from __future__ import annotations

import os

from mangum import Mangum

from aqm_simulator.cli import build_runtime

# Force REST for the Lambda regardless of what else is set: this handler serves
# the pull interface and nothing else.
os.environ.setdefault("AQM_INTERFACE", "rest")

# build_runtime performs the full startup validation (Req 14.9): an absent or
# invalid API key raises here, at import/cold-start, so the function fails to
# initialize rather than serving an unauthenticated request.
_runtime = build_runtime(env=dict(os.environ))

if _runtime.app is None:  # pragma: no cover - defensive; REST is forced above
    raise RuntimeError("REST interface did not initialize; check AQM_INTERFACE")

# API Gateway HTTP API uses payload format 2.0.
handler = Mangum(_runtime.app, lifespan="off", api_gateway_base_path="/")
