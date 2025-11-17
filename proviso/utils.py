#
#
#

import httpx
from hishel import SyncSqliteStorage
from hishel.httpx import SyncCacheTransport
from packaging.version import Version
from unearth.fetchers import DEFAULT_SECURE_ORIGINS


class CachingClient(httpx.Client):
    """httpx.Client wrapper that implements the Fetcher protocol for unearth with caching."""

    def __init__(self, *args, cache_db_path=None, **kwargs):
        """Initialize CachingClient with optional persistent caching.

        Args:
            cache_db_path: Optional path to SQLite database for persistent caching.
                          If None, uses in-memory caching (default).
            *args: Passed to httpx.Client
            **kwargs: Passed to httpx.Client
        """
        # Create storage based on whether persistent caching is requested
        if cache_db_path:
            storage = SyncSqliteStorage(database_path=cache_db_path)
        else:
            storage = None  # hishel will use in-memory storage

        # Create cache transport wrapping the default HTTP transport
        transport = SyncCacheTransport(
            next_transport=httpx.HTTPTransport(), storage=storage
        )
        # Initialize parent with cache transport
        super().__init__(*args, transport=transport, **kwargs)

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
