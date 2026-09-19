"""CloudWatch Synthetics canary: serving API liveness (unauthenticated).

Feature: observability-xray-synthetics.

Hits the serving API's ``/health`` route on a schedule and asserts HTTP 200. This
proves the front door (API Gateway) and the serving Lambda are up and cold-start-
healthy, WITHOUT a credential — it is the cheapest, highest-signal check and the
one that alarms first when the stack is simply down.

It deliberately does NOT assert on air-quality data: an authenticated data check is
a separate canary (``serving_authenticated``), because liveness and data-freshness
fail for different reasons and should alarm independently.

The target URL arrives as the ``TARGET_URL`` environment variable (the serving
stack's ``ApiUrl`` output), never hard-coded — the same rule the CDK stacks follow.
``active_tracing`` on the canary emits an X-Ray segment per run, so a failing run
is visible on the same service map as the request it probes.
"""

import os
import urllib.request

from aws_synthetics.common import synthetics_logger as logger
from aws_synthetics.selenium import synthetics_webdriver as syn_webdriver  # noqa: F401


def _health_check() -> None:
    base = os.environ["TARGET_URL"].rstrip("/")
    url = f"{base}/health"
    logger.info(f"probing {url}")
    request = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(request, timeout=20) as response:
        status = response.status
        logger.info(f"health status={status}")
        if status != 200:
            raise AssertionError(f"expected 200 from {url}, got {status}")


def handler(event, context):
    """Synthetics entrypoint."""
    _health_check()
    return "serving health ok"
