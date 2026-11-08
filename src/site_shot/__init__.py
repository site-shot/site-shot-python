"""Official Python SDK for the Site-Shot screenshot API.

    pip install site-shot

.. code-block:: python

    from site_shot import SiteShot

    client = SiteShot("YOUR_API_KEY")  # or set SITESHOT_API_KEY
    png = client.capture("example.com", full_size=True)

Zero dependencies (standard library only), sync, fully typed.
Docs: https://www.site-shot.com/ — API keys: https://www.site-shot.com/start/
"""

from .client import (
    DEFAULT_BASE_URL,
    CaptureOptions,
    CaptureResult,
    ImageFormat,
    SiteShot,
)
from .errors import (
    APIError,
    AuthError,
    ConcurrencyLimitError,
    CountryUnavailableError,
    InvalidParamsError,
    PageLoadError,
    PartialCaptureWarning,
    QuotaError,
    SiteShotError,
    SiteShotTimeoutError,
    SiteShotWarning,
)

__version__ = "0.3.1"

__all__ = [
    "SiteShot",
    "CaptureOptions",
    "CaptureResult",
    "DEFAULT_BASE_URL",
    "ImageFormat",
    "SiteShotError",
    "AuthError",
    "QuotaError",
    "ConcurrencyLimitError",
    "CountryUnavailableError",
    "InvalidParamsError",
    "SiteShotTimeoutError",
    "APIError",
    "PageLoadError",
    "SiteShotWarning",
    "PartialCaptureWarning",
    "__version__",
]
