"""Offline tests for the Site-Shot Python SDK — the HTTP layer is fully stubbed.

Mirrors the coverage of the Node SDK's test suite: auth placement, param
serialization and passthrough, every return mode, base64 decoding, the whole
error taxonomy (including ``country_unavailable``), retry policy, and the
client-side deadline covering the body download.
"""

import base64
import io
import json
import os
import socket
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
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

PIXELS = b"not-really-a-png-but-bytes-are-bytes"
PIXELS_B64 = base64.b64encode(PIXELS).decode("ascii")


# ---------------------------------------------------------------------------
# Stub HTTP layer
# ---------------------------------------------------------------------------


class FakeResponse:
    """Minimal stand-in for an ``http.client.HTTPResponse``."""

    def __init__(self, body, status=200):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self._buffer = io.BytesIO(body)
        self.status = status
        self.closed = False

    def read(self, size=-1):
        return self._buffer.read(size)

    def close(self):
        self.closed = True


def json_response(payload, status=200):
    return FakeResponse(json.dumps(payload), status)


class Call:
    def __init__(self, url, headers, timeout):
        self.url = url
        self.headers = headers
        self.timeout = timeout

    @property
    def params(self):
        return urllib.parse.parse_qs(
            urllib.parse.urlsplit(self.url).query, keep_blank_values=True
        )

    def param(self, name):
        values = self.params.get(name)
        return values[0] if values else None


class StubTransport:
    """Records calls and replies from a queue (the last reply repeats).

    A reply may be a response object, an exception instance (raised), or a
    callable ``(url, headers, timeout) -> response``.
    """

    def __init__(self, *replies):
        self.calls = []
        self._replies = list(replies) or [json_response({"image": PIXELS_B64})]

    def __call__(self, url, headers, timeout):
        self.calls.append(Call(url, headers, timeout))
        reply = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
        if isinstance(reply, BaseException):
            raise reply
        if callable(reply):
            return reply(url, headers, timeout)
        return reply

    @property
    def last(self):
        return self.calls[-1]


def make_client(*replies, **kwargs):
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
# ---------------------------------------------------------------------------


class ErrorTaxonomyTests(SiteShotTestCase):
    def test_country_unavailable_envelope_on_http_200(self):
        client, _ = make_client(json_response({"error": "country_unavailable"}))
        with self.assertRaises(CountryUnavailableError) as ctx:
            client.capture("https://whatismycountry.com/", country="DE", strict_country=True)
        error = ctx.exception
        self.assertIsInstance(error, SiteShotError)
        self.assertEqual(error.http_status, 200)
        self.assertEqual(error.body, {"error": "country_unavailable"})

    def test_country_unavailable_envelope_on_a_404(self):
        client, _ = make_client(json_response({"error": "country_unavailable"}, 404))
        with self.assertRaises(CountryUnavailableError):
            client.capture("https://whatismycountry.com/", country="BR", strict_country=True)

    def test_country_unavailable_from_capture_json_too(self):
        client, _ = make_client(json_response({"error": "country_unavailable"}))
        with self.assertRaises(CountryUnavailableError):
            client.capture_json("https://example.com/", country="JP", strict_country=True)

    def test_http_401_and_403_raise_auth_error(self):
        for status in (401, 403):
            client, _ = make_client(json_response({"error": "invalid userkey"}, status))
            with self.assertRaises(AuthError):
                client.capture("https://example.com/")

    def test_auth_flavoured_in_band_error_on_http_200(self):
        client, _ = make_client(json_response({"error": "Invalid userkey"}))
        with self.assertRaises(AuthError):
            client.capture("https://example.com/")

    def test_http_402_and_429_raise_quota_error(self):
        for status in (402, 429):
            client, _ = make_client(FakeResponse("limit", status))
            with self.assertRaises(QuotaError):
                client.capture("https://example.com/")

    def test_quota_flavoured_in_band_error(self):
        client, _ = make_client(json_response({"error": "monthly quota exceeded"}))
        with self.assertRaises(QuotaError):
            client.capture("https://example.com/")

    def test_http_400_raises_invalid_params(self):
        client, _ = make_client(json_response({"error": "width out of range"}, 400))
        with self.assertRaises(InvalidParamsError):
            client.capture("https://example.com/", width=9)

    def test_invalid_flavoured_in_band_error(self):
        client, _ = make_client(json_response({"error": "unsupported format"}))
        with self.assertRaises(InvalidParamsError):
            client.capture("https://example.com/", format="webp")

    def test_render_timeout_reported_by_the_api(self):
        client, _ = make_client(json_response({"error": "Render timed out"}))
        with self.assertRaises(SiteShotTimeoutError):
            client.capture("https://example.com/")

    def test_http_500_raises_api_error_with_status_and_body(self):
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
        client, transport = make_client(json_response({"error": "country_unavailable"}), retries=3)
        with self.assertRaises(CountryUnavailableError):
            client.capture("https://example.com/", country="DE", strict_country=True)
        self.assertEqual(len(transport.calls), 1)

    def test_programming_errors_are_not_swallowed_as_connection_failures(self):
        client, transport = make_client(ValueError("bug in a callback"), retries=3)
        with self.assertRaises(ValueError):
            client.capture("https://example.com/")
        self.assertEqual(len(transport.calls), 1)


# ---------------------------------------------------------------------------
# Default (urllib) transport
# ---------------------------------------------------------------------------


class UrllibTransportTests(SiteShotTestCase):
    def test_default_transport_issues_a_urllib_get(self):
        captured = {}

        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["headers"] = request.headers
            captured["timeout"] = timeout
            return json_response({"image": PIXELS_B64})

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            client = SiteShot("test-key")
            self.assertEqual(client.capture("https://example.com/"), PIXELS)

        self.assertEqual(captured["method"], "GET")
        self.assertTrue(captured["url"].startswith("https://api.site-shot.com/?"))
        self.assertAlmostEqual(captured["timeout"], 90.0)
        lowered = {k.lower(): v for k, v in captured["headers"].items()}
        self.assertEqual(lowered["accept"], "application/json")

    def test_http_error_is_treated_as_a_response_not_a_connection_failure(self):
        body = json.dumps({"error": "invalid userkey"}).encode("utf-8")
        error = urllib.error.HTTPError(
            "https://api.site-shot.com/", 403, "Forbidden", {}, io.BytesIO(body)
        )

        def fake_urlopen(request, timeout=None):
            raise error

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            client = SiteShot("test-key")
            with self.assertRaises(AuthError) as ctx:
                client.capture("https://example.com/")
        self.assertEqual(ctx.exception.http_status, 403)

    def test_url_error_surfaces_as_api_error(self):
        def fake_urlopen(request, timeout=None):
            raise urllib.error.URLError("nodename nor servname provided")

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            client = SiteShot("test-key")
            with self.assertRaises(APIError):
                client.capture("https://example.com/")


if __name__ == "__main__":
    unittest.main()
