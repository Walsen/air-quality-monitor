"""HTTP adapter for the reference-contract feed.

The credential is folded into the client headers at construction and never retained as an
attribute, so Requirement 5.2's prohibition holds structurally rather than by discipline.
"""

from aqm_ingestion.adapters.http_feed.client import (
    DEFAULT_FEED_TIMEOUT_SECONDS,
    FeedRequestError,
    HttpFeedClient,
)

__all__ = ["DEFAULT_FEED_TIMEOUT_SECONDS", "FeedRequestError", "HttpFeedClient"]
