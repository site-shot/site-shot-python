# site-shot

Official Python SDK for the [Site-Shot](https://www.site-shot.com/) screenshot API.

```bash
pip install site-shot
```

Capture website screenshots in a real Chromium browser: full-page capture, country
proxies, automatic ad & cookie-banner removal. **Zero dependencies** (standard
library only), Python >= 3.9, fully typed (`py.typed`), sync.

API plans from **$5/mo** (X-Lite, 2,000 screenshots). The
[free browser tool](https://www.site-shot.com/) is no-signup, but the **API
requires a key** — get one at <https://www.site-shot.com/start/>, which walks
through the key, the install and a first capture in three steps.

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
print(meta["source_code"][:200])
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
not valid values.

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
SDK update.

| Option | Type / range | API default | Notes |
|---|---|---|---|
| `url` | str, **required** (positional-only) | — | bare domains like `example.com` accepted (`https://` assumed) |
| `width` | int 100–8000 | 1024 | viewport width |
| `height` | int 100–20000 | 768 | viewport height |
| `zoom` | int 5–1000 | 100 | percentage zoom |
| `full_size` | bool | False | full-page capture (height capped by `max_height`) |
| `max_height` | int 100–20000 | 20000 | only meaningful with `full_size` |
| `scaled_width` | int 50–10000 | — | scale result image to width |
| `format` | `"png"` \| `"jpeg"` | png | |
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

`SiteShot` also works as a context manager (`with SiteShot(...) as client:`); it
holds no pooled resources, so closing it is a no-op.

## Errors

Every error is a subclass of `SiteShotError` and carries `http_status` and the
raw response `body` where available.

| Error | Raised when |
|---|---|
| `AuthError` | missing or rejected API key (also raised by the constructor on an empty key) |
| `QuotaError` | plan quota exhausted, payment required, or no active subscription on the account |
| `CountryUnavailableError` | `strict_country` capture and the requested country has no capacity right now |
| `InvalidParamsError` | the API rejected a parameter (out-of-range width, bad format, ...) |
| `SiteShotTimeoutError` | client-side deadline, or the API reported a render timeout |
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
image itself — so errors surface as typed exceptions instead of an error picture
pretending to be your screenshot.

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
(`country` + `strict_country`), every frame validated before it is returned, and a fraction of a
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
