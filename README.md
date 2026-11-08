# site-shot

Official Python SDK for the [Site-Shot](https://www.site-shot.com/) screenshot API.

```bash
pip install site-shot
```

Capture website screenshots in a real Chromium browser: full-page capture, country
proxies, automatic ad & cookie-banner removal. **Zero dependencies** (standard
library only), Python >= 3.9, fully typed (`py.typed`), sync.

The **API requires a key** — get one at <https://www.site-shot.com/start/>, which
walks through the key, the install and a first capture in three steps. A
confirmed account's API key comes with the free plan: 100 API captures a month,
no card needed, renewing every month; paid plans start at **$5/mo** (X-Lite,
2,000 screenshots). Accounts confirmed before 2026-10-30 switch the free plan on
with one button on `/start/`; an account that has had a paid plan before does not
get the free plan. Free-plan captures come from the default location or the US;
other countries need a paid plan.
The [free browser tool](https://www.site-shot.com/) is separate and needs no signup.

## Quickstart

```python
from site_shot import SiteShot

client = SiteShot("YOUR_API_KEY")  # or set SITESHOT_API_KEY in the env

png = client.capture("example.com", full_size=True)  # png is bytes
with open("shot.png", "wb") as f:
    f.write(png)
```

The import package is `site_shot`; the pip name is `site-shot` (PyPI treats the
two spellings as the same project).

Building an AI agent with an MCP-capable client instead? Use the sibling MCP
server: `npx -y site-shot-mcp`. Working in Node? `npm install site-shot-sdk`.

## CSV → local report example

For a validated CSV, bounded capture workers, saved PNGs, per-row outcomes and an
escaped local HTML report, see [the runnable CSV report example](https://github.com/site-shot/site-shot-python/blob/main/examples/README.md).
It uses the optional `examples` extra; the SDK itself stays zero-dependency.

## Capture cleanly (ads and cookie banners removed)

```python
png = client.capture(
    "https://example.com/",
    no_ads=True,
    no_cookie_popup=True,
)
```

## Return modes

One capture concept, four return modes — pick the method, not a flag:

```python
# 1. Bytes (primary mode)
png = client.capture("https://example.com/")

# 2. Straight to a file
client.capture_to_file("https://example.com/", "shot.png")

# 3. Base64 string (data URLs, LLM vision payloads)
b64 = client.capture_base64("https://example.com/")

# 4. Full JSON result (image plus metadata; add source_code for rendered HTML)
meta = client.capture_json("https://example.com/", source_code=True)
print(meta["source_code"][:200])  # present whenever requested; the SDK raises otherwise
```

`url` is positional-only, every capture option is a keyword argument. An options
dict works too: `client.capture(url, **options)`.

### `build_url()` — build the request URL without executing it

```python
url = client.build_url("https://example.com/", width=1280)
```

> **⚠️ Key-leak warning:** the returned URL **embeds your API key** (`userkey`),
> and Site-Shot has no signed-URL scheme. Use it for debugging or server-side
> proxying only. **Never** put it in an `<img src>` or anywhere a browser or
> third party can see it.

## Screenshots from another country

Pass `country` as an **ISO 3166-1 alpha-2 code** (e.g. `"DE"`, `"BR"`, `"JP"` —
the current list is at <https://www.site-shot.com/countries/>). It automatically
sets a matching IP, language, time zone, and geolocation. Full country names are
not valid values. Any `country` other than `"US"` needs a paid plan; free-plan
captures come from the default location or the US.

By default, if the requested country has no capacity at that moment, the API
silently falls back to a US vantage point. Set `strict_country` to fail fast
instead:

```python
from site_shot import SiteShot, CountryUnavailableError

client = SiteShot("YOUR_API_KEY", retries=2)  # recommended for geo captures

try:
    shot = client.capture(
        "https://whatismycountry.com/",
        country="DE",          # ISO 3166-1 alpha-2, never "Germany"
        strict_country=True,
        no_ads=True,
        no_cookie_popup=True,
    )
except CountryUnavailableError:
    ...  # No live DE capacity right now — retry later or drop strict_country.
```

A GPS override, independent of the exit IP:

```python
png = client.capture(
    "https://whatismycountry.com/",
    country="JP",              # ISO code
    geolocation="35.6762,139.6503",
    time_zone="Asia/Tokyo",
    language="ja",
)
```

More on geotargeted screenshots:
<https://www.site-shot.com/blog/screenshot-website-from-another-country/>

## Options

Option names mirror the HTTP query parameters **verbatim** (snake_case) — the
[API reference on the homepage](https://www.site-shot.com/#documentation) and
this SDK share one vocabulary. Booleans are accepted and coerced to `1`/`0`.
**Unknown options pass through verbatim**, so future API params work without an
SDK update. A name that looks like a typo of a known option (`full_page`,
`delay`) is still sent, with a `SiteShotWarning` naming the option it most
likely meant, because the API ignores names it does not know.

| Option | Type / range | API default | Notes |
|---|---|---|---|
| `url` | str, **required** (positional-only) | — | bare domains like `example.com` accepted (`https://` assumed) |
| `width` | int 100–8000 | 1024 | viewport width |
| `height` | int 100–20000 | 768 | viewport height |
| `zoom` | int 5–1000 | 100 | percentage zoom |
| `full_size` | bool | False | full-page capture (height capped by `max_height`) |
| `max_height` | int 100–20000 | 20000 | only meaningful with `full_size`; with `format="webp"`, a full-page capture taller than 16,383 px is cut at 16,383 px from the top (libwebp's per-side limit) |
| `scaled_width` | int 50–10000 | — | scale result image to width |
| `format` | `"png"` \| `"jpeg"` \| `"webp"` | png | `"webp"` is lossless, about 35% smaller than PNG on a typical page (it varies by page); `"jpg"` is an alias of `"jpeg"`; case-insensitive; anything else raises `InvalidParamsError` before a request |
| `delay_time` | int ms 0–60000 | 500 | wait before capture (SPAs, animations) |
| `timeout` | int ms 0–120000 | 60000 | server-side render deadline |
| `user_agent` | str | — | custom UA for the rendering browser |
| `request_headers` | dict | — | emitted as repeated `request_header=Name:value` params |
| `http_proxy` / `proxy_username` / `proxy_password` | str | — | bring-your-own-proxy passthrough |
| `proxy_rotation` | bool | — | omit and Site-Shot picks the route; `True` rotates a proxy on every attempt; `False` makes one attempt without rotation (via `country` if set, else direct) |
| `source_code` | bool | False | include rendered HTML (use with `capture_json`) |
| `javascript_code` | str | — | inject JS into the page before capture |
| `no_ads` | bool | False | remove ads |
| `no_cookie_popup` | bool | False | remove cookie-consent banners |
| `webgl` | bool | False | give the page a working (software) WebGL context; detectable by bot protection, so leave it off unless the page needs it |
| `fresh_session` | bool | False | start in a clean browser: no cookies or local storage saved by an earlier capture, and nothing from this one kept |
| `handle_challenges` | bool | — | wait through interstitial challenge pages; passing it at all, with either value, turns off Site-Shot's adaptive retries for hard hosts, so leave it out unless you need it |
| `country` | ISO 3166-1 alpha-2 | — | e.g. `"DE"` — auto-sets language/time zone/geolocation |
| `strict_country` | bool | False | fail fast instead of silent US fallback |
| `geolocation` | `"lat,lng"` | — | GPS override, independent of IP |
| `language` | str | from `country` / en | Accept-Language |
| `time_zone` | IANA name | America/New_York | see <https://www.site-shot.com/time-zones/> |

`userkey` (your API key) is owned by the `SiteShot` constructor — never a
per-call option. `response_type` is owned by the return-mode methods.

A `CaptureOptions` `TypedDict` is exported for callers who build option dicts:

```python
from site_shot import CaptureOptions

options: CaptureOptions = {"width": 1280, "full_size": True, "country": "DE"}
png = client.capture("https://example.com/", **options)
```

`url` is positional-only on every capture method, so an options dict that also
carries a `url` key still type-checks and runs — the positional URL wins, just
like `userkey`. (These snippets are type-checked under `mypy --strict` in the
test suite.)

The SDK sends GET requests; very long `javascript_code` or `user_agent` values
can exceed practical URL length limits (~8 KB).

## Client options

```python
client = SiteShot(
    "YOUR_API_KEY",
    base_url="https://api.site-shot.com/",
    timeout=90,   # client-side deadline in SECONDS; default = server `timeout` + 30s headroom
    retries=0,    # connection-level retries only (see below)
)
```

The API key falls back to the `SITESHOT_API_KEY` environment variable — the same
variable the [Site-Shot MCP server](https://www.npmjs.com/package/site-shot-mcp)
uses:

```python
import os
client = SiteShot()                                 # reads SITESHOT_API_KEY
client = SiteShot(os.environ["SITESHOT_API_KEY"])    # or pass it explicitly
```

Two different things are called "timeout", exactly as in the HTTP API:

- the constructor's `timeout` is the **client-side deadline in seconds**, and it
  bounds the *whole* exchange — connecting, waiting for the status line and
  headers, and downloading the image body (a slow drip in any of those phases
  cannot outlast it);
- the capture option `timeout` is the **server-side render deadline in
  milliseconds** (the verbatim HTTP param name).

The default client deadline (the render `timeout` plus 30 s) covers one render
attempt. When the API retries a slow page on another route the request can run
longer; the SDK then raises `SiteShotTimeoutError`, and the abandoned capture
may still count toward your allowance. For pages you
know are slow, pass a longer client deadline, for example `SiteShot(timeout=180)`.

`SiteShot` also works as a context manager (`with SiteShot(...) as client:`); it
holds no pooled resources, so closing it is a no-op.

## Errors

Every error is a subclass of `SiteShotError` and carries `http_status`, the
raw response `body` and the API's machine-readable `error_code` (for example
`"proxy_navigation_failed"`) where available.

| Error | Raised when |
|---|---|
| `AuthError` | missing or rejected API key (also raised by the constructor on an empty key) |
| `QuotaError` | a paid plan's quota exhausted, the free plan's 100 captures for the month used up (HTTP 403 until it renews), a `country` other than US on the free plan (Site-Shot refuses it and returns an error instead of an image; the refused request still counts toward the month's 100), payment required, or no active subscription on the account |
| `ConcurrencyLimitError` | a subclass of `QuotaError`: too many captures at once on this key (HTTP 429). The free plan runs one capture at a time; the refused request is not counted, so start it again when the running one finishes |
| `CountryUnavailableError` | `strict_country` capture and the requested country has no capacity right now |
| `InvalidParamsError` | a parameter was rejected, by the SDK before sending (unsupported `format`, a URL that is not http(s)) or by the API (out-of-range width, a malformed target address, a private or local host, ...) |
| `SiteShotTimeoutError` | client-side deadline, or the API reported a render timeout |
| `PageLoadError` | a subclass of `APIError`: the target page could not be opened, because its host does not resolve or is unreachable (`error_code` `"proxy_navigation_failed"`) |
| `APIError` | anything else (server errors, unparseable bodies, connection failures) |

```python
from site_shot import SiteShot, SiteShotError, QuotaError

try:
    png = client.capture("https://example.com/")
except QuotaError as e:
    print("out of quota:", e, e.http_status)
except SiteShotError as e:
    print("capture failed:", e)
```

Under the hood the SDK always asks the API for a JSON response and decodes the
image itself, so it can tell a failed render from a screenshot: an error the API
reports, or an error card it delivers as a capture, raises a typed exception
instead of being handed back as your image.

One case is in between: when a render reaches its `timeout`, the API may return
the frame the browser had by then. That image is real but the page may not have
finished loading, so the SDK returns it and emits a `PartialCaptureWarning`. To
treat it as a failure:

```python
import warnings
from site_shot import PartialCaptureWarning

warnings.simplefilter("error", PartialCaptureWarning)
```

## Retries

Default: `0`. Screenshots cost quota, and a timed-out render may still have
consumed one — so the SDK never auto-retries a render that completed with an
error. The `retries` option applies to **connection-level failures only** (DNS,
connection reset, no bytes received), with jittered backoff. `retries=2` is a
sensible setting for `country=` captures.

## Building an agent?

If your agent drives a browser, it can screenshot pages itself — the right tool for pages that must
be signed into or stepped through a flow. For public URLs, delegating the capture to the API is
usually better engineering: the same pipeline on every request, country-specific vantage points
(`country` + `strict_country`), failed renders raised as typed errors, and a fraction of a
cent per capture instead of a browser session plus vision tokens per look. The full comparison:
[AI agent vs. screenshot API — who should capture the page](https://www.site-shot.com/blog/ai-agent-vs-screenshot-api/).

## Requirements

- Python >= 3.9
- Zero dependencies (standard library `urllib` only)

## Links

- Quickstart & API keys: <https://www.site-shot.com/start/>
- Pricing: <https://www.site-shot.com/pricing/>
- For AI agents & assistants: <https://www.site-shot.com/ai-agents/>
- Supported countries: <https://www.site-shot.com/countries/>
- Time zones: <https://www.site-shot.com/time-zones/>
- Node.js SDK: `npm install site-shot-sdk`
- MCP server (agent tooling sibling): `npx -y site-shot-mcp`
- When an agent should delegate the capture: <https://www.site-shot.com/blog/ai-agent-vs-screenshot-api/>

## License

MIT
