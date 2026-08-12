"""Official Python SDK for the Site-Shot screenshot API.

    pip install site-shot

.. code-block:: python

    from site_shot import SiteShot

    client = SiteShot("YOUR_API_KEY")  # or set SITESHOT_API_KEY
    png = client.capture("example.com", full_size=True)

Zero dependencies (standard library only), sync, fully typed.
Docs: https://www.site-shot.com/ — API keys: https://www.site-shot.com/pricing/
"""

from .client import (
    DEFAULT_BASE_URL,
    CaptureOptions,
    CaptureResult,
    SiteShot,
)
from .errors import (
    APIError,
    AuthError,
    CountryUnavailableError,
    InvalidParamsError,
    QuotaError,
    SiteShotError,
    SiteShotTimeoutError,
)

__version__ = "0.1.0"

__all__ = [
    "SiteShot",
    "CaptureOptions",
    "CaptureResult",
    "DEFAULT_BASE_URL",
    "SiteShotError",
    "AuthError",
    "QuotaError",
    "CountryUnavailableError",
    "InvalidParamsError",
    "SiteShotTimeoutError",
    "APIError",
    "__version__",
]
