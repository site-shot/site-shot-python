"""Typed error taxonomy for the Site-Shot SDK.

Every error raised by the SDK is a subclass of :class:`SiteShotError` and
carries the HTTP status (when one was received) plus the raw response body or
JSON envelope for debugging. The underlying cause, when there is one, is
available through the standard ``__cause__`` attribute.

The class names are identical to the ones used by the official Node.js SDK
(``site-shot-sdk``), so the two libraries can be documented with one vocabulary.
"""

from __future__ import annotations

from typing import Any, Optional

__all__ = [
    "SiteShotError",
    "AuthError",
    "QuotaError",
    "CountryUnavailableError",
    "InvalidParamsError",
    "SiteShotTimeoutError",
    "APIError",
]


class SiteShotError(Exception):
    """Base class for every error raised by the Site-Shot SDK."""

    def __init__(
        self,
        message: str,
        *,
        http_status: Optional[int] = None,
        body: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        #: HTTP status code of the API response, when one was received.
        self.http_status = http_status
        #: Raw response body or parsed JSON envelope, when available.
        self.body = body


class AuthError(SiteShotError):
    """Missing or rejected API key (``userkey``)."""


class QuotaError(SiteShotError):
    """Plan quota exhausted or payment required."""


class CountryUnavailableError(SiteShotError):
    """The requested ``country`` has no capacity right now.

    Only reachable with ``strict_country=True`` — that is what turns the API's
    ``country_unavailable`` envelope on. Without it the API silently falls back
    to a US vantage point instead of failing.
    """


class InvalidParamsError(SiteShotError):
    """The API rejected one of the request parameters (out of range, bad format, ...)."""


class SiteShotTimeoutError(SiteShotError):
    """Client-side timeout, or the API reported that the render timed out."""


class APIError(SiteShotError):
    """Anything else: 5xx responses, unparseable bodies, connection failures."""
