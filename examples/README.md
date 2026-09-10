# CSV screenshots → local HTML report

A runnable, public-page example using the official Python SDK. Each CSV row makes
**one client capture call**. The run saves PNG files, a per-row JSON manifest and
an HTML index for human review. It does not retry failed rows, reuse an old image,
reduce quality on failure, publish files or export PDF. The service's own rendering
and routing policy still applies inside each API request.

**API prerequisite:** an account, confirmed email, active paid API plan and API
key. See [Quickstart](https://www.site-shot.com/start/) and
[pricing](https://www.site-shot.com/pricing/). The separate free browser tool is
not a free API trial. Captures consume your plan's allowance; a timeout can happen
after server work has started. Confirm the budget before running a CSV.

## Install and run

From the cloned SDK repository, with Python 3.9 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[examples]'
```

The optional `examples` extra adds Pillow for full PNG decoding/validation. The
SDK itself remains standard-library-only. Keep the example and SDK checkout at
the same revision when following this guide.

Set `SITESHOT_API_KEY` through your secret manager or a masked terminal prompt.
Leading and trailing whitespace is removed, matching the SDK; an empty key is
rejected before creating a report or submitting captures.
For Bash, this avoids putting the key in shell history or the process arguments:

```bash
read -r -s -p 'Site-Shot API key: ' SITESHOT_API_KEY
printf '\n'
export SITESHOT_API_KEY
python examples/csv_report.py examples/report_urls.csv --output reports
unset SITESHOT_API_KEY
```

The supplied CSV contains **five real public Site-Shot pages**, not customer data.
It is a proposed demo input, not evidence of a completed live capture. Inspect or
replace it before spending API quota. No live run is performed by installation,
tests, imports or `--help`.

### CSV contract

```csv
id,url
home,https://www.site-shot.com/
pricing,https://www.site-shot.com/pricing/
```

- UTF-8 (optional BOM), exactly `id,url` as the header, one to 1,000 data rows.
- Stable IDs: 1–64 ASCII letters, digits, `_` or `-`; first character must be a
  letter or digit. IDs must be unique ignoring case, so files remain safe on
  case-insensitive filesystems. Reserved device names such as `CON` are rejected.
  IDs, not URL-derived names, name the PNGs.
- Absolute public HTTP(S) URLs on standard web ports. This intentionally narrow
  example rejects credentials, query strings, fragments, whitespace, local
  hostnames and non-public IP literals. It does not resolve DNS during validation;
  hostname syntax checks are not a proof that a hostname is publicly reachable.
  Ambiguous shortened/octal/hex numeric host spellings are rejected as well.
- Use only pages you are entitled to capture. Private/authenticated pages and
  token-bearing URLs are out of scope. No auth cookies, custom headers or injected
  JavaScript are accepted by this CLI.
- The entire CSV is validated **before** any capture. Invalid input exits `2`;
  nothing is silently skipped or trimmed.

### Fixed capture budget

Every row uses the same settings, recorded once in `manifest.json`:

| Setting | Value |
|---|---|
| Viewport | 1280 × 800 |
| Full page / maximum height | `full_size=True`, `max_height=4000` |
| Format | PNG |
| Cleanup | `no_ads=True`, `no_cookie_popup=True` |
| Wait before capture | `delay_time=1500` milliseconds |
| Server render deadline | `timeout=60000` milliseconds |
| Whole client exchange deadline | 90 seconds |
| Client retries | `0` |

These are **example settings, not optimal values for every site**. Pages taller
than 4,000 pixels can be clipped. The client deadline includes response download;
this example has no separate total-batch deadline. A maximum-size input can take
a long time and spend significant quota. Cleanup is not guaranteed on every site.

Default concurrency is **one worker**. Increase `--workers` only after checking
your account's concurrent-request allowance and budget; the CLI accepts 1–8,
which is an example safety bound, **not a plan entitlement**. For example,
`--workers 2` is appropriate only if your plan permits it. Each worker constructs
its own SDK client. The script never discovers a higher allowance or increases
concurrency automatically.

## Read the result

A run creates a new UTC-stamped, randomly suffixed folder, for example:

```text
reports/run-<UTC timestamp>-<random>/
  index.html
  manifest.json
  images/
    home.png
    pricing.png
```

Open that run's `index.html` locally in your browser. It has no scripts, remote
fonts, tracking or external image loads. The source-page links are explicit
outbound links. Do not publish the directory automatically: the manifest and
report contain the supplied URLs and the screenshots themselves.

The manifest records each row's ID, source URL, UTC start/end, duration, status,
image path/dimensions/hash or a safe error code. Settings, client deadline,
retries and worker count are recorded once for the whole run. CSV order is
preserved even when workers finish out of order. A second run cannot overwrite
the first.

PNG bytes must pass signature, container integrity and full pixel decoding before
an image gets its final filename. This example rejects images over 40 MiB,
32 million pixels, or multiple frames. JSON, HTML, JPEG or corrupt/truncated
responses are errors, not `.png` screenshots. A failed local image write may
leave a `.tmp` file, never a report-linked PNG. A report-storage failure or an
interrupted run leaves an incomplete directory; do not treat its existence as a
completed report.

**Decoding is not a content-quality check.** Before sharing, open every image and
confirm the intended page, readable content, geometry and acceptable clipping;
reject blank, challenge, login or target-error pages. Even an exit code of `0`
means only that image capture/decoding and local report creation completed;
visual review is still required. This is not visual regression testing, a
functional test, semantic comparison, an accessibility audit or legal evidence.

### Exit statuses and failures

- `0`: every row produced a decoded PNG; human review still required.
- `1`: at least one row failed; the HTML explicitly says **Incomplete capture
  batch** and keeps failed rows visible. Other rows are still attempted once.
- `2`: invalid input/missing key or a local report-storage problem. The CLI states
  whether requests were not sent or the run could not be completed.
- `130`: interrupted with Ctrl+C. Not-yet-started captures are cancelled, including
  if interruption happens while the batch is being submitted. Already in-flight
  requests are allowed to finish and can still consume quota. The script waits
  for them (up to their remaining client deadlines) before exiting; that partial
  directory is not a completed report.

Errors expose only a fixed code (such as `capture_timeout`,
`quota_or_payment_required`, `authentication_failed`, `invalid_png` or
`local_file_error`) and, when available, an HTTP status. Raw exception messages,
bodies, URLs built by the SDK and API keys are not logged or copied into the
report. Fix the reported issue deliberately before deciding whether to start a
**new** billable run; there is no automatic retry/resume path.

## Test without API calls

```bash
python -m unittest discover -s tests -v
```

The example tests use generated local images and fake clients. They cover CSV and
URL validation, secret-safe failures, incomplete reports, decoding, unique run
folders, escaped HTML and bounded concurrency. They do not prove live API
parameters, target-page quality or paid-plan concurrency. Those remain separate,
explicitly approved demo checks before publishing a tutorial or recording video.
