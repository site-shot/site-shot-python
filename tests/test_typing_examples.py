"""The documented examples, executed *and* type-checked.

``mypy --strict`` covers this file (see ``[tool.mypy]`` in ``pyproject.toml``),
which is the point: the exported ``CaptureOptions`` ``TypedDict`` only earns its
place in a ``py.typed`` package if a caller's option dict actually type checks
against the capture methods. It did not — a dict carrying ``url`` collided with
the ``url`` parameter (``"capture" gets multiple values for keyword argument
"url"``) — and no test caught it, because that failure is invisible at runtime
and the suite only ever splatted dicts without a ``url`` key.

So: every snippet the README teaches is reproduced here with explicit result
annotations. A documented call that stops type checking now fails CI.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from typing import Any, Dict, List, Mapping

try:  # Run straight from a checkout (src layout) without installing first.
    import site_shot  # noqa: F401
except ImportError:  # pragma: no cover
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from site_shot import (
    CaptureOptions,
    CaptureResult,
    CountryUnavailableError,
    QuotaError,
    SiteShot,
    SiteShotError,
)

PIXELS_B64 = "aGVsbG8td29ybGQ="  # b"hello-world"
PIXELS = b"hello-world"


class StubResponse:
    """Typed stand-in for an HTTP response — this file never touches a socket."""

    status = 200

    def __init__(self, payload: Dict[str, Any]) -> None:
        self._buffer = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(size)

    def close(self) -> None:
        return None


class StubTransport:
    def __init__(self, payload: Dict[str, Any]) -> None:
        self.payload = payload
        self.urls: List[str] = []

    def __call__(self, url: str, headers: Mapping[str, str], timeout: float) -> Any:
        self.urls.append(url)
        return StubResponse(self.payload)


def make_client(**kwargs: Any) -> "tuple[SiteShot, StubTransport]":
    transport = StubTransport({"image": PIXELS_B64, "source_code": "<html></html>"})
    client = SiteShot("test-key", transport=transport, **kwargs)
    return client, transport


class ReadmeExamplesTypeCheck(unittest.TestCase):
    def test_quickstart_returns_bytes(self) -> None:
        client, _ = make_client()
        png: bytes = client.capture("example.com", full_size=True)
        self.assertEqual(png, PIXELS)

    def test_every_return_mode_has_the_documented_type(self) -> None:
        client, _ = make_client()
        png: bytes = client.capture("https://example.com/")
        encoded: str = client.capture_base64("https://example.com/")
        meta: CaptureResult = client.capture_json("https://example.com/", source_code=True)
        url: str = client.build_url("https://example.com/", width=1280)
        self.assertEqual(png, PIXELS)
        self.assertEqual(encoded, PIXELS_B64)
        self.assertEqual(meta["source_code"], "<html></html>")
        self.assertIn("width=1280", url)

        with tempfile.TemporaryDirectory() as directory:
            path: str = client.capture_to_file(
                "https://example.com/", os.path.join(directory, "shot.png")
            )
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), PIXELS)

    def test_capture_options_dict_splats_onto_every_method(self) -> None:
        # The README's snippet, verbatim.
        options: CaptureOptions = {"width": 1280, "full_size": True, "country": "DE"}
        client, transport = make_client()
        png: bytes = client.capture("https://example.com/", **options)
        self.assertEqual(png, PIXELS)
        self.assertIn("width=1280", transport.urls[-1])

    def test_capture_options_dict_may_carry_url_like_the_typed_dict_declares(self) -> None:
        # `CaptureOptions` declares `url`, so this must type check as well as
        # run. The positional argument wins, exactly like `userkey`.
        options: CaptureOptions = {"url": "https://ignored.example/", "width": 800}
        client, transport = make_client()
        self.assertEqual(client.capture("https://example.com/", **options), PIXELS)
        self.assertEqual(client.capture_base64("https://example.com/", **options), PIXELS_B64)
        self.assertIsInstance(client.capture_json("https://example.com/", **options), dict)
        built: str = client.build_url("https://example.com/", **options)
        self.assertIn("url=https%3A%2F%2Fexample.com%2F", built)
        with tempfile.TemporaryDirectory() as directory:
            client.capture_to_file(
                "https://example.com/", os.path.join(directory, "shot.png"), **options
            )
        for requested in transport.urls:
            self.assertNotIn("ignored.example", requested)

    def test_client_options_and_context_manager(self) -> None:
        transport = StubTransport({"image": PIXELS_B64})
        with SiteShot(
            "YOUR_API_KEY",
            base_url="https://api.site-shot.com/",
            timeout=90,
            retries=2,
            transport=transport,
        ) as client:
            self.assertEqual(client.capture("https://example.com/"), PIXELS)

    def test_error_types_are_catchable_as_documented(self) -> None:
        transport = StubTransport({"error": "country_unavailable"})
        client = SiteShot("test-key", transport=transport)
        caught: SiteShotError
        try:
            client.capture(
                "https://whatismycountry.com/", country="DE", strict_country=True
            )
        except CountryUnavailableError as exc:
            caught = exc
        except QuotaError as exc:  # pragma: no cover - documented sibling branch
            caught = exc
        except SiteShotError as exc:  # pragma: no cover - documented catch-all
            caught = exc
        else:  # pragma: no cover
            self.fail("expected CountryUnavailableError")
        self.assertEqual(caught.http_status, 200)
        self.assertIsInstance(caught.body, dict)


if __name__ == "__main__":
    unittest.main()
