"""Sync Site-Shot API client built on the standard library only.

The client wraps ``GET https://api.site-shot.com/`` and nothing else. Option
names mirror the HTTP query parameters verbatim (snake_case), so the HTTP
reference, this SDK and the Node.js SDK all teach the same vocabulary.
"""

from __future__ import annotations

import base64
import binascii
import collections.abc
import http.client
import io
import json as _json
import os
import random
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, TypedDict

from .errors import (
    APIError,
    AuthError,
    CountryUnavailableError,
    InvalidParamsError,
    QuotaError,
    SiteShotError,
    SiteShotTimeoutError,
)

__all__ = ["SiteShot", "CaptureOptions", "CaptureResult", "DEFAULT_BASE_URL"]

DEFAULT_BASE_URL = "https://api.site-shot.com/"
SDK_VERSION = "0.1.0"

#: The API's own default server-side render deadline (the ``timeout`` param), ms.
DEFAULT_SERVER_TIMEOUT_MS = 60_000
#: Client-side headroom on top of the server deadline, so the server answers first.
CLIENT_TIMEOUT_HEADROOM_S = 30.0

_CHUNK_SIZE = 64 * 1024

#: Params owned by the SDK; silently dropped when passed per call.
RESERVED_PARAMS = frozenset({"url", "userkey", "response_type", "request_headers"})

_AUTH_PATTERN = re.compile(r"userkey|api.?key|invalid key|unauthoriz|forbidden|authenticat")
_QUOTA_PATTERN = re.compile(r"quota|limit exceed|payment|credit|subscription")
_INVALID_PATTERN = re.compile(r"invalid|out of range|must be|unsupported")
_TIMEOUT_PATTERN = re.compile(r"time.?out|timed out")


class CaptureOptions(TypedDict, total=False):
    """Capture options, for callers who prefer building a dict.

    Names mirror the HTTP query parameters of ``GET https://api.site-shot.com/``
    verbatim. Booleans are accepted and coerced to ``1``/``0``. Unknown options
    pass through verbatim, so future API params work without an SDK release
    (a ``TypedDict`` cannot express that, but the methods take ``**params``).

    ``userkey`` is owned by the :class:`SiteShot` constructor and
    ``response_type`` by the return-mode methods — neither is settable per call.
    """

    #: The page to capture. Bare domains like ``example.com`` are accepted.
    url: str
    #: Viewport width in px, 100-8000. API default: 1024.
    width: int
    #: Viewport height in px, 100-20000. API default: 768.
    height: int
    #: Percentage zoom, 5-1000. API default: 100.
    zoom: int
    #: Capture the full scrollable page (height capped by ``max_height``).
    full_size: bool
    #: Height cap for ``full_size`` captures, 100-20000. API default: 20000.
    max_height: int
    #: Scale the result image to this width, 50-10000.
    scaled_width: int
    #: ``"png"`` or ``"jpeg"``. API default: ``"png"``.
    format: str
    #: Wait this many ms before capturing, 0-60000. API default: 500.
    delay_time: int
    #: Server-side render deadline in ms, 0-120000. API default: 60000.
    timeout: int
    #: Custom User-Agent for the rendering browser.
    user_agent: str
    #: Extra request headers, emitted as repeated ``request_header=Name:value``.
    request_headers: Mapping[str, str]
    #: Bring-your-own-proxy passthrough.
    http_proxy: str
    proxy_username: str
    proxy_password: str
    #: Rotate the outbound proxy per attempt. Off by default, and switched on
    #: automatically when ``country`` is set - pass ``False`` to opt out.
    proxy_rotation: bool
    #: Include the rendered HTML in the JSON result (use with ``capture_json``).
    source_code: bool
    #: JavaScript injected into the page before capture.
    javascript_code: str
    #: Remove ads. API default: 0.
    no_ads: bool
    #: Remove cookie-consent banners. API default: 0.
    no_cookie_popup: bool
    #: Render from this country - ISO 3166-1 alpha-2 code, e.g. ``"DE"``.
    country: str
    #: Fail with :class:`CountryUnavailableError` instead of a silent US fallback.
    strict_country: bool
    #: GPS override as ``"lat,lng"``, independent of IP.
    geolocation: str
    #: Accept-Language override, e.g. ``"de"``.
    language: str
    #: IANA time-zone name, e.g. ``"Europe/Berlin"``.
    time_zone: str


#: Parsed JSON result of a capture. Unknown fields are preserved.
CaptureResult = Dict[str, Any]

#: Transport seam: ``transport(url, headers, timeout_seconds) -> response``.
#: The response must expose ``status`` (or ``code``), ``read(size)`` and
#: ``close()``. It must be returned for *any* HTTP status; only connection-level
#: failures may raise. ``timeout_seconds`` is a *deadline for the whole
#: exchange*, not a per-socket-operation timeout: the built-in transport starts
#: counting when it is called and the budget covers connect, the status line and
#: headers, and (together with the body reader below) the download.
Transport = Callable[[str, Mapping[str, str], float], Any]


def _sleep(seconds: float) -> None:
    """Indirection so tests can neutralise retry backoff."""
    time.sleep(seconds)


def _backoff_delay(attempt: int) -> float:
    base: float = min(0.25 * (2 ** (attempt - 1)), 2.0)
    return base * (0.5 + random.random() * 0.5)


class _DeadlineReader(io.RawIOBase):
    """Raw reader that re-arms the socket timeout from an *absolute* deadline.

    A socket timeout is per operation, so it does not bound a response: a server
    that dribbles one byte of the status line or headers just inside the socket
    timeout keeps the client waiting ``bytes x timeout`` seconds, which is
    unbounded in practice. Every read here is instead bounded by the time left
    on the client deadline, which is what makes that deadline cover the header
    phase and not just the body.
    """

    def __init__(self, source: Any, sock: Any, deadline: float) -> None:
        super().__init__()
        # ``source`` is the socket's own buffered reader. Holding on to it keeps
        # the socket's io-refcount alive (urllib closes the socket as soon as
        # the headers are in and relies on that refcount for the body) and gives
        # us the one object that must be closed to release the fd.
        self._source = source
        self._raw = getattr(source, "raw", source)
        self._sock = sock
        self._deadline = deadline

    def readable(self) -> bool:
        return True

    def fileno(self) -> int:
        return int(self._raw.fileno())

    def readinto(self, buffer: Any) -> Optional[int]:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("client-side deadline exceeded")
        try:
            self._sock.settimeout(remaining)
        except OSError:  # pragma: no cover - socket already torn down
            pass
        result = self._raw.readinto(buffer)
        return None if result is None else int(result)

    def close(self) -> None:
        try:
            self._source.close()
        finally:
            super().close()


def _deadline_opener(deadline: float) -> urllib.request.OpenerDirector:
    """An opener whose responses read under ``deadline`` from the first byte on.

    The deadline is woven in at the lowest level (the socket reader) so it
    covers the status line and the headers as well as the body — matching the
    Node SDK, where a single ``AbortController`` timer covers the whole
    exchange.
    """

    class _DeadlineHTTPResponse(http.client.HTTPResponse):
        def __init__(
            self,
            sock: Any,
            debuglevel: int = 0,
            method: Optional[str] = None,
            url: Optional[str] = None,
        ) -> None:
            super().__init__(sock, debuglevel, method=method, url=url)
            # Swapped in before ``begin()`` reads the status line, so the very
            # first read of the exchange is already deadline-bound.
            self.fp = io.BufferedReader(_DeadlineReader(self.fp, sock, deadline))

    # Bound to a differently spelled local: a class body cannot read an
    # enclosing function's variable of the same name it is assigning.
    deadline_response = _DeadlineHTTPResponse

    class _DeadlineHTTPConnection(http.client.HTTPConnection):
        response_class = deadline_response

    class _DeadlineHTTPSConnection(http.client.HTTPSConnection):
        response_class = deadline_response

    class _DeadlineHTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
            return self.do_open(_DeadlineHTTPConnection, req)

    class _DeadlineHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
            # ``None`` makes http.client build the same verified default context
            # urllib would have used.
            context = getattr(self, "_context", None)
            return self.do_open(_DeadlineHTTPSConnection, req, context=context)

    return urllib.request.build_opener(_DeadlineHTTPHandler(), _DeadlineHTTPSHandler())


def _urllib_transport(url: str, headers: Mapping[str, str], timeout: float) -> Any:
    """Default transport: a plain GET through :mod:`urllib.request`.

    ``timeout`` is honoured as a deadline for the whole exchange, header phase
    included — not as urllib's per-socket-operation timeout.

    ``HTTPError`` is returned rather than raised — it is a perfectly good
    response object, and the SDK classifies errors from the body, not from the
    exception type.
    """
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    opener = _deadline_opener(time.monotonic() + timeout)
    try:
        return opener.open(request, timeout=timeout)  # noqa: S310 - fixed https endpoint
    except urllib.error.HTTPError as exc:
        return exc


def _response_status(response: Any) -> int:
    status = getattr(response, "status", None)
    if status is None:
        status = getattr(response, "code", None)
    try:
        return int(status)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _is_timeout(exc: BaseException) -> bool:
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, (socket.timeout, TimeoutError))


def _is_connection_error(exc: BaseException) -> bool:
    return isinstance(exc, (urllib.error.URLError, OSError, http.client.HTTPException))


#: Where the underlying socket hides, per response flavour: a success-path
#: ``HTTPResponse`` (``fp`` is the buffered reader) and an ``HTTPError``, whose
#: ``fp`` *is* the ``HTTPResponse``. Missing the second one let error bodies
#: overshoot the deadline by a whole socket timeout.
_SOCKET_PATHS = (("fp", "raw", "_sock"), ("fp", "fp", "raw", "_sock"), ("fp", "_sock"))


def _response_socket(response: Any) -> Any:
    for path in _SOCKET_PATHS:
        node: Any = response
        for attribute in path:
            node = getattr(node, attribute, None)
            if node is None:
                break
        if node is not None:
            return node
    return None


def _tighten_socket_timeout(response: Any, remaining: float) -> None:
    """Best-effort: shrink the socket timeout to the time left on the deadline.

    Without this a body that stalls late in the transfer could block for a full
    socket timeout *on top of* the deadline. Belt and braces for the built-in
    transport (whose reader is already deadline-bound), and the only such brake
    a custom transport gets — the per-chunk deadline check below is what
    actually enforces the bound.
    """
    try:
        sock = _response_socket(response)
        if sock is not None:
            sock.settimeout(max(remaining, 0.001))
    except Exception:  # pragma: no cover - platform specific internals
        pass


def _normalize_url(raw: Any) -> str:
    trimmed = ("" if raw is None else str(raw)).strip()
    if not trimmed:
        raise InvalidParamsError('Missing required "url" option.')
    candidate = trimmed if re.match(r"^https?://", trimmed, re.IGNORECASE) else "https://" + trimmed
    parts = urllib.parse.urlsplit(candidate)
    if not parts.netloc or any(ch.isspace() for ch in parts.netloc):
        raise InvalidParamsError(
            'Invalid url: "{0}". Pass a web page URL such as https://example.com '
            "(bare domains like example.com are also accepted).".format(trimmed)
        )
    return candidate


def _append_param(params: List[Tuple[str, str]], key: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, bool):
        params.append((key, "1" if value else "0"))
        return
    if isinstance(value, (set, frozenset)):
        # Sets have no order, and their iteration order changes between
        # processes under hash randomization. Sort so a given call always
        # serializes to the same query string (cache keys, tests, diffs).
        for item in sorted(value, key=str):
            _append_param(params, key, item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _append_param(params, key, item)
        return
    params.append((key, str(value)))


def _strip_data_url_prefix(image: str) -> str:
    if image.startswith("data:"):
        comma = image.find(",")
        if comma != -1:
            return image[comma + 1 :]
    return image


def _error_text(value: Any) -> Optional[str]:
    """Return ``value`` when it is a usable error string, else ``None``."""
    if isinstance(value, str) and value.strip():
        return value
    return None


def _classify_error(status: int, error_text: Optional[str], body: Any) -> SiteShotError:
    lower = (error_text or "").lower()

    if lower == "country_unavailable":
        return CountryUnavailableError(
            "The requested country has no available capacity right now "
            "(country_unavailable). Retry later, or drop strict_country to allow "
            "the default US fallback.",
            http_status=status,
            body=body,
        )
    # 401 is the only status that means "the key itself was rejected": it is
    # what the API returns for a missing or unknown ``userkey``.
    if _AUTH_PATTERN.search(lower) or status == 401:
        return AuthError(
            "Site-Shot rejected the API key{0}. Check the key or get one at "
            "https://www.site-shot.com/pricing/.".format(": " + error_text if error_text else ""),
            http_status=status,
            body=body,
        )
    # 403 is a *billing* state, not a key problem: the API returns it when the
    # account has no active subscription, and a bad or missing key is always a
    # 401. Classifying it as AuthError would send users to check a key that is
    # perfectly valid.
    if status in (402, 403, 429) or _QUOTA_PATTERN.search(lower):
        return QuotaError(
            "Site-Shot quota or payment issue{0}.".format(
                ": " + error_text if error_text else " (HTTP {0})".format(status)
            ),
            http_status=status,
            body=body,
        )
    if status == 400 or _INVALID_PATTERN.search(lower):
        return InvalidParamsError(
            "Site-Shot rejected the request parameters{0}.".format(
                ": " + error_text if error_text else " (HTTP {0})".format(status)
            ),
            http_status=status,
            body=body,
        )
    if _TIMEOUT_PATTERN.search(lower):
        return SiteShotTimeoutError(
            "Site-Shot render timed out: {0}".format(error_text),
            http_status=status,
            body=body,
        )
    return APIError(
        "Site-Shot API error{0} (HTTP {1}).".format(
            ": " + error_text if error_text else "", status
        ),
        http_status=status,
        body=body,
    )


class SiteShot:
    """Site-Shot API client.

    .. code-block:: python

        from site_shot import SiteShot

        client = SiteShot("YOUR_API_KEY")  # or set SITESHOT_API_KEY
        png = client.capture("example.com", full_size=True)

    :param api_key: Your API key. Falls back to the ``SITESHOT_API_KEY``
        environment variable — the same variable the Site-Shot MCP server uses.
    :param base_url: API endpoint. Default ``https://api.site-shot.com/``.
    :param timeout: Client-side deadline **in seconds**, bounding the whole
        exchange: connecting, waiting for the status line and headers, and
        downloading the body. Default: the per-call server
        ``timeout`` param (or its API default of 60000 ms) plus 30 s headroom,
        so the server always gets to answer first. Note the per-call ``timeout``
        capture option is a different thing: the server-side render deadline, in
        milliseconds, named verbatim after the HTTP param.
    :param retries: Retries for **connection-level failures only** (DNS,
        connection reset, no bytes received), with jittered backoff. Default
        ``0`` — renders cost quota, so the SDK never auto-retries a
        completed-but-failed render.
    :param transport: Transport override (test seam). Defaults to
        :mod:`urllib.request`.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: Optional[float] = None,
        retries: int = 0,
        transport: Optional[Transport] = None,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("SITESHOT_API_KEY")
        if not key or not str(key).strip():
            raise AuthError(
                "Missing Site-Shot API key. Pass it to SiteShot(api_key) or set the "
                "SITESHOT_API_KEY environment variable. Get a key at "
                "https://www.site-shot.com/pricing/."
            )
        self.api_key = str(key).strip()
        self.base_url = base_url or DEFAULT_BASE_URL
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self._transport: Transport = transport or _urllib_transport

    # -- context manager (no pooled resources; here so habits do not error) ---

    def __enter__(self) -> "SiteShot":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def close(self) -> None:
        """No-op. The client holds no pooled resources."""

    # -- return modes --------------------------------------------------------

    def capture(self, url: str, /, **params: Any) -> bytes:
        """Capture a screenshot and return the raw image bytes.

        ``url`` is positional-only so that ``**options`` dicts carrying a ``url``
        key (:class:`CaptureOptions` declares one) type-check and run: the
        positional argument wins, exactly like ``userkey``.
        """
        encoded = self.capture_base64(url, **params)
        compact = "".join(encoded.split())
        try:
            data = base64.b64decode(compact, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise APIError(
                "Site-Shot returned a malformed base64 image payload "
                "({0} base64 characters could not be decoded).".format(len(encoded))
            ) from exc
        if not data:
            raise APIError("Site-Shot returned an empty image payload.")
        return data

    def capture_to_file(self, url: str, path: str, /, **params: Any) -> str:
        """Capture a screenshot and write it to ``path``. Returns ``path``."""
        data = self.capture(url, **params)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def capture_base64(self, url: str, /, **params: Any) -> str:
        """Capture and return plain base64 (any data-URL prefix stripped).

        Handy for data URLs and LLM vision payloads.
        """
        result = self.capture_json(url, **params)
        image = result.get("image")
        if not isinstance(image, str) or not image:
            raise APIError("Site-Shot response did not contain an image.", body=result)
        return _strip_data_url_prefix(image)

    def capture_json(self, url: str, /, **params: Any) -> CaptureResult:
        """Capture and return the full JSON result.

        Contains the base64 ``image`` plus any metadata the API includes —
        and ``source_code`` (the rendered HTML) when you ask for it.
        """
        query = self._build_params(url, params)
        query.append(("response_type", "json"))
        endpoint = self._endpoint(query)
        timeout_s = self._resolve_timeout(params)
        return self._request(endpoint, timeout_s)

    def build_url(self, url: str, /, **params: Any) -> str:
        """Build the request URL without executing it.

        The URL is in the API's default image-response mode.

        .. warning::
            The returned URL **embeds your API key** (``userkey``) and Site-Shot
            has no signed-URL scheme. Use it for debugging or server-side
            proxying only — never in an ``<img src>`` or anywhere a browser or
            third party can see it.
        """
        return self._endpoint(self._build_params(url, params))

    # -- internals -----------------------------------------------------------

    def _endpoint(self, query: List[Tuple[str, str]]) -> str:
        separator = "&" if "?" in self.base_url else "?"
        return "{0}{1}{2}".format(self.base_url, separator, urllib.parse.urlencode(query))

    def _build_params(self, url: str, options: Mapping[str, Any]) -> List[Tuple[str, str]]:
        query: List[Tuple[str, str]] = [
            ("url", _normalize_url(url)),
            ("userkey", self.api_key),
        ]
        for key, value in options.items():
            if key in RESERVED_PARAMS:
                continue
            _append_param(query, key, value)
        headers = options.get("request_headers")
        if isinstance(headers, collections.abc.Mapping):
            for name, value in headers.items():
                query.append(("request_header", "{0}:{1}".format(name, value)))
        return query

    def _resolve_timeout(self, options: Mapping[str, Any]) -> float:
        if self.timeout is not None:
            return float(self.timeout)
        server_ms = float(DEFAULT_SERVER_TIMEOUT_MS)
        raw = options.get("timeout")
        if raw is not None and not isinstance(raw, bool):
            try:
                candidate = float(raw)
            except (TypeError, ValueError):
                candidate = 0.0
            if candidate > 0:
                server_ms = candidate
        return server_ms / 1000.0 + CLIENT_TIMEOUT_HEADROOM_S

    def _request(self, endpoint: str, timeout_s: float) -> CaptureResult:
        headers = {
            "accept": "application/json",
            "user-agent": "site-shot/{0} python".format(SDK_VERSION),
        }
        attempt = 0
        while True:
            deadline = time.monotonic() + timeout_s
            try:
                response = self._transport(endpoint, headers, timeout_s)
            except SiteShotError:
                raise
            except Exception as exc:  # noqa: BLE001 - re-raised classified below
                if _is_timeout(exc):
                    raise SiteShotTimeoutError(
                        "Site-Shot request timed out after {0:g} s "
                        "(client-side deadline).".format(timeout_s)
                    ) from exc
                if not _is_connection_error(exc):
                    raise
                if time.monotonic() >= deadline:
                    # The connection died at or after the deadline: that is the
                    # deadline doing its job (a transport may report it as a
                    # torn-down connection rather than a timeout), so report a
                    # timeout and never retry it.
                    raise SiteShotTimeoutError(
                        "Site-Shot request timed out after {0:g} s "
                        "(client-side deadline).".format(timeout_s)
                    ) from exc
                if attempt < self.retries:
                    attempt += 1
                    _sleep(_backoff_delay(attempt))
                    continue
                raise APIError(
                    "Connection to the Site-Shot API failed: {0}".format(exc)
                ) from exc

            # Past this point the request reached the API: a render may have
            # been spent, so nothing here is ever retried.
            try:
                status = _response_status(response)
                body = self._read_body(response, deadline, timeout_s, status)
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # pragma: no cover - best effort
                        pass
            return self._parse_response(status, body)

    def _read_body(self, response: Any, deadline: float, timeout_s: float, status: int) -> str:
        """Read the whole body under the same deadline as the request.

        The client-side deadline must bound the *full* response, not just the
        wait for headers — a screenshot body is the bulk of the transfer.

        ``read1`` is used in preference to ``read`` because ``read(n)`` blocks
        until it has collected all *n* bytes: a body that trickles in one byte
        at a time would sit inside a single ``read`` call for hours and never
        reach the deadline check below. ``read1`` returns as soon as anything
        arrives, so the deadline is re-evaluated on every drip.
        """
        read = getattr(response, "read1", None)
        if not callable(read):
            read = response.read
        chunks: List[bytes] = []
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SiteShotTimeoutError(
                    "Site-Shot request timed out after {0:g} s while downloading the "
                    "response body (client-side deadline).".format(timeout_s),
                    http_status=status,
                )
            _tighten_socket_timeout(response, remaining)
            try:
                chunk = read(_CHUNK_SIZE)
            except Exception as exc:  # noqa: BLE001 - re-raised classified below
                if isinstance(exc, SiteShotError):
                    raise
                if _is_timeout(exc):
                    raise SiteShotTimeoutError(
                        "Site-Shot request timed out after {0:g} s while downloading the "
                        "response body (client-side deadline).".format(timeout_s),
                        http_status=status,
                    ) from exc
                if not _is_connection_error(exc):
                    raise
                raise APIError(
                    "Failed to read the Site-Shot API response body: {0}".format(exc),
                    http_status=status,
                ) from exc
            if not chunk:
                break
            chunks.append(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
        return b"".join(chunks).decode("utf-8", "replace")

    def _parse_response(self, status: int, text: str) -> CaptureResult:
        try:
            envelope = _json.loads(text)
        except ValueError:
            envelope = None

        ok = 200 <= status < 300

        if isinstance(envelope, dict):
            # The API uses two different error keys depending on how the
            # request failed, so both have to be read. The real envelopes are
            # pinned as fixtures in tests/test_client.py.
            #
            # 1. A failure *during capture* comes back as HTTP *200* - the
            #    failure status does not survive onto the response - with the
            #    capture envelope carrying ``error`` alongside a placeholder
            #    error ``image``. Checking ``error`` on every status, not just
            #    non-2xx, is what stops that placeholder being handed back as if
            #    it were a screenshot.
            error_text = _error_text(envelope.get("error"))
            if error_text is not None:
                raise _classify_error(status, error_text, envelope)

            # 2. A request rejected *before capture starts* (bad or missing
            #    key, inactive subscription) comes back on a non-2xx status with
            #    ``message``. ``message`` is deliberately NOT read on a 2xx:
            #    only that rejection path uses the key for failures, so treating
            #    it as an error signal on a successful capture would turn any
            #    future metadata field named ``message`` into a spurious raise.
            if not ok:
                raise _classify_error(status, _error_text(envelope.get("message")), envelope)
            return envelope

        if not ok:
            raise _classify_error(status, text[:500] if text else None, text)

        raise APIError(
            "Unexpected non-JSON response from the Site-Shot API (HTTP {0}).".format(status),
            http_status=status,
            body=text[:500],
        )
