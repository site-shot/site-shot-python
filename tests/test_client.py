"""Offline tests for the Site-Shot Python SDK. No test reaches the internet.

Mirrors the coverage of the Node SDK's test suite: auth placement, param
serialization and passthrough, every return mode, base64 decoding, the whole
error taxonomy (including ``country_unavailable``), and retry policy — all of it
through a stubbed transport.

The client-side deadline is the exception: it is tested against a real socket
server on loopback (``UrllibTransportTests``), because the failure it guards
against — every byte arriving inside the socket timeout, forever — only exists
below the transport seam and cannot be stubbed.
"""

import base64
import http.client
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union
from unittest import mock

try:  # Run straight from a checkout (src layout) without installing first.
    import site_shot  # noqa: F401
except ImportError:  # pragma: no cover
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from site_shot import (
    APIError,
    AuthError,
    CaptureOptions,
    CountryUnavailableError,
    InvalidParamsError,
    QuotaError,
    SiteShot,
    SiteShotError,
    SiteShotTimeoutError,
    __version__,
)
from site_shot.client import _deadline_opener, _tighten_socket_timeout

PIXELS = b"not-really-a-png-but-bytes-are-bytes"
PIXELS_B64 = base64.b64encode(PIXELS).decode("ascii")


# ---------------------------------------------------------------------------
# Stub HTTP layer
# ---------------------------------------------------------------------------


# The stub HTTP layer is annotated even though the test methods below are not:
# that is what gives the client and transport real types inside every test body,
# so mypy sees consumer-shaped calls instead of `Any`.


class FakeResponse:
    """Minimal stand-in for an ``http.client.HTTPResponse``."""

    def __init__(self, body: Union[str, bytes], status: int = 200) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        self._buffer = io.BytesIO(body)
        self.status = status
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)

    def close(self) -> None:
        self.closed = True


def json_response(payload: Any, status: int = 200) -> FakeResponse:
    return FakeResponse(json.dumps(payload), status)


class Call:
    def __init__(self, url: str, headers: Mapping[str, str], timeout: float) -> None:
        self.url = url
        self.headers = headers
        self.timeout = timeout

    @property
    def params(self) -> Dict[str, List[str]]:
        return urllib.parse.parse_qs(
            urllib.parse.urlsplit(self.url).query, keep_blank_values=True
        )

    def param(self, name: str) -> Optional[str]:
        values = self.params.get(name)
        return values[0] if values else None


class StubTransport:
    """Records calls and replies from a queue (the last reply repeats).

    A reply may be a response object, an exception instance (raised), or a
    callable ``(url, headers, timeout) -> response``.
    """

    def __init__(self, *replies: Any) -> None:
        self.calls: List[Call] = []
        self._replies: List[Any] = list(replies) or [json_response({"image": PIXELS_B64})]

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> Any:
        self.calls.append(Call(url, headers, timeout))
        reply = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            return reply(url, headers, timeout)
        return reply

    @property
    def last(self) -> Call:
        return self.calls[-1]


def make_client(*replies: Any, **kwargs: Any) -> Tuple[SiteShot, StubTransport]:
    transport = StubTransport(*replies)
    kwargs.setdefault("api_key", "test-key")
    client = SiteShot(transport=transport, **kwargs)
    return client, transport


class SiteShotTestCase(unittest.TestCase):
    def setUp(self):
        # Never let a developer's real environment leak into the tests.
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("SITESHOT_API_KEY", None)
        # Retry backoff must not slow the suite down.
        sleep_patcher = mock.patch("site_shot.client._sleep")
        self.sleep = sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)


# ---------------------------------------------------------------------------
# Constructor / auth
# ---------------------------------------------------------------------------


class ConstructorTests(SiteShotTestCase):
    def test_missing_key_raises_auth_error(self):
        with self.assertRaises(AuthError):
            SiteShot()
        with self.assertRaises(AuthError):
            SiteShot("   ")

    def test_auth_error_message_points_at_pricing(self):
        with self.assertRaises(AuthError) as ctx:
            SiteShot()
        self.assertIn("SITESHOT_API_KEY", str(ctx.exception))
        self.assertIn("https://www.site-shot.com/pricing/", str(ctx.exception))

    def test_falls_back_to_env_var(self):
        os.environ["SITESHOT_API_KEY"] = "env-key"
        transport = StubTransport(json_response({"image": PIXELS_B64}))
        client = SiteShot(transport=transport)
        client.capture("https://example.com/")
        self.assertEqual(transport.last.param("userkey"), "env-key")

    def test_explicit_key_wins_over_env(self):
        os.environ["SITESHOT_API_KEY"] = "env-key"
        client, transport = make_client(api_key="explicit-key")
        client.capture("https://example.com/")
        self.assertEqual(transport.last.param("userkey"), "explicit-key")

    def test_key_is_stripped(self):
        client, transport = make_client(api_key="  padded-key\n")
        client.capture("https://example.com/")
        self.assertEqual(transport.last.param("userkey"), "padded-key")

    def test_context_manager_is_a_noop(self):
        with make_client()[0] as client:
            self.assertIsInstance(client, SiteShot)

    def test_version_is_exported(self):
        self.assertEqual(__version__, "0.1.0")


class AuthPlacementTests(SiteShotTestCase):
    def test_key_goes_in_userkey_query_param_never_in_a_header(self):
        client, transport = make_client()
        client.capture("https://example.com/")
        call = transport.last
        self.assertEqual(call.param("userkey"), "test-key")
        lowered = {k.lower(): v for k, v in call.headers.items()}
        self.assertNotIn("authorization", lowered)
        self.assertNotIn("test-key", json.dumps(lowered))

    def test_request_advertises_json_and_the_sdk_user_agent(self):
        client, transport = make_client()
        client.capture("https://example.com/")
        headers = {k.lower(): v for k, v in transport.last.headers.items()}
        self.assertEqual(headers["accept"], "application/json")
        self.assertIn("site-shot/0.1.0", headers["user-agent"])

    def test_per_call_userkey_cannot_override_the_constructor_key(self):
        client, transport = make_client()
        client.capture("https://example.com/", userkey="other-key")
        self.assertEqual(transport.last.params["userkey"], ["test-key"])

    def test_endpoint_defaults_to_the_public_api(self):
        client, transport = make_client()
        client.capture("https://example.com/")
        self.assertTrue(transport.last.url.startswith("https://api.site-shot.com/?"))

    def test_custom_base_url_is_honoured(self):
        client, transport = make_client(base_url="https://api.example.test/shot")
        client.capture("https://example.com/")
        self.assertTrue(transport.last.url.startswith("https://api.example.test/shot?"))

    def test_base_url_that_already_has_a_query_gets_an_ampersand(self):
        client, transport = make_client(base_url="https://api.example.test/?v=2")
        client.capture("https://example.com/")
        self.assertIn("?v=2&", transport.last.url)
        self.assertEqual(transport.last.param("v"), "2")


# ---------------------------------------------------------------------------
# Param serialization
# ---------------------------------------------------------------------------


class ParamSerializationTests(SiteShotTestCase):
    def test_params_are_verbatim_booleans_coerced_unknowns_passed_through(self):
        client, transport = make_client()
        client.capture(
            "https://example.com/page",
            width=1280,
            full_size=True,
            proxy_rotation=False,
            no_ads=1,
            country="DE",
            strict_country=True,
            delay_time=0,
            language=None,
            time_zone=None,
            some_future_param="value-42",
        )
        call = transport.last
        self.assertEqual(call.param("url"), "https://example.com/page")
        self.assertEqual(call.param("width"), "1280")
        self.assertEqual(call.param("full_size"), "1")
        self.assertEqual(call.param("proxy_rotation"), "0")
        self.assertEqual(call.param("no_ads"), "1")
        self.assertEqual(call.param("country"), "DE")
        self.assertEqual(call.param("strict_country"), "1")
        self.assertEqual(call.param("delay_time"), "0")
        self.assertEqual(call.param("some_future_param"), "value-42")
        self.assertNotIn("language", call.params)
        self.assertNotIn("time_zone", call.params)

    def test_options_dict_can_be_splatted(self):
        options: CaptureOptions = {"width": 1280, "no_ads": True}
        client, transport = make_client()
        client.capture("https://example.com/", **options)
        self.assertEqual(transport.last.param("width"), "1280")
        self.assertEqual(transport.last.param("no_ads"), "1")

    def test_an_options_dict_carrying_url_splats_onto_every_method(self):
        # `CaptureOptions` declares `url`, so the exported TypedDict must splat
        # onto a call that also passes the URL positionally — the URL parameter
        # is positional-only for exactly this reason. See the typing example in
        # tests/test_typing_examples.py, which mypy --strict checks.
        options: CaptureOptions = {"url": "https://ignored.example/", "width": 800}
        # A fresh response per call: one FakeResponse body can only be read once.
        client, transport = make_client(lambda *_: json_response({"image": PIXELS_B64}))
        client.capture("https://example.com/", **options)
        self.assertEqual(transport.last.params["url"], ["https://example.com/"])
        self.assertEqual(transport.last.param("width"), "800")

        client.capture_base64("https://example.com/", **options)
        self.assertEqual(transport.last.params["url"], ["https://example.com/"])
        client.capture_json("https://example.com/", **options)
        self.assertEqual(transport.last.params["url"], ["https://example.com/"])
        url = client.build_url("https://example.com/", **options)
        self.assertIn("url=https%3A%2F%2Fexample.com%2F", url)
        self.assertNotIn("ignored.example", url)
        with tempfile.TemporaryDirectory() as directory:
            client.capture_to_file(
                "https://example.com/", os.path.join(directory, "shot.png"), **options
            )
        self.assertEqual(transport.last.params["url"], ["https://example.com/"])

    def test_capture_methods_always_request_response_type_json(self):
        client, transport = make_client()
        client.capture("https://example.com/", response_type="image")
        self.assertEqual(transport.last.params["response_type"], ["json"])

    def test_bare_domains_get_https_prepended(self):
        client, transport = make_client()
        client.capture("example.com")
        self.assertEqual(transport.last.param("url"), "https://example.com")

    def test_http_urls_are_left_alone(self):
        client, transport = make_client()
        client.capture("http://example.com/x")
        self.assertEqual(transport.last.param("url"), "http://example.com/x")

    def test_empty_url_raises_without_touching_the_network(self):
        client, transport = make_client()
        with self.assertRaises(InvalidParamsError):
            client.capture("")
        with self.assertRaises(InvalidParamsError):
            client.capture("   ")
        self.assertEqual(len(transport.calls), 0)

    def test_unparseable_url_raises_invalid_params(self):
        client, transport = make_client()
        with self.assertRaises(InvalidParamsError):
            client.capture("https://")
        with self.assertRaises(InvalidParamsError):
            client.capture("not a url")
        self.assertEqual(len(transport.calls), 0)

    def test_request_headers_become_repeated_request_header_params(self):
        client, transport = make_client()
        client.capture(
            "https://example.com/",
            request_headers={"X-First": "one", "X-Second": "two"},
        )
        call = transport.last
        self.assertEqual(call.params["request_header"], ["X-First:one", "X-Second:two"])
        self.assertNotIn("request_headers", call.params)

    def test_list_values_become_repeated_params(self):
        client, transport = make_client()
        client.capture("https://example.com/", request_header=["A:1", "B:2"])
        self.assertEqual(transport.last.params["request_header"], ["A:1", "B:2"])

    def test_tuple_values_keep_their_order(self):
        client, transport = make_client()
        client.capture("https://example.com/", request_header=("B:2", "A:1"))
        self.assertEqual(transport.last.params["request_header"], ["B:2", "A:1"])

    def test_set_values_are_sorted_so_the_query_string_is_deterministic(self):
        # Set iteration order varies between processes under string hash
        # randomization; an unsorted emit made the same call serialize
        # differently run to run.
        client, transport = make_client()
        client.capture("https://example.com/", request_header={"C:3", "A:1", "B:2"})
        self.assertEqual(transport.last.params["request_header"], ["A:1", "B:2", "C:3"])

    def test_query_strings_and_specials_survive_a_round_trip(self):
        client, transport = make_client()
        target = "https://example.com/search?q=a b&tag=c+d#frag"
        client.capture(target, javascript_code="document.title = 'a&b';")
        call = transport.last
        self.assertEqual(call.param("url"), target)
        self.assertEqual(call.param("javascript_code"), "document.title = 'a&b';")


# ---------------------------------------------------------------------------
# Return modes
# ---------------------------------------------------------------------------


class ReturnModeTests(SiteShotTestCase):
    def test_capture_returns_decoded_bytes(self):
        client, _ = make_client(json_response({"image": PIXELS_B64}))
        data = client.capture("https://example.com/")
        self.assertIsInstance(data, bytes)
        self.assertEqual(data, PIXELS)

    def test_capture_decodes_data_url_prefixed_base64(self):
        client, _ = make_client(
            json_response({"image": "data:image/png;base64," + PIXELS_B64})
        )
        self.assertEqual(client.capture("https://example.com/"), PIXELS)

    def test_capture_tolerates_whitespace_wrapped_base64(self):
        wrapped = "\n".join([PIXELS_B64[:10], PIXELS_B64[10:]])
        client, _ = make_client(json_response({"image": wrapped}))
        self.assertEqual(client.capture("https://example.com/"), PIXELS)

    def test_capture_base64_strips_any_data_url_prefix(self):
        client, _ = make_client(
            json_response({"image": "data:image/png;base64," + PIXELS_B64})
        )
        self.assertEqual(client.capture_base64("https://example.com/"), PIXELS_B64)

    def test_capture_json_returns_the_full_result(self):
        client, transport = make_client(
            json_response(
                {"image": PIXELS_B64, "source_code": "<html></html>", "extra_field": 7}
            )
        )
        meta = client.capture_json("https://example.com/", source_code=True)
        self.assertEqual(meta["image"], PIXELS_B64)
        self.assertEqual(meta["source_code"], "<html></html>")
        self.assertEqual(meta["extra_field"], 7)
        self.assertEqual(transport.last.param("source_code"), "1")

    def test_capture_to_file_writes_the_decoded_bytes(self):
        client, _ = make_client(json_response({"image": PIXELS_B64}))
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "shot.png")
            returned = client.capture_to_file("https://example.com/", path)
            self.assertEqual(returned, path)
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), PIXELS)

    def test_capture_to_file_forwards_params(self):
        client, transport = make_client(json_response({"image": PIXELS_B64}))
        with tempfile.TemporaryDirectory() as directory:
            client.capture_to_file(
                "https://example.com/", os.path.join(directory, "shot.png"), full_size=True
            )
        self.assertEqual(transport.last.param("full_size"), "1")

    def test_response_is_closed(self):
        response = json_response({"image": PIXELS_B64})
        client, _ = make_client(response)
        client.capture("https://example.com/")
        self.assertTrue(response.closed)

    def test_large_bodies_are_read_across_chunks(self):
        big = base64.b64encode(b"x" * 300_000).decode("ascii")
        client, _ = make_client(json_response({"image": big}))
        self.assertEqual(client.capture("https://example.com/"), b"x" * 300_000)


class BuildUrlTests(SiteShotTestCase):
    def test_build_url_embeds_the_key_and_never_executes(self):
        client, transport = make_client()
        url = client.build_url("example.com", width=1280, no_ads=True)
        parts = urllib.parse.urlsplit(url)
        params = urllib.parse.parse_qs(parts.query)
        self.assertEqual(parts.scheme + "://" + parts.netloc, "https://api.site-shot.com")
        self.assertEqual(params["url"], ["https://example.com"])
        self.assertEqual(params["userkey"], ["test-key"])
        self.assertEqual(params["width"], ["1280"])
        self.assertEqual(params["no_ads"], ["1"])
        self.assertNotIn("response_type", params)
        self.assertEqual(len(transport.calls), 0)

    def test_build_url_percent_escapes_reserved_characters(self):
        client, _ = make_client(api_key="key/with+special=chars&more")
        url = client.build_url(
            "https://example.com/path?a=1&b=2 3",
            javascript_code="if (a && b) { document.title='x'; }",
        )
        self.assertNotIn(" ", url)
        self.assertIn("url=https%3A%2F%2Fexample.com%2Fpath%3Fa%3D1%26b%3D2+3", url)
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(params["url"], ["https://example.com/path?a=1&b=2 3"])
        self.assertEqual(params["userkey"], ["key/with+special=chars&more"])
        self.assertEqual(
            params["javascript_code"], ["if (a && b) { document.title='x'; }"]
        )

    def test_build_url_rejects_an_empty_url(self):
        client, _ = make_client()
        with self.assertRaises(InvalidParamsError):
            client.build_url("")


# ---------------------------------------------------------------------------
# Error taxonomy
#
# The fixtures below are the API's REAL error envelopes, not invented shapes.
# The API uses two different error keys depending on how the request failed:
#
#   A. A request rejected before capture starts answers a non-2xx status with
#      {"message": "..."} - never an `error` key.
#   B. A failure during capture answers HTTP *200* and the capture envelope,
#      which carries `error` plus a placeholder error `image`. It never sets a
#      top-level `message`.
#
# Provenance of each fixture is noted inline. "live" = captured against
# https://api.site-shot.com on 2026-08-13; "derived" = reconstructed from the
# API implementation.
# ---------------------------------------------------------------------------


def app_error_envelope(message: str, internal_status: int) -> Dict[str, Any]:
    """Real during-capture failure envelope (shape B).

    HTTP 200, an ``error`` key, and a placeholder error image posing as a
    screenshot. ``response.status_code`` carries the internal failure status -
    it is NOT the HTTP status of the API response, which is 200 on this path.
    Derived from the API implementation.
    """
    return {
        "screenshot_parameters": {
            "format": "png",
            "request_headers": [],
            "response_type": "json",
            "url": "https://example.com/",
            "width": 1024,
            "height": 768,
            "zoom": 100,
            "full_size": "0",
            "no_ads": 0,
            "no_cookie_popup": 0,
            "source_code": 0,
            "proxy_rotation": "1",
        },
        "response": {"status_code": internal_status, "headers": []},
        "image": "data:image/png;base64,{0}".format(PIXELS_B64),
        "error": message,
    }


class ErrorTaxonomyTests(SiteShotTestCase):
    def test_country_unavailable_envelope_on_http_200(self):
        # derived: a strict_country capture with no capacity fails with
        # 'country_unavailable' and an internal 503, carried in the `error`
        # key. The documented public contract is likewise
        # `"error": "country_unavailable"`.
        body = app_error_envelope("country_unavailable", 503)
        client, _ = make_client(json_response(body))
        with self.assertRaises(CountryUnavailableError) as ctx:
            client.capture("https://whatismycountry.com/", country="DE", strict_country=True)
        error = ctx.exception
        self.assertIsInstance(error, SiteShotError)
        # The transport status really is 200 - the 503 lives inside the body.
        self.assertEqual(error.http_status, 200)
        self.assertEqual(error.body, body)

    def test_capture_failure_envelope_never_leaks_its_placeholder_image(self):
        # The regression this guards: the envelope carries a valid base64
        # `image`, so failing to read `error` would return the "screenshot
        # creation error" placeholder as a successful capture.
        client, _ = make_client(json_response(app_error_envelope("Screenshot capture failed", 500)))
        with self.assertRaises(SiteShotError):
            client.capture("https://example.com/")

    def test_country_unavailable_from_capture_json_too(self):
        client, _ = make_client(json_response(app_error_envelope("country_unavailable", 503)))
        with self.assertRaises(CountryUnavailableError):
            client.capture_json("https://example.com/", country="JP", strict_country=True)

    def test_real_401_envelopes_raise_auth_error_with_the_message(self):
        cases = [
            # live: curl "https://api.site-shot.com/?url=...&userkey=<invalid>"
            {"message": "Invalid authentication credentials"},
            # live: same URL with the userkey param omitted or empty
            {"message": "No API key found in request"},
        ]
        for body in cases:
            client, _ = make_client(json_response(body, 401))
            with self.assertRaises(AuthError) as ctx:
                client.capture("https://example.com/")
            error = ctx.exception
            self.assertEqual(error.http_status, 401)
            self.assertEqual(error.body, body)
            # The whole point of the fix: the API's message must survive into
            # the error text instead of being silently dropped.
            self.assertIn(body["message"], str(error))

    def test_real_403_envelope_raises_quota_error_not_auth_error(self):
        # derived: an account whose subscription is inactive is rejected with
        # 403 and {"message": "No active subscription found"}. Not reproducible
        # live without such an account.
        #
        # 403 is a billing state, not a key problem - the key is valid, the
        # subscription lapsed - so it must NOT tell the user to check their key.
        body = {"message": "No active subscription found"}
        client, _ = make_client(json_response(body, 403))
        with self.assertRaises(QuotaError) as ctx:
            client.capture("https://example.com/")
        error = ctx.exception
        self.assertNotIsInstance(error, AuthError)
        self.assertEqual(error.http_status, 403)
        self.assertEqual(error.body, body)
        self.assertIn("No active subscription found", str(error))

    def test_message_on_a_successful_2xx_capture_is_metadata_not_an_error(self):
        # `message` only signals failure on a non-2xx rejection, so it must
        # never turn a successful capture into a raise.
        client, _ = make_client(
            json_response({"image": PIXELS_B64, "message": "rendered from DE"})
        )
        self.assertEqual(client.capture("https://example.com/"), PIXELS)

    def test_error_key_wins_over_a_sibling_message_key(self):
        client, _ = make_client(
            json_response({"error": "country_unavailable", "message": "informational"})
        )
        with self.assertRaises(CountryUnavailableError):
            client.capture("https://example.com/")

    def test_rejections_classify_by_status_without_body_text(self):
        # Defensive: a 401/403 whose body the SDK cannot mine for a message must
        # still classify by status rather than fall through to APIError - 401 as
        # a key problem, 403 as a subscription problem.
        for status, expected in ((401, AuthError), (403, QuotaError)):
            client, _ = make_client(json_response({}, status))
            with self.assertRaises(expected):
                client.capture("https://example.com/")

    def test_http_402_and_429_raise_quota_error(self):
        # NOTE: no evidence api.site-shot.com currently emits either status -
        # no request rate limiting is applied, and the capture path never sets a
        # non-2xx status in json mode. These stay as defensive status-only
        # mappings; the bodies use the same shape as every other non-2xx
        # rejection.
        for status in (402, 429):
            client, _ = make_client(json_response({"message": "API rate limit exceeded"}, status))
            with self.assertRaises(QuotaError):
                client.capture("https://example.com/")

    def test_quota_flavoured_capture_failure(self):
        client, _ = make_client(json_response(app_error_envelope("monthly quota exceeded", 402)))
        with self.assertRaises(QuotaError):
            client.capture("https://example.com/")

    def test_param_flavoured_capture_failure(self):
        client, _ = make_client(json_response(app_error_envelope("width out of range", 400)))
        with self.assertRaises(InvalidParamsError):
            client.capture("https://example.com/", width=9)

    def test_invalid_flavoured_capture_failure(self):
        client, _ = make_client(json_response(app_error_envelope("unsupported format", 400)))
        with self.assertRaises(InvalidParamsError):
            client.capture("https://example.com/", format="webp")

    def test_render_timeout_reported_by_the_api(self):
        client, _ = make_client(json_response(app_error_envelope("Render timed out", 504)))
        with self.assertRaises(SiteShotTimeoutError):
            client.capture("https://example.com/")

    def test_5xx_in_the_apis_json_error_shape_surfaces_its_message(self):
        # The same {"message": ...} envelope is used for upstream failures as
        # for rejections, so a 502/503 must be mined the same way.
        client, _ = make_client(
            json_response({"message": "Service temporarily unavailable"}, 503)
        )
        with self.assertRaises(APIError) as ctx:
            client.capture("https://example.com/")
        self.assertEqual(ctx.exception.http_status, 503)
        self.assertIn("Service temporarily unavailable", str(ctx.exception))

    def test_http_500_raises_api_error_with_status_and_body(self):
        # A non-JSON 5xx is what an HTML error page from the edge looks like.
        client, _ = make_client(FakeResponse("upstream exploded", 500))
        with self.assertRaises(APIError) as ctx:
            client.capture("https://example.com/")
        self.assertEqual(ctx.exception.http_status, 500)
        self.assertIn("upstream exploded", str(ctx.exception.body))

    def test_http_200_with_a_non_json_body_raises_api_error(self):
        client, _ = make_client(FakeResponse("<html>error page</html>", 200))
        with self.assertRaises(APIError):
            client.capture("https://example.com/")

    def test_json_result_without_an_image_raises_api_error(self):
        client, _ = make_client(json_response({"status": "ok but empty"}))
        with self.assertRaises(APIError):
            client.capture("https://example.com/")

    def test_capture_json_tolerates_a_result_without_an_image(self):
        client, _ = make_client(json_response({"source_code": "<html></html>"}))
        meta = client.capture_json("https://example.com/", source_code=True)
        self.assertEqual(meta["source_code"], "<html></html>")

    def test_malformed_base64_raises_api_error_instead_of_corrupt_bytes(self):
        for image in ("@@@@ not base64 @@@@", "%%%%", "AAAAA"):
            client, _ = make_client(json_response({"image": image}))
            with self.assertRaises(APIError):
                client.capture("https://example.com/")

    def test_empty_image_payload_raises_api_error(self):
        client, _ = make_client(json_response({"image": ""}))
        with self.assertRaises(APIError):
            client.capture("https://example.com/")

    def test_connection_failure_raises_api_error_with_the_cause_attached(self):
        client, _ = make_client(urllib.error.URLError("name resolution failed"))
        with self.assertRaises(APIError) as ctx:
            client.capture("https://example.com/")
        self.assertIsInstance(ctx.exception.__cause__, urllib.error.URLError)

    def test_every_error_is_a_site_shot_error(self):
        for error_class in (
            AuthError,
            QuotaError,
            CountryUnavailableError,
            InvalidParamsError,
            SiteShotTimeoutError,
            APIError,
        ):
            self.assertTrue(issubclass(error_class, SiteShotError))


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------


class StallingBodyResponse:
    """A response whose body drips forever, one byte per read."""

    def __init__(self, status=200, delay=0.01):
        self.status = status
        self.delay = delay
        self.reads = 0

    def read(self, size=-1):
        self.reads += 1
        time.sleep(self.delay)
        return b"x"

    def close(self):
        pass


class DrippingHTTPResponse:
    """Mimics ``http.client.HTTPResponse``: ``read(n)`` blocks until it has all
    *n* bytes, while ``read1(n)`` returns as soon as a byte trickles in.

    Reading such a body with ``read`` would take ~an hour of a one-byte drip
    before the SDK ever got to look at its deadline again.
    """

    status = 200

    def __init__(self, delay=0.01):
        self.delay = delay
        self.read_calls = 0
        self.read1_calls = 0

    def read(self, size=-1):
        self.read_calls += 1
        time.sleep(3600)  # blocks until `size` bytes have accumulated
        return b""

    def read1(self, size=-1):
        self.read1_calls += 1
        time.sleep(self.delay)
        return b"x"

    def close(self):
        pass


class TimeoutTests(SiteShotTestCase):
    def test_connect_timeout_raises_site_shot_timeout_error(self):
        client, transport = make_client(socket.timeout("timed out"), timeout=0.05)
        with self.assertRaises(SiteShotTimeoutError):
            client.capture("https://example.com/")
        self.assertEqual(len(transport.calls), 1)

    def test_url_error_wrapping_a_timeout_is_classified_as_a_timeout(self):
        client, _ = make_client(urllib.error.URLError(socket.timeout("timed out")))
        with self.assertRaises(SiteShotTimeoutError):
            client.capture("https://example.com/")

    def test_client_deadline_bounds_the_body_download(self):
        # The response headers arrive instantly; the body never ends. A deadline
        # that only covered the headers would hang here forever.
        client, transport = make_client(StallingBodyResponse(), timeout=0.2, retries=3)
        started = time.monotonic()
        with self.assertRaises(SiteShotTimeoutError) as ctx:
            client.capture("https://example.com/")
        elapsed = time.monotonic() - started
        self.assertIn("body", str(ctx.exception))
        self.assertLess(elapsed, 3.0)
        self.assertEqual(len(transport.calls), 1)  # never retried

    def test_a_trickling_body_is_read_with_read1_so_the_deadline_is_checked(self):
        # Regression guard: read(n) blocks until it has n bytes, so a slow drip
        # would never return to the deadline check. read1 must be preferred.
        response = DrippingHTTPResponse()
        client, transport = make_client(response, timeout=0.2)
        started = time.monotonic()
        with self.assertRaises(SiteShotTimeoutError):
            client.capture("https://example.com/")
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(response.read_calls, 0)
        self.assertGreater(response.read1_calls, 1)
        self.assertEqual(len(transport.calls), 1)

    def test_timeout_while_reading_the_body_is_classified_as_a_timeout(self):
        class TimingOutBody:
            status = 200

            def read(self, size=-1):
                raise socket.timeout("read timed out")

            def close(self):
                pass

        client, transport = make_client(TimingOutBody(), retries=2)
        with self.assertRaises(SiteShotTimeoutError):
            client.capture("https://example.com/")
        self.assertEqual(len(transport.calls), 1)

    def test_broken_body_read_raises_api_error_and_is_not_retried(self):
        class BrokenBody:
            status = 200

            def read(self, size=-1):
                raise ConnectionResetError("peer went away")

            def close(self):
                pass

        client, transport = make_client(BrokenBody(), retries=2)
        with self.assertRaises(APIError):
            client.capture("https://example.com/")
        self.assertEqual(len(transport.calls), 1)

    def test_default_client_timeout_is_the_server_deadline_plus_headroom(self):
        client, transport = make_client()
        client.capture("https://example.com/")
        self.assertAlmostEqual(transport.last.timeout, 90.0)

    def test_default_client_timeout_follows_the_server_timeout_param(self):
        client, transport = make_client()
        client.capture("https://example.com/", timeout=120000)
        self.assertAlmostEqual(transport.last.timeout, 150.0)

    def test_explicit_client_timeout_wins(self):
        client, transport = make_client(timeout=5)
        client.capture("https://example.com/", timeout=120000)
        self.assertAlmostEqual(transport.last.timeout, 5.0)


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------


class RetryTests(SiteShotTestCase):
    def test_connection_errors_are_not_retried_by_default(self):
        client, transport = make_client(urllib.error.URLError("connection reset"))
        with self.assertRaises(APIError):
            client.capture("https://example.com/")
        self.assertEqual(len(transport.calls), 1)
        self.sleep.assert_not_called()

    def test_retries_two_recovers_from_connection_errors(self):
        client, transport = make_client(
            urllib.error.URLError("connection reset"),
            ConnectionResetError("reset"),
            json_response({"image": PIXELS_B64}),
            retries=2,
        )
        self.assertEqual(client.capture("https://example.com/"), PIXELS)
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_exhausted_retries_raise_api_error_with_the_cause(self):
        client, transport = make_client(urllib.error.URLError("connection reset"), retries=1)
        with self.assertRaises(APIError) as ctx:
            client.capture("https://example.com/")
        self.assertIsInstance(ctx.exception.__cause__, urllib.error.URLError)
        self.assertEqual(len(transport.calls), 2)

    def test_backoff_is_jittered_and_bounded(self):
        from site_shot.client import _backoff_delay

        for attempt in range(1, 8):
            delay = _backoff_delay(attempt)
            self.assertGreater(delay, 0)
            self.assertLessEqual(delay, 2.0)

    def test_http_error_statuses_are_never_retried(self):
        client, transport = make_client(FakeResponse("boom", 500), retries=3)
        with self.assertRaises(APIError):
            client.capture("https://example.com/")
        self.assertEqual(len(transport.calls), 1)

    def test_in_band_errors_are_never_retried(self):
        client, transport = make_client(
            json_response(app_error_envelope("country_unavailable", 503)), retries=3
        )
        with self.assertRaises(CountryUnavailableError):
            client.capture("https://example.com/", country="DE", strict_country=True)
        self.assertEqual(len(transport.calls), 1)

    def test_programming_errors_are_not_swallowed_as_connection_failures(self):
        client, transport = make_client(ValueError("bug in a callback"), retries=3)
        with self.assertRaises(ValueError):
            client.capture("https://example.com/")
        self.assertEqual(len(transport.calls), 1)


# ---------------------------------------------------------------------------
# Default (urllib) transport — driven against a real socket server
# ---------------------------------------------------------------------------


DRIP_INTERVAL = 0.05
#: Long enough that a client which only bounds *individual socket reads* keeps
#: waiting well past every assertion below, short enough not to stall the suite.
DRIP_ROUNDS = 200


def send_all(connection, payload):
    try:
        connection.sendall(payload)
        return True
    except OSError:  # the client hung up — expected once its deadline fires
        return False


def reply_json(payload, status=200, reason="OK"):
    """Script: one complete, immediate HTTP response."""

    def script(connection, stop):
        body = json.dumps(payload).encode("utf-8")
        head = (
            "HTTP/1.1 {0} {1}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: {2}\r\n"
            "Connection: close\r\n\r\n"
        ).format(status, reason, len(body)).encode("ascii")
        send_all(connection, head + body)

    return script


def drip_headers(connection, stop):
    """Script: a response whose *headers* arrive one byte at a time.

    Every byte lands inside any sane per-socket-operation timeout, so a client
    that mistakes urllib's ``timeout`` for a deadline waits
    ``header_bytes x timeout`` seconds — which is not a bound at all.
    """
    send_all(connection, b"HTTP/1.1 200 OK\r\n")
    for index in range(DRIP_ROUNDS):
        if stop.wait(DRIP_INTERVAL):
            return
        if not send_all(connection, "X-Pad-{0}: x\r\n".format(index).encode("ascii")):
            return


def drip_body(status=200, reason="OK"):
    """Script: headers land at once, then the body trickles forever."""

    def script(connection, stop):
        head = (
            "HTTP/1.1 {0} {1}\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 4096\r\n"
            "Connection: close\r\n\r\n"
        ).format(status, reason).encode("ascii")
        if not send_all(connection, head):
            return
        for _ in range(DRIP_ROUNDS):
            if stop.wait(DRIP_INTERVAL):
                return
            if not send_all(connection, b"x"):
                return

    return script


class LocalHTTPServer:
    """A real HTTP server on localhost, scripted at the byte level.

    Sockets are the only honest way to test a deadline: a stubbed transport
    cannot reproduce "each byte arrives inside the socket timeout, forever",
    which is exactly the shape that used to pin the client open.
    """

    def __init__(self, script):
        self._script = script
        self.requests = []
        self.stop = threading.Event()
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(5)
        self._socket.settimeout(0.1)
        self.port = self._socket.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    @property
    def base_url(self):
        return "http://127.0.0.1:{0}/".format(self.port)

    def _accept_loop(self):
        while not self.stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:  # pragma: no cover - socket closed under us
                return
            worker = threading.Thread(target=self._handle, args=(connection,), daemon=True)
            worker.start()

    def _handle(self, connection):
        try:
            connection.settimeout(5.0)
            request = b""
            while b"\r\n\r\n" not in request:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                request += chunk
            self.requests.append(request.decode("latin-1"))
            self._script(connection, self.stop)
        except OSError:  # pragma: no cover - client hung up first
            pass
        finally:
            try:
                connection.close()
            except OSError:  # pragma: no cover
                pass

    def close(self):
        self.stop.set()
        try:
            self._socket.close()
        except OSError:  # pragma: no cover
            pass
        self._thread.join(timeout=2.0)


class UrllibTransportTests(SiteShotTestCase):
    def setUp(self):
        super().setUp()
        # Never route the loopback tests through a developer's or CI's proxy.
        proxy_patcher = mock.patch.dict(
            os.environ, {"no_proxy": "*", "NO_PROXY": "*"}, clear=False
        )
        proxy_patcher.start()
        self.addCleanup(proxy_patcher.stop)
        for name in ("http_proxy", "HTTP_PROXY", "all_proxy", "ALL_PROXY"):
            os.environ.pop(name, None)

    def serve(self, script):
        server = LocalHTTPServer(script)
        self.addCleanup(server.close)
        return server

    def test_default_transport_issues_a_get_with_the_sdk_headers(self):
        server = self.serve(reply_json({"image": PIXELS_B64}))
        client = SiteShot("test-key", base_url=server.base_url)
        self.assertEqual(client.capture("https://example.com/", width=1280), PIXELS)
        request = server.requests[0]
        request_line = request.splitlines()[0]
        self.assertTrue(request_line.startswith("GET /?"))
        self.assertIn("userkey=test-key", request_line)
        self.assertIn("width=1280", request_line)
        lowered = request.lower()
        self.assertIn("accept: application/json", lowered)
        self.assertIn("user-agent: site-shot/0.1.0 python", lowered)

    def test_http_error_status_is_treated_as_a_response_not_a_connection_failure(self):
        # Real 403 shape: the API's `message` key.
        server = self.serve(
            reply_json({"message": "No active subscription found"}, 403, "Forbidden")
        )
        client = SiteShot("test-key", base_url=server.base_url)
        with self.assertRaises(QuotaError) as ctx:
            client.capture("https://example.com/")
        self.assertEqual(ctx.exception.http_status, 403)

    def test_a_refused_connection_surfaces_as_api_error(self):
        closed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()
        client = SiteShot("test-key", base_url="http://127.0.0.1:{0}/".format(port))
        with self.assertRaises(APIError) as ctx:
            client.capture("https://example.com/")
        self.assertNotIsInstance(ctx.exception, SiteShotTimeoutError)

    def test_client_deadline_bounds_the_header_phase(self):
        # Regression guard for the deadline that only covered the body: urllib's
        # `timeout` is per socket operation, so a server dripping headers held
        # the client for header_bytes x timeout seconds (measured: 27 s for a
        # 68-byte drip against a 2 s "deadline", growing with the header size).
        server = self.serve(drip_headers)
        client = SiteShot("test-key", base_url=server.base_url, timeout=1.0, retries=3)
        started = time.monotonic()
        with self.assertRaises(SiteShotTimeoutError):
            client.capture("https://example.com/")
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 4.0)
        self.assertEqual(len(server.requests), 1)  # a spent render is never retried

    def test_client_deadline_bounds_a_dripping_body(self):
        server = self.serve(drip_body())
        client = SiteShot("test-key", base_url=server.base_url, timeout=1.0)
        started = time.monotonic()
        with self.assertRaises(SiteShotTimeoutError):
            client.capture("https://example.com/")
        self.assertLess(time.monotonic() - started, 4.0)

    def test_client_deadline_bounds_a_dripping_body_on_an_error_status(self):
        # The error path returns an HTTPError, whose internals are shaped
        # differently from a success-path HTTPResponse — it used to escape the
        # socket-timeout tightening and overshoot the deadline.
        server = self.serve(drip_body(500, "Internal Server Error"))
        client = SiteShot("test-key", base_url=server.base_url, timeout=1.0)
        started = time.monotonic()
        with self.assertRaises(SiteShotTimeoutError):
            client.capture("https://example.com/")
        self.assertLess(time.monotonic() - started, 4.0)


class FakeSocket:
    """Just enough socket for ``HTTPResponse``: a canned byte stream."""

    def __init__(self, data):
        self._data = data
        self.timeouts = []

    def makefile(self, mode="rb", *args, **kwargs):
        return io.BufferedReader(io.BytesIO(self._data))

    def settimeout(self, value):
        self.timeouts.append(value)


class DeadlineOpenerTests(SiteShotTestCase):
    """The deadline must reach https:// — the scheme every real capture uses.

    The socket-level tests above run over http:// on loopback, so these pin the
    wiring itself: both schemes get the same deadline-bound response class.
    """

    RESPONSE = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"

    def connection_classes(self, deadline):
        opener = _deadline_opener(deadline)
        captured = {}

        def fake_do_open(handler, connection_class, req, **kwargs):
            captured[req.type] = connection_class
            return None

        with mock.patch.object(urllib.request.AbstractHTTPHandler, "do_open", fake_do_open):
            for handler in getattr(opener, "handlers"):  # not in typeshed, real since 2.x
                if isinstance(handler, urllib.request.HTTPSHandler):
                    handler.https_open(urllib.request.Request("https://api.site-shot.com/"))
                elif isinstance(handler, urllib.request.HTTPHandler):
                    handler.http_open(urllib.request.Request("http://api.site-shot.com/"))
        return captured

    def test_both_schemes_get_a_deadline_bound_connection(self):
        captured = self.connection_classes(time.monotonic() + 30.0)
        self.assertEqual(set(captured), {"http", "https"})
        self.assertTrue(issubclass(captured["http"], http.client.HTTPConnection))
        self.assertTrue(issubclass(captured["https"], http.client.HTTPSConnection))
        self.assertIs(captured["http"].response_class, captured["https"].response_class)
        self.assertIsNot(captured["https"].response_class, http.client.HTTPResponse)

    def test_a_live_deadline_re_arms_the_socket_on_every_read(self):
        response_class = self.connection_classes(time.monotonic() + 30.0)["https"].response_class
        sock = FakeSocket(self.RESPONSE)
        response = response_class(sock)
        response.begin()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b"ok")
        # Headers, not just the body: the status line alone re-armed the socket.
        self.assertTrue(sock.timeouts)
        self.assertTrue(all(0 < value <= 30.0 for value in sock.timeouts))

    def test_an_expired_deadline_stops_the_header_read(self):
        response_class = self.connection_classes(time.monotonic() - 1.0)["https"].response_class
        response = response_class(FakeSocket(self.RESPONSE))
        with self.assertRaises(socket.timeout):
            response.begin()


class SocketTighteningTests(SiteShotTestCase):
    """The deadline brake must find the socket on both response shapes."""

    class Sock:
        def __init__(self):
            self.timeout = None

        def settimeout(self, value):
            self.timeout = value

    def http_response(self, sock):
        """The success-path shape: ``response.fp`` is the buffered reader."""
        raw = types.SimpleNamespace(_sock=sock)
        return types.SimpleNamespace(fp=types.SimpleNamespace(raw=raw))

    def test_success_path_response_socket_is_tightened(self):
        sock = self.Sock()
        _tighten_socket_timeout(self.http_response(sock), 1.5)
        self.assertEqual(sock.timeout, 1.5)

    def test_error_path_response_socket_is_tightened(self):
        # urllib.error.HTTPError.fp *is* the HTTPResponse, so the socket sits
        # one level deeper than on the success path.
        sock = self.Sock()
        _tighten_socket_timeout(types.SimpleNamespace(fp=self.http_response(sock)), 1.5)
        self.assertEqual(sock.timeout, 1.5)

    def test_unknown_response_shape_is_left_alone(self):
        _tighten_socket_timeout(object(), 1.5)  # must not raise


if __name__ == "__main__":
    unittest.main()
