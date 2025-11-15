#
#
#

from logging import getLogger

import httpx
from packaging.version import Version
from unearth.fetchers import DEFAULT_SECURE_ORIGINS

log = getLogger('proviso.utils')


class CachingClient(httpx.Client):
    """httpx.Client wrapper that implements the Fetcher protocol for unearth with caching."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._cache = {}

    def get(self, url, **kwargs):
        """Override get() to add simple caching."""

        # Create cache key from URL, lots of assumptions here, e.g. headers
        # don't matter that should hold for our use cases
        cache_key = url

        # Check memory cache first
        if cache_key in self._cache:
            log.debug(f'Cache hit: {url}')
            return self._cache[cache_key]

        # Not in cache, fetch from network
        log.debug(f'Cache miss, fetching: {url}')
        response = super().get(url, **kwargs)

        self._cache[cache_key] = response

        return response

    def get_stream(self, url, *, headers=None):
        """Required by Fetcher protocol."""
        return self.stream('GET', url, headers=headers)

    def iter_secure_origins(self):
        """Required by Fetcher protocol."""
        yield from DEFAULT_SECURE_ORIGINS


def format_python_version_for_markers(version_str):
    """Format a Python version string for use in marker evaluation.

    Args:
        version_str: Version string like "3.9", "3.10.5", "3.11.0"

    Returns:
        Dict with 'python_version' and 'python_full_version' keys
    """
    v = Version(version_str)

    # python_version is major.minor (e.g., "3.9")
    python_version = f"{v.major}.{v.minor}"

    # python_full_version includes patch
    python_full_version = f"{v.major}.{v.minor}.{v.micro}"

    return {
        'python_version': python_version,
        'python_full_version': python_full_version,
    }
