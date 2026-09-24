#!/usr/bin/env python3
"""Live smoke test against the real Site-Shot API.

Gated on SITESHOT_API_KEY: without the env var it prints SKIP and exits 0, so it
is safe anywhere but only meaningful with a real key. Each run spends render
quota, so it performs a single capture. Not wired into CI on purpose — run it
manually as part of the publish checklist:

    SITESHOT_API_KEY=... python scripts/smoke.py
"""

from __future__ import annotations

import os
import sys

try:
    from site_shot import SiteShot
except ImportError:  # run straight from a checkout
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
    from site_shot import SiteShot

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
#: A real render of example.com is far bigger than any error blob.
MIN_PLAUSIBLE_BYTES = 5_000


def _check_png(data: bytes) -> str | None:
    """Return a failure message, or None if `data` is a plausible PNG."""
    if not data.startswith(PNG_MAGIC):
        return "expected PNG magic bytes, got {0} ({1} bytes total)".format(
            data[:8].hex(), len(data)
        )
    if len(data) < MIN_PLAUSIBLE_BYTES:
        return "image implausibly small ({0} bytes < {1}).".format(
            len(data), MIN_PLAUSIBLE_BYTES
        )
    return None


def _check_webp_lossless(data: bytes) -> str | None:
    """Return a failure message, or None if `data` is a lossless (VP8L) WebP.

    Lossless is judged by the chunk id at byte 12, not by the container: a
    lossy WebP is also `RIFF....WEBP`, just with a `VP8 ` (with a trailing
    space) or `VP8X` chunk instead of `VP8L`.
    """
    if data[0:4] != b"RIFF" or data[8:12] != b"WEBP":
        return "expected a RIFF/WEBP container, got {0} ({1} bytes total)".format(
            data[:12].hex(), len(data)
        )
    if data[12:16] != b"VP8L":
        return "expected a lossless VP8L chunk, got {0!r} ({1} bytes total)".format(
            data[12:16], len(data)
        )
    if len(data) < MIN_PLAUSIBLE_BYTES:
        return "image implausibly small ({0} bytes < {1}).".format(
            len(data), MIN_PLAUSIBLE_BYTES
        )
    return None


def main() -> int:
    key = os.environ.get("SITESHOT_API_KEY", "")
    if not key.strip():
        print("SKIP: SITESHOT_API_KEY is not set; live smoke test not run.")
        return 0

    client = SiteShot(key)

    png = client.capture("https://example.com/")
    png_failure = _check_png(png)
    if png_failure is not None:
        print("FAIL (png): {0}".format(png_failure), file=sys.stderr)
        return 1
    print("PASS: live capture returned a plausible PNG ({0} bytes).".format(len(png)))

    webp = client.capture("https://example.com/", format="webp")
    webp_failure = _check_webp_lossless(webp)
    if webp_failure is not None:
        print("FAIL (webp): {0}".format(webp_failure), file=sys.stderr)
        return 1
    print("PASS: live capture returned a plausible lossless WebP ({0} bytes).".format(len(webp)))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
