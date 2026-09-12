"""The container entry point: `python -m aqm_ingestion.cli`.

This is the module the image's `CMD` has always named, and until now it did not exist — the
image could be built but never started. Writing it is wiring rather than design:
`composition.build_runtime` already assembles every port from a validated configuration, and
`serving.build_app` already produces the authenticated FastAPI application. This module only
reads the real environment, hands it over, and serves whichever interfaces are enabled.

STARTUP FAILS FAST, per the Config_Loader rule: `resolve_and_validate` reports EVERY invalid
value rather than the first, and nothing is constructed until it returns. An invalid
configuration exits non-zero with the problems on stderr — never a half-started process
holding settings nobody chose.

WHY THE SERVING INTERFACE IS THE ONLY ONE STARTED HERE. Requirement 26.10 makes the three
interfaces (`enable_push`, `enable_pull`, `enable_serving`) independent switches, and the push
and pull entries are long-running loops with their own lifecycles that `Runtime` exposes as
`mqtt_entry` and `feed_entry`. Serving them all from one process would make the deployment's
per-interface runtime choice — invocation-scoped for pull, resident for push — impossible to
honour. So this entry serves the HTTP API, and a process that should ingest is started with
its own command.

NOTHING IS WRITTEN TO STDOUT EXCEPT THE STRUCTURED LOG. `uvicorn`'s own access log is left
to the logging configuration the runtime installs, so the single-line-JSON contract survives.
"""

from __future__ import annotations

import os
import sys

import uvicorn
from fastapi import FastAPI

from aqm_ingestion.composition import build_runtime
from aqm_ingestion.config.loader import ConfigError, read_config_file, resolve_and_validate
from aqm_ingestion.ports.clock import SystemClock

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000


def main(argv: list[str] | None = None) -> int:
    """Resolve configuration, build the runtime, and serve. Returns an exit status."""
    del argv
    try:
        config = resolve_and_validate(
            os.environ, read_config_file(os.environ.get("AQM_CONFIG_FILE"))
        )
    except ConfigError as error:
        # Every problem, not the first: a partly valid config would hand the service settings
        # nobody chose. stderr rather than print, because stdout carries single-line JSON.
        sys.stderr.write(f"configuration is invalid: {error}\n")
        return 2

    runtime = build_runtime(config, SystemClock())
    # `Runtime.app` is typed `object | None` so the composition module need not import FastAPI
    # into every consumer. Narrowing it here is also a real guard: a runtime that produced
    # something else would otherwise reach uvicorn as an unservable object.
    if not isinstance(runtime.app, FastAPI):
        sys.stderr.write(
            "serving is not enabled in this configuration, so there is no application to "
            "serve; set interfaces.enable_serving\n"
        )
        return 3

    # The bind address comes from configuration and DEFAULTS TO LOOPBACK. A container that
    # must be reachable maps a port, and the compose file sets 0.0.0.0 inside the container
    # because the published mapping is what limits reachability to the host's loopback.
    # Defaulting to 0.0.0.0 here would make every ad-hoc run publicly reachable instead.
    host = os.environ.get("AQM_LISTEN_HOST", _DEFAULT_HOST)
    port = int(os.environ.get("AQM_LISTEN_PORT", str(_DEFAULT_PORT)))

    uvicorn.run(runtime.app, host=host, port=port, log_config=None, access_log=False)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the container, not the suite
    raise SystemExit(main())
