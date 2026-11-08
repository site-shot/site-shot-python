"""Sync Site-Shot API client built on the standard library only.

The client wraps ``GET https://api.site-shot.com/`` and nothing else. Option
names mirror the HTTP query parameters verbatim (snake_case), so the HTTP
reference, this SDK and the Node.js SDK all teach the same vocabulary.
"""

from __future__ import annotations

import base64
import binascii
import collections.abc
import difflib
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
import warnings
from typing import Any, Callable, Dict, List, Literal, Mapping, Optional, Tuple, TypedDict

from .errors import (
    APIError,
    AuthError,
    ConcurrencyLimitError,
    CountryUnavailableError,
    InvalidParamsError,
    PageLoadError,
    PartialCaptureWarning,
    QuotaError,
    SiteShotError,
    SiteShotTimeoutError,
    SiteShotWarning,
)

__all__ = ["SiteShot", "CaptureOptions", "CaptureResult", "DEFAULT_BASE_URL", "ImageFormat"]

DEFAULT_BASE_URL = "https://api.site-shot.com/"
SDK_VERSION = "0.3.1"

#: The API's own default server-side render deadline (the ``timeout`` param), ms.
DEFAULT_SERVER_TIMEOUT_MS = 60_000
#: Client-side headroom on top of the server deadline, so the server answers first.
CLIENT_TIMEOUT_HEADROOM_S = 30.0

#: Appended to every client-side deadline error. The API may retry a slow page
#: on another route and so outlast the default deadline; the request it was
#: working on is not cancelled by the client giving up.
_CLIENT_TIMEOUT_HINT = (
    "The API may still have run the capture, and it may count toward your "
    "allowance. For slow pages, pass a longer deadline: SiteShot(timeout=...)."
)

_CHUNK_SIZE = 64 * 1024

#: Params owned by the SDK; silently dropped when passed per call.
RESERVED_PARAMS = frozenset({"url", "userkey", "response_type", "request_headers"})

# "forbidden" is deliberately absent from _AUTH_PATTERN. A capture that fails
# upstream reports the failing HTTP status line verbatim, so ``error`` can read
# "403 Forbidden" on an otherwise-200 response - a valid key blamed for something
# it did not cause. A real key rejection always arrives either as a 401 or with
# text naming the key, credentials or authentication.
_AUTH_PATTERN = re.compile(r"userkey|api.?key|invalid key|unauthoriz|authenticat")
# The free plan's two refusals are matched by text, because a 200 capture
# envelope leaves only the text to classify by. Once the month's captures are
# spent the API answers HTTP 403 with {"message": ...} reading "Free plan used
# up: ..." or "Free API trial used up: ..." - it may use either wording, so both
# prefixes are matched. A country other than US on the free plan is refused
# with "Geotargeting needs a paid plan: ..."; with response_type=json, which
# this SDK always sends, that arrives as HTTP 200 with the text in ``error`` and
# no image, so only the text makes it a QuotaError. Only these prefixes are
# stable; the rest of each sentence may change.
_QUOTA_PATTERN = re.compile(
    r"quota|limit exceed|payment|credit|subscription"
    r"|paid plan|free plan used up|free api trial used up"
)
_INVALID_PATTERN = re.compile(r"invalid|out of range|must be|unsupported")
# The API's refusals of the target itself: "Malformed target address" for a URL
# it cannot parse, and the SSRF guard's "Forbidden target address" (a 403) for
# private and local hosts. Checked before the 403 billing branch.
_TARGET_PATTERN = re.compile(r"target address")
# The ss-plugin's 429: "API dedicated workers limit exceeded. Allowed number of
# parallel requests is N".
_CONCURRENCY_PATTERN = re.compile(r"workers limit|parallel requests")
#: ``error_code`` for a target page that could not be opened (unresolvable or
#: unreachable host). Its ``error`` text reads "504 Gateway Timeout", which is
#: why the code, not the text, has to decide.
_PAGE_LOAD_CODES = frozenset({"proxy_navigation_failed"})
_TIMEOUT_PATTERN = re.compile(r"time.?out|timed out")

#: Output formats the API renders. ``"webp"`` is lossless WebP; ``"jpg"`` is the
#: API's own alias of ``"jpeg"``. The API ignores letter case, and so does the SDK.
ImageFormat = Literal["png", "jpeg", "jpg", "webp"]
_FORMAT_ALIASES = {"png": "png", "jpeg": "jpeg", "jpg": "jpeg", "webp": "webp"}


class CaptureOptions(TypedDict, total=False):
    """Capture options, for callers who prefer building a dict.

    Names mirror the HTTP query parameters of ``GET https://api.site-shot.com/``
    verbatim. Booleans are accepted and coerced to ``1``/``0``. Unknown options
    pass through verbatim, so future API params work without an SDK release
    (a ``TypedDict`` cannot express that, but the methods take ``**params``).

    A name that is not a known option but is close to one (``full_page``,
    ``delay``) is still sent, with a :class:`SiteShotWarning` naming the option
    it most likely meant: the API ignores names it does not know, so a typo
    otherwise goes unnoticed.

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
    #: With ``format="webp"``, a full-page capture taller than 16,383 px is cut
    #: at 16,383 px from the top -- libwebp's per-side limit.
    max_height: int
    #: Scale the result image to this width, 50-10000.
    scaled_width: int
    #: ``"png"`` (default) and ``"webp"`` are lossless; ``"webp"`` is about 35%
    #: smaller than ``"png"`` on a typical page (it varies by page), but capped
    #: at 16,383 px per side by libwebp. ``"jpeg"`` (alias ``"jpg"``) is lossy.
    #: Any other value raises :class:`InvalidParamsError` before a request is
    #: sent. API default: ``"png"``.
    format: ImageFormat
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
    #: Route selection. There is no default, and omitting this is not the same as
    #: passing either value: leave it out and Site-Shot picks the route (recommended).
    #: ``True`` sends every attempt through a rotating proxy. ``False`` makes a single
    #: attempt without rotation - through one proxy of ``country`` when a country is
    #: set, otherwise direct.
    proxy_rotation: bool
    #: Include the rendered HTML in the JSON result (use with ``capture_json``).
    source_code: bool
    #: JavaScript injected into the page before capture.
    javascript_code: str
    #: Remove ads. API default: 0.
    no_ads: bool
    #: Remove cookie-consent banners. API default: 0.
    no_cookie_popup: bool
    #: Give the page a working (software) WebGL context. Detectable by bot
    #: protection, so leave it off unless the page needs it. API default: 0.
    webgl: bool
    #: Start in a clean browser: no cookies or local storage saved by an earlier
    #: capture, and nothing from this one kept for a later one. API default: 0.
    fresh_session: bool
    #: Wait through interstitial challenge pages instead of capturing them.
    #: Passing it at all, with either value, turns off Site-Shot's adaptive
    #: retries for hard hosts; leave it out to keep them.
    handle_challenges: bool
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


#: Every query parameter the API documents, for typo hints.
_KNOWN_PARAMS = frozenset(CaptureOptions.__annotations__) | RESERVED_PARAMS | {"request_header"}
#: Names other screenshot APIs use, which the closeness check would miss.
_PARAM_ALIASES = {
    "fullpage": "full_size",
    "full_page": "full_size",
    "delay": "delay_time",
    "wait": "delay_time",
    "block_ads": "no_ads",
}


def _warn_unknown_param(key: str) -> None:
    """Warn when an unknown option name looks like a typo of a known one.

    Names with no close match pass through silently, so a new API param works
    before the SDK knows about it.
    """
    if key in _KNOWN_PARAMS:
        return
    guess = _PARAM_ALIASES.get(key.lower())
    if guess is None:
        matches = difflib.get_close_matches(key.lower(), sorted(_KNOWN_PARAMS), n=1)
        guess = matches[0] if matches else None
    if guess is None or guess == key:
        return
    warnings.warn(
        SiteShotWarning(
            'Unknown capture option "{0}" (did you mean "{1}"?). It is sent as is, '
            "and the API ignores names it does not know.".format(key, guess)
        ),
        stacklevel=4,
    )


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


#: An explicit scheme: ``name://`` or one of the non-web schemes written without
#: slashes. ``example.com:8080`` and ``localhost:3000`` are hosts with a port,
#: not schemes, and stay accepted as bare domains.
_SCHEME_PATTERN = re.compile(
    r"^([a-z][a-z0-9+.-]*)://"
    r"|^(javascript|mailto|data|file|about|blob|tel|view-source|chrome):",
    re.IGNORECASE,
)


def _normalize_url(raw: Any) -> str:
    trimmed = ("" if raw is None else str(raw)).strip()
    if not trimmed:
        raise InvalidParamsError('Missing required "url" option.')
    scheme = _SCHEME_PATTERN.match(trimmed)
    if scheme and (scheme.group(1) or scheme.group(2)).lower() not in ("http", "https"):
        raise InvalidParamsError(
            'Unsupported URL scheme in "{0}": only http(s) web pages can be captured.'.format(
                trimmed
            )
        )
    candidate = trimmed if re.match(r"^https?://", trimmed, re.IGNORECASE) else "https://" + trimmed
    parts = urllib.parse.urlsplit(candidate)
    if not parts.netloc or any(ch.isspace() for ch in parts.netloc):
        raise InvalidParamsError(
            'Invalid url: "{0}". Pass a web page URL such as https://example.com '
            "(bare domains like example.com are also accepted).".format(trimmed)
        )
    return candidate


def _normalize_format(raw: Any) -> Any:
    """Refuse a format the API cannot render, before a request is spent on it.

    The API does not refuse one up front: it runs the render ladder and then
    answers with an error card, not a screenshot.
    """
    if raw is None or raw == "":
        # None is omitted from the query and the API default (png) applies;
        # an explicit "" is still sent as a bare `format=`, as it was before.
        return raw
    canonical = _FORMAT_ALIASES.get(raw.lower()) if isinstance(raw, str) else None
    if canonical is None:
        raise InvalidParamsError(
            'Unsupported format: {0!r}. Use "png", "jpeg" (alias "jpg") or "webp".'.format(raw)
        )
    return canonical


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


def _render_headers(envelope: Mapping[str, Any]) -> Tuple[int, Dict[str, str]]:
    """``response.status_code`` and ``response.headers`` (lower-cased names).

    The API reports the render's own status and headers there, as
    ``{"status_code": 504, "headers": [{"name": ..., "value": ...}, ...]}``.
    """
    response = envelope.get("response")
    if not isinstance(response, collections.abc.Mapping):
        return 0, {}
    try:
        inner_status = int(str(response.get("status_code")))
    except (TypeError, ValueError):
        inner_status = 0
    headers: Dict[str, str] = {}
    raw = response.get("headers")
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, collections.abc.Mapping) and isinstance(entry.get("name"), str):
                headers[entry["name"].lower()] = str(entry.get("value", ""))
    return inner_status, headers


def _is_set(value: Optional[str]) -> bool:
    return value is not None and value.strip().lower() not in ("", "0", "false", "no")


def _check_render_outcome(status: int, envelope: CaptureResult) -> None:
    """Refuse an error card the API delivered as a successful capture.

    When a render fails late (typically at its timeout) the API can answer
    HTTP 200 with no ``error`` key, an image, ``response.status_code`` >= 500
    and an ``X-WS-Error-Code`` render header. With ``X-WS-Fallback-Capture`` or
    ``X-WS-Timeout-Recovered`` set, the image is the frame the browser had when
    time ran out: real, possibly incomplete, so it is returned with a
    :class:`PartialCaptureWarning`. Without either, the image is the renderer's
    error card, and returning it would hand the user a picture of an error as
    their screenshot.
    """
    inner_status, headers = _render_headers(envelope)
    code = _error_text(headers.get("x-ws-error-code"))
    if inner_status < 500 or code is None:
        return
    if _is_set(headers.get("x-ws-fallback-capture")) or _is_set(
        headers.get("x-ws-timeout-recovered")
    ):
        warnings.warn(
            PartialCaptureWarning(
                "Site-Shot returned the frame captured when the render stopped ({0}, "
                "render status {1}); the page may not have finished loading.".format(
                    code, inner_status
                )
            ),
            stacklevel=5,
        )
        return
    error_class = SiteShotTimeoutError if _TIMEOUT_PATTERN.search(code.lower()) else APIError
    raise error_class(
        "Site-Shot returned an error card instead of a screenshot ({0}, render status "
        "{1}). The capture may still count toward your allowance.".format(code, inner_status),
        http_status=status,
        body=envelope,
        error_code=code,
    )


def _classify_error(
    status: int, error_text: Optional[str], body: Any, error_code: Optional[str] = None
) -> SiteShotError:
    error = _classify_error_without_code(status, error_text, body, error_code)
    error.error_code = error_code
    return error


def _classify_error_without_code(
    status: int, error_text: Optional[str], body: Any, error_code: Optional[str]
) -> SiteShotError:
    lower = (error_text or "").lower()
    code = (error_code or "").lower()

    if lower == "country_unavailable" or code == "country_unavailable":
        return CountryUnavailableError(
            "The requested country has no available capacity right now "
            "(country_unavailable). Retry later, or drop strict_country to allow "
            "the default US fallback.",
            http_status=status,
            body=body,
        )
    if code in _PAGE_LOAD_CODES:
        return PageLoadError(
            "Site-Shot could not open the target page ({0}): check that the address "
            "exists and is reachable from the public internet{1}.".format(
                code, " (API said: {0})".format(error_text) if error_text else ""
            ),
            http_status=status,
            body=body,
        )
    # 401 is the only status that means "the key itself was rejected": it is
    # what the API returns for a missing or unknown ``userkey``.
    if _AUTH_PATTERN.search(lower) or status == 401:
        return AuthError(
            "Site-Shot rejected the API key{0}. Check the key, or get a free one at "
            "https://www.site-shot.com/start/.".format(": " + error_text if error_text else ""),
            http_status=status,
            body=body,
        )
    if _TARGET_PATTERN.search(lower):
        return InvalidParamsError(
            "Site-Shot refused the target URL: {0}. Only public http(s) web pages "
            "can be captured.".format((error_text or "").rstrip(".")),
            http_status=status,
            body=body,
        )
    if status == 429 or _CONCURRENCY_PATTERN.search(lower):
        return ConcurrencyLimitError(
            "Site-Shot parallel-request limit reached{0}. Start the next capture when a "
            "running one finishes (the free plan runs one at a time); this request was "
            "not counted.".format(": " + error_text.rstrip(".") if error_text else ""),
            http_status=status,
            body=body,
        )
    # 403 is a *billing* state, not a key problem: the API returns it when the
    # account has no active subscription or has used up the free plan's
    # captures for the month (a country other than US on the free plan is
    # refused too, but in JSON mode as a 200 envelope matched by text above),
    # and a bad or missing key is always a 401. Classifying it as AuthError would send users to check a key that is
    # perfectly valid. (The SSRF guard's "Forbidden target address" is also a
    # 403; it is caught as a target refusal above.)
    if status in (402, 403) or _QUOTA_PATTERN.search(lower):
        # The API's own "No active subscription found" carries no next step.
        hint = (
            " Switch on the free plan at https://www.site-shot.com/start/ or choose a "
            "plan at https://www.site-shot.com/pricing/."
            if "no active subscription" in lower
            else ""
        )
        return QuotaError(
            "Site-Shot quota or payment issue{0}.{1}".format(
                ": " + error_text.rstrip(".") if error_text else " (HTTP {0})".format(status),
                hint,
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
        ``timeout`` param (or its API default of 60000 ms) plus 30 s headroom.
        That covers one render attempt; when the API retries a slow page on
        another route it can take longer, the client gives up first, and the
        abandoned capture may still count. Note the per-call ``timeout``
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
                "SITESHOT_API_KEY environment variable. Get a free key at "
                "https://www.site-shot.com/start/."
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
        and ``source_code`` (the rendered HTML) when you ask for it. When it
        was asked for and the API left it out (it does so when the render ran
        out of time), :class:`SiteShotTimeoutError` is raised, so
        ``result["source_code"]`` is always there when requested.
        """
        query = self._build_params(url, params)
        query.append(("response_type", "json"))
        endpoint = self._endpoint(query)
        timeout_s = self._resolve_timeout(params)
        result = self._request(endpoint, timeout_s)
        requested = params.get("source_code")
        if requested is not None and _is_set(str(requested)) and "source_code" not in result:
            raise SiteShotTimeoutError(
                "Site-Shot returned no source_code although it was requested; the render "
                "most likely ran out of time. The capture may still count toward your "
                "allowance.",
                body=result,
            )
        return result

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
            _warn_unknown_param(key)
            if key == "format":
                value = _normalize_format(value)
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
                        ("Site-Shot request timed out after {0:g} s "
                        "(client-side deadline). " + _CLIENT_TIMEOUT_HINT).format(timeout_s)
                    ) from exc
                if not _is_connection_error(exc):
                    raise
                if time.monotonic() >= deadline:
                    # The connection died at or after the deadline: that is the
                    # deadline doing its job (a transport may report it as a
                    # torn-down connection rather than a timeout), so report a
                    # timeout and never retry it.
                    raise SiteShotTimeoutError(
                        ("Site-Shot request timed out after {0:g} s "
                        "(client-side deadline). " + _CLIENT_TIMEOUT_HINT).format(timeout_s)
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
                    ("Site-Shot request timed out after {0:g} s while downloading the "
                    "response body (client-side deadline). " + _CLIENT_TIMEOUT_HINT).format(timeout_s),
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
                        ("Site-Shot request timed out after {0:g} s while downloading the "
                        "response body (client-side deadline). " + _CLIENT_TIMEOUT_HINT).format(timeout_s),
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
            error_code = _error_text(envelope.get("error_code"))
            if error_text is not None:
                raise _classify_error(status, error_text, envelope, error_code)

            # 2. A request rejected *before capture starts* (bad or missing
            #    key, inactive subscription) comes back on a non-2xx status with
            #    ``message``. ``message`` is deliberately NOT read on a 2xx:
            #    only that rejection path uses the key for failures, so treating
            #    it as an error signal on a successful capture would turn any
            #    future metadata field named ``message`` into a spurious raise.
            if not ok:
                raise _classify_error(
                    status, _error_text(envelope.get("message")), envelope, error_code
                )
            # 3. A late render failure can arrive with neither key: see
            #    _check_render_outcome.
            _check_render_outcome(status, envelope)
            return envelope

        if not ok:
            raise _classify_error(status, text[:500] if text else None, text)

        raise APIError(
            "Unexpected non-JSON response from the Site-Shot API (HTTP {0}).".format(status),
            http_status=status,
            body=text[:500],
        )
