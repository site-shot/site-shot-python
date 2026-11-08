# Changelog

All notable changes to the `site-shot` package on PyPI.

## Unreleased

### Added

- `PageLoadError` (a subclass of `APIError`): the target page could not be
  opened because its host does not resolve or is unreachable
  (`error_code` `"proxy_navigation_failed"`). Before, this was reported as a
  render timeout.
- `ConcurrencyLimitError` (a subclass of `QuotaError`): too many captures at
  once on the key (HTTP 429). The free plan runs one capture at a time.
- `error_code` on every `SiteShotError`: the API's machine-readable code, when
  the response carried one.
- `PartialCaptureWarning`: the render reached its timeout and the API returned
  the frame it had by then.
- `SiteShotWarning` for an unknown option name that looks like a typo of a
  known one (`full_page`, `delay`); the option is still sent.
- `webgl`, `fresh_session` and `handle_challenges` in `CaptureOptions` and the
  README.

### Changed

- The renderer's error card the API delivers as a successful capture (render
  status 5xx, `X-WS-Error-Code: screenshot_capture_timeout`, and no
  frame-kept header) raises `SiteShotTimeoutError` instead of being returned
  as the screenshot. The rule is the API's own.
- A requested `source_code` missing from the result raises
  `SiteShotTimeoutError` instead of leaving the key out.
- URLs with a scheme other than http(s) (`ftp:`, `file:`, `javascript:`, ...)
  raise `InvalidParamsError` before a request is sent.
- "Malformed target address" and the private-address refusal
  ("Forbidden target address") raise `InvalidParamsError`, not
  `APIError`/`QuotaError`.
- A gateway HTML error page (HTTP 502/504) gives a short message; the page
  stays in `error.body`.
- Key errors point at <https://www.site-shot.com/start/>, where the key is
  issued; "No active subscription found" says where to switch on the free plan
  or choose a plan.
- Client-side timeout errors say the abandoned capture may still count.

## 0.3.1 (2026-10-31)

- Docs: the API key comes with the free plan (100 captures a month).

## 0.3.0 (2026-09-24)

- `format="webp"` (lossless WebP). Unsupported formats raise
  `InvalidParamsError` before a request is sent.
- CSV screenshot report example (`examples/csv_report.py`).

## 0.2.1 (2026-08-16)

- Docs: when an agent should delegate the capture to the API.

## 0.2.0 (2026-08-13)

- Read both of the API's error keys (`error` and `message`); HTTP 403 is a
  `QuotaError`, not an `AuthError`.
- An upstream "403 Forbidden" capture failure is no longer blamed on the key.
- Corrected documented API defaults and the three states of `proxy_rotation`.

## 0.1.0 (2026-08-12)

- First release: `capture`, `capture_to_file`, `capture_base64`,
  `capture_json` and `build_url`, typed errors, and a client deadline that
  covers the whole exchange.
