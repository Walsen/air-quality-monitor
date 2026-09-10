"""The HTTP feed adapter, speaking the reference contract's own endpoints.

Requirements 5.1, 5.2, 5.6, 5.7.

THE CREDENTIAL NEVER BECOMES AN ATTRIBUTE. It is folded into the client's default headers at
construction and then dropped, so there is no ``self._credential`` for a later ``repr``, debug
dump
or log call to reach. Requirement 5.2 forbids it appearing in any log entry, response body or
archive, and §7 asks for structure rather than discipline: a value the object does not hold
cannot
leak from the object. The tests assert this by rendering ``vars()``.

THE TRANSPORT IS INJECTED, which is what lets the adapter's REAL request-building and
response-handling code run in the offline suite against a canned transport with no socket. That
is
§1's Dependency Inversion earning its place rather than being asserted: the boundary that gets
swapped (a live feed for a canned one) is the boundary that makes the thing testable.

THE BODY IS RETURNED AS BYTES, NEVER PARSED. Requirement 16.1 archives the payload before
anything
derives from it, so an adapter that parsed and re-serialised would deprive the archive of the
bytes
that actually arrived — and those are the ones worth keeping when a feed starts emitting
something
unexpected.
"""

from __future__ import annotations

import datetime as dt

import httpx

from aqm_ingestion.observability.logging import get_logger

_logger = get_logger("adapters.http_feed")

DEFAULT_FEED_TIMEOUT_SECONDS = 10.0
"""Longer than the forecast's 2 s because a feed window is the request's whole purpose.

Requirement 24.4 caps a FORECAST at 2 s because a response is still useful without one. A feed
fetch has no such fallback — giving up early would report "the feed had nothing" for what was
only
impatience, which is exactly the confusion Requirement 5.6 exists to prevent.
"""


class FeedRequestError(RuntimeError):
    """A feed request failed, so the caller must not treat the window as empty.

    Requirement 5.6 lists a transport error beside an unparseable body precisely so a failure
    cannot be mistaken for "the feed had nothing to give", which would advance the high-water
    mark
    past data the feed never delivered.
    """


class HttpFeedClient:
    """The FeedClient port over the reference contract's HTTP endpoints."""

    def __init__(
        self,
        base_url: str,
        credential: str,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = DEFAULT_FEED_TIMEOUT_SECONDS,
    ) -> None:
        """Build the client, folding the credential into the default headers.

        ``credential`` is consumed here and deliberately not retained on the instance.
        """
        self._client = httpx.Client(
            base_url=base_url,
            headers={"X-API-KEY": credential},
            transport=transport,
            timeout=timeout_seconds,
        )

    def fetch_data(
        self, since: dt.datetime, until: dt.datetime, species: frozenset[str]
    ) -> bytes:
        """Request `/SensorData` for a species set and a window (Requirement 5.1).

        The species set is SORTED into the query string. A frozenset has no defined iteration
        order, and §2 forbids letting incidental order reach output — here it would make the
        request URL vary between runs, which defeats both caching and reproducibility.
        """
        return self._get(
            "/SensorData",
            {
                "species": ",".join(sorted(species)),
                "since": since.isoformat(),
                "until": until.isoformat(),
            },
        )

    def fetch_sensors(self) -> bytes:
        """Request `/ListSensors` for the site metadata (Requirement 5.7)."""
        return self._get("/ListSensors", {})

    def _get(self, path: str, params: dict[str, str]) -> bytes:
        """One request, translating every failure into FeedRequestError.

        Raises:
            FeedRequestError: for a transport failure or a non-success status. The message names
                the path and the failure kind but NEVER the credential, and httpx's own error
                text is not interpolated for that reason — it can carry request headers.
        """
        try:
            response = self._client.get(path, params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as status_error:
            # The STATUS only. A response body from a failing feed may echo the request, headers
            # included, so it must not reach the log or the exception message.
            _logger.error(
                "feed_request_failed",
                path=path,
                failure_kind="HTTPStatusError",
                status_code=status_error.response.status_code,
            )
            raise FeedRequestError(
                f"feed request to {path} failed with status "
                f"{status_error.response.status_code}"
            ) from None
        except httpx.HTTPError as transport_error:
            kind = type(transport_error).__name__
            _logger.error("feed_request_failed", path=path, failure_kind=kind)
            # `from None` deliberately: httpx's chained exception repr can include the request,
            # and a traceback reaching a log would carry the header with it.
            raise FeedRequestError(f"feed request to {path} failed: {kind}") from None
        return response.content

    def close(self) -> None:
        """Release the underlying connection pool."""
        self._client.close()


__all__ = ["DEFAULT_FEED_TIMEOUT_SECONDS", "FeedRequestError", "HttpFeedClient"]
