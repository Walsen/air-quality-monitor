"""AWS Lambda entrypoint for the serving API (POC deployment).

Adds NO application logic. It runs the ingestion service's own startup path —
`resolve_and_validate` then `composition.build_runtime`, exactly as the CLI does —
and wraps the resulting FastAPI serving app with Mangum for API Gateway. All
authentication, rate limiting, and route logic are the application's, unchanged.

Per POC-ADDENDUM.md under .kiro/specs/ingestion-and-serving-service: only the
serving interface is deployed. The ingest path and the backing stores are out of
scope for the POC, so data-bearing dependencies are whatever the environment
wires — in the POC that means the auth gate and /health are live while data
routes answer against empty or absent stores.
"""

from __future__ import annotations

import os
from typing import Any, cast

from mangum import Mangum

from aqm_ingestion.composition import build_runtime
from aqm_ingestion.config.loader import read_config_file, resolve_and_validate
from aqm_ingestion.ports.clock import SystemClock

# Serving is the only interface this Lambda runs. resolve_and_validate reports
# every invalid value and raises ConfigError otherwise, so a bad configuration
# fails at cold start rather than serving a request.
_config = resolve_and_validate(
    os.environ, read_config_file(os.environ.get("AQM_CONFIG_FILE"))
)
_runtime = build_runtime(_config, SystemClock())

if not hasattr(_runtime, "app") or _runtime.app is None:
    raise RuntimeError(
        "serving is not enabled in this configuration; set AQM_ENABLE_SERVING"
    )

# `Runtime.app` is typed `object | None` because composition keeps FastAPI out of that
# boundary's types; here it is concretely the ASGI serving app, and the guard above
# excluded None. Cast so Mangum's ASGI-typed parameter is satisfied without loosening the
# composition's own type.
handler = Mangum(cast(Any, _runtime.app), lifespan="off", api_gateway_base_path="/")
