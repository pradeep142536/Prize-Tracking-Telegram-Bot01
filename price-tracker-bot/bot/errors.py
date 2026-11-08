"""Exceptions shared across the bot's layers."""
from __future__ import annotations


class TrackerError(Exception):
    """Base class for all expected errors."""


class UnsafeURL(TrackerError, ValueError):
    """The link is malformed or points somewhere we must never fetch."""


class FetchError(TrackerError):
    """A page could not be fetched or its price could not be read.

    permanent    retrying is pointless (404/410).
    status       HTTP status code when there was one.
    blocked      the store refused us (HTTP 403/429 or a captcha page). We never try to get
                 around that: the monitor backs off and leaves the store alone for a while.
    retry_after  seconds the store asked us to wait (Retry-After header), if any.
    """

    def __init__(
        self,
        message: str,
        permanent: bool = False,
        status: int | None = None,
        blocked: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.permanent = permanent
        self.status = status
        self.blocked = blocked
        self.retry_after = retry_after
