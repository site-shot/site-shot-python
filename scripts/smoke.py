#!/usr/bin/env python3
"""Live smoke test against the real Site-Shot API.

Gated on SITESHOT_API_KEY: without the env var it prints SKIP and exits 0, so it
is safe anywhere but only meaningful with a real key. Each run spends render
quota, so it performs a single capture. Not wired into CI on purpose — run it
manually as part of the publish checklist:

    SITESHOT_API_KEY=... python scripts/smoke.py
"""

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


def main() -> int:
    key = os.environ.get("SITESHOT_API_KEY", "")
    if not key.strip():
        print("SKIP: SITESHOT_API_KEY is not set; live smoke test not run.")
        return 0

    client = SiteShot(key)
    data = client.capture("https://example.com/")

    if not data.startswith(PNG_MAGIC):
        print(
            "FAIL: expected PNG magic bytes, got {0} ({1} bytes total)".format(
                data[:8].hex(), len(data)
            ),
            file=sys.stderr,
        )
        return 1
    if len(data) < MIN_PLAUSIBLE_BYTES:
        print(
            "FAIL: image implausibly small ({0} bytes < {1}).".format(
                len(data), MIN_PLAUSIBLE_BYTES
            ),
            file=sys.stderr,
        )
        return 1

    print("PASS: live capture returned a plausible PNG ({0} bytes).".format(len(data)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
