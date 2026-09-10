"""Capture a public-URL CSV once per row; produce a local, review-required report.

Instructions and limits: examples/README.md. No client retries or live test mode.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import ipaddress
import json
import os
import re
import secrets
import sys
import time
import warnings
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

from PIL import Image

from site_shot import (
    APIError,
    AuthError,
    CountryUnavailableError,
    InvalidParamsError,
    QuotaError,
    SiteShot,
    SiteShotError,
    SiteShotTimeoutError,
)

CAPTURE_SETTINGS: dict[str, Any] = {
    "width": 1280,
    "height": 800,
    "full_size": True,
    "max_height": 4000,
    "format": "png",
    "no_ads": True,
    "no_cookie_popup": True,
    "delay_time": 1500,
    "timeout": 60000,
}
CLIENT_TIMEOUT_SECONDS = 90
MAX_ROWS = 1000
MAX_WORKERS = 8
MAX_IMAGE_BYTES = 40 * 1024 * 1024
MAX_IMAGE_PIXELS = 32_000_000
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
RESERVED_IDS = {"con", "prn", "aux", "nul"} | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
}


class InputError(ValueError):
    """An input failed validation before any API requests were submitted."""


class InvalidImage(ValueError):
    """Response bytes are not a complete, bounded PNG image."""


class CaptureClient(Protocol):
    def capture(self, url: str, /, **params: Any) -> bytes: ...


@dataclass(frozen=True)
class Row:
    id: str
    url: str


@dataclass(frozen=True)
class BatchResult:
    directory: Path
    captured: int
    failed: int

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def validate_url(value: str) -> None:
    # This deliberately small public-page example never accepts auth/share tokens.
    if not value or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        raise InputError("URL must not contain whitespace or control characters")
    if "\\" in value:
        raise InputError("URL must not contain backslashes")
    try:
        url = urlsplit(value)
        host = url.hostname
        if url.scheme not in ("http", "https") or not host:
            raise InputError("Use an absolute HTTP(S) URL")
        if url.username is not None or url.password is not None or url.query or url.fragment:
            raise InputError("Use a public URL without credentials, query strings or fragments")
        if url.port not in (None, 80, 443):
            raise InputError("Only standard public web ports are supported")
        # Classify the same ASCII hostname a browser resolves, including Unicode
        # digits and dot separators, rather than accepting their pre-IDNA form.
        host = host.encode("idna").decode("ascii").lower().rstrip(".")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # Browsers accept shortened, octal and hex IPv4 spellings that
            # ipaddress intentionally rejects. Never treat those as DNS names.
            if re.fullmatch(
                r"(?:0[xX][0-9A-Fa-f]+|[0-9]+)(?:\.(?:0[xX][0-9A-Fa-f]+|[0-9]+))*", host
            ):
                raise InputError("Use an unambiguous public hostname or IP address")
            if (
                "." not in host
                or host.endswith((".local", ".localhost", ".internal"))
                or len(host) > 253
                or any(
                    not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                    for label in host.split(".")
                )
            ):
                raise InputError("Use a public hostname, not a local/private address")
        else:
            if not address.is_global:
                raise InputError("Use a public hostname, not a local/private address")
    except (UnicodeError, ValueError) as exc:
        if isinstance(exc, InputError):
            raise
        raise InputError("Malformed HTTP(S) URL") from None


def validate_rows(rows: Sequence[Row]) -> None:
    if not rows or len(rows) > MAX_ROWS:
        raise InputError(f"Supply between 1 and {MAX_ROWS} rows")
    seen: set[str] = set()
    for number, row in enumerate(rows, 2):
        if (
            not ID_PATTERN.fullmatch(row.id)
            or row.id.casefold() in seen
            or row.id.casefold() in RESERVED_IDS
        ):
            raise InputError(f"CSV row {number} needs a unique, filename-safe ID")
        try:
            validate_url(row.url)
        except InputError as exc:
            raise InputError(f"CSV row {number}: {exc}") from None
        seen.add(row.id.casefold())


def read_rows(path: Path) -> list[Row]:
    rows: list[Row] = []
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream, strict=True)
            if next(reader, None) != ["id", "url"]:
                raise InputError("CSV must start with exactly the header id,url")
            for number, values in enumerate(reader, 2):
                if len(values) != 2:
                    raise InputError(f"CSV row {number} must have exactly two fields")
                rows.append(Row(*values))
                if len(rows) > MAX_ROWS:
                    raise InputError(f"This example accepts at most {MAX_ROWS} rows")
    except (OSError, UnicodeError, csv.Error):
        raise InputError("CSV could not be read as valid UTF-8 CSV") from None
    validate_rows(rows)
    return rows


def validate_png(data: bytes) -> tuple[int, int]:
    if not isinstance(data, bytes) or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise InvalidImage("Response is not PNG")
    if len(data) > MAX_IMAGE_BYTES:
        raise InvalidImage("Image exceeds this example's size limit")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                if image.format != "PNG" or getattr(image, "n_frames", 1) != 1:
                    raise InvalidImage("Expected one PNG frame")
                width, height = image.size
                if width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
                    raise InvalidImage("Image exceeds this example's pixel limit")
                image.verify()
            # verify checks the container; load also requires complete pixel data.
            with Image.open(BytesIO(data)) as image:
                image.load()
        return width, height
    except InvalidImage:
        raise
    except Exception:  # noqa: BLE001 -- decoder failures must not expose untrusted response data
        raise InvalidImage("PNG could not be completely decoded") from None


ERROR_CODES = (
    (AuthError, "authentication_failed"),
    (QuotaError, "quota_or_payment_required"),
    (CountryUnavailableError, "country_unavailable"),
    (InvalidParamsError, "invalid_capture_parameters"),
    (SiteShotTimeoutError, "capture_timeout"),
    (APIError, "api_error"),
    (SiteShotError, "capture_error"),
    (InvalidImage, "invalid_png"),
    (OSError, "local_file_error"),
)


def capture_row(row: Row, directory: Path, factory: Callable[[], CaptureClient]) -> dict[str, Any]:
    start = time.monotonic()
    result: dict[str, Any] = {"id": row.id, "url": row.url, "started_at": utc_now()}
    try:
        data = factory().capture(row.url, **CAPTURE_SETTINGS)
        width, height = validate_png(data)
        filename = f"images/{row.id}.png"
        # A failed local write can leave only a .tmp, never a report-linked PNG.
        temporary = directory / f"images/{row.id}.png.tmp"
        with temporary.open("xb") as stream:
            stream.write(data)
        temporary.replace(directory / filename)
        result.update(
            status="captured",
            image=filename,
            width=width,
            height=height,
            sha256=hashlib.sha256(data).hexdigest(),
            visual_review="required",
        )
    except Exception as exc:  # noqa: BLE001 -- per-row failure accounting with secret-safe codes
        code = next((code for cls, code in ERROR_CODES if isinstance(exc, cls)), "unexpected_error")
        result.update(status="failed", error=code)
        # Never serialize exception text/body/cause: upstream errors may echo secrets.
        if isinstance(exc, SiteShotError) and isinstance(exc.http_status, int):
            result["http_status"] = exc.http_status
    result.update(finished_at=utc_now(), duration_ms=round((time.monotonic() - start) * 1000))
    return result


def write_html(directory: Path, manifest: dict[str, Any]) -> None:
    cards = []
    for row in manifest["rows"]:
        label, url = escape(row["id"]), escape(row["url"], quote=True)
        if row["status"] == "captured":
            filename = escape(row["image"], quote=True)
            content = (
                f"<p>Captured; visual review required ({row['width']} × {row['height']}).</p>"
                f'<a href="{filename}">Open full-size PNG<img loading="lazy" src="{filename}" '
                f'alt="Screenshot for {label}"></a>'
            )
        else:
            content = f'<p class="error">Failed: {escape(row["error"])}</p>'
        cards.append(
            f'<article><h2>{label}</h2><a href="{url}" target="_blank" '
            f'rel="noopener noreferrer">{url}</a>{content}</article>'
        )
    title = "Incomplete capture batch" if manifest["failed"] else "Capture batch: review required"
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src 'self' file:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<meta name="referrer" content="no-referrer"><title>{title}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;color:#172033;background:#f4f6f9}}article{{background:white;border:1px solid #ccd3df;border-radius:8px;padding:1rem;margin:1rem 0;overflow-wrap:anywhere}}img{{display:block;max-width:100%;height:auto;max-height:720px;object-fit:contain;object-position:top;margin-top:1rem}}a{{color:#084faa}}.error{{color:#a01919;font-weight:600}}pre{{white-space:pre-wrap}}</style></head>
<body><h1>{title}</h1><p>{manifest["captured"]} captured; {manifest["failed"]} failed.</p>
<p>Image decoding is not a content check. Review every image for the intended page,
readable content, clipping, blanks and challenge/error pages before sharing this report.</p>
<p>Run started: {escape(manifest["started_at"])}. Times are UTC. <a href="manifest.json">Manifest</a>.</p>
<details><summary>Settings used for every row</summary><pre>{escape(json.dumps(manifest["capture_settings"], indent=2))}</pre></details>
{"".join(cards)}</body></html>"""
    (directory / "index.html").write_text(html, encoding="utf-8")


def run_batch(
    rows: Sequence[Row], output: Path, factory: Callable[[], CaptureClient], workers: int = 1
) -> BatchResult:
    if not 1 <= workers <= MAX_WORKERS:
        raise InputError(f"workers must be between 1 and {MAX_WORKERS}")
    validate_rows(rows)
    output.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    directory = output / f"run-{stamp}-{secrets.token_hex(4)}"
    directory.mkdir()
    (directory / "images").mkdir()
    started = utc_now()
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        results = list(executor.map(lambda row: capture_row(row, directory, factory), rows))
    finally:
        # map can be interrupted while still submitting, before its iterator's
        # own cancellation cleanup exists. Never start queued billable work then.
        executor.shutdown(wait=True, cancel_futures=True)
    failed = sum(row["status"] == "failed" for row in results)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "started_at": started,
        "finished_at": utc_now(),
        "status": "incomplete" if failed else "captures_completed_review_required",
        "capture_settings": dict(CAPTURE_SETTINGS),
        "client_timeout_seconds": CLIENT_TIMEOUT_SECONDS,
        "client_retries": 0,
        "workers": workers,
        "captured": len(results) - failed,
        "failed": failed,
        "rows": results,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    write_html(directory, manifest)
    return BatchResult(directory, len(results) - failed, failed)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Capture a public-URL CSV into a local HTML report."
    )
    parser.add_argument("csv", type=Path, help="UTF-8 CSV with exactly id,url columns")
    parser.add_argument(
        "--output", type=Path, default=Path("reports"), help="Parent of a unique run folder"
    )
    parser.add_argument(
        "--workers",
        type=int,
        choices=range(1, MAX_WORKERS + 1),
        default=1,
        help="Default 1; increase only after confirming your plan's concurrency allowance",
    )
    args = parser.parse_args(argv)
    key = os.environ.get("SITESHOT_API_KEY", "").strip()
    if not key:
        print("Set SITESHOT_API_KEY in your environment; no requests were sent.", file=sys.stderr)
        return 2
    try:
        rows = read_rows(args.csv)
        if key in str(args.output) or any(key in row.url or key in row.id for row in rows):
            raise InputError("The API key must not occur in input data or the output path")
        result = run_batch(
            rows,
            args.output,
            lambda: SiteShot(key, timeout=CLIENT_TIMEOUT_SECONDS, retries=0),
            args.workers,
        )
    except InputError as exc:
        print(f"Input error: {exc}. No requests were sent.", file=sys.stderr)
        return 2
    except OSError:
        print("Local report storage failed; this run is not complete.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(
            "Interrupted; queued captures cancelled. In-flight captures may have consumed quota. "
            "This run is not a completed report.",
            file=sys.stderr,
        )
        return 130
    print(
        f"Captured: {result.captured}; failed: {result.failed}. Report: {result.directory / 'index.html'}"
    )
    print("Review every captured image before treating it as a usable result.")
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
