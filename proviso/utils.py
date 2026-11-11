#
#
#

import sqlite3
from logging import getLogger
from pathlib import Path

import httpx
from hishel import SqliteStorage
from hishel.httpx import CacheTransport
from packaging.version import Version
from unearth.fetchers import DEFAULT_SECURE_ORIGINS

log = getLogger(__name__)

# The columns of a hishel 1.x cache's `entries` table. hishel 2.x refuses to
# open such a database.
HISHEL_V1_ENTRIES_COLUMNS = {
    'id',
    'cache_key',
    'data',
    'created_at',
    'deleted_at',
}


def _resolve_cache_db_path(cache_db_path):
    """Return the file hishel's SqliteStorage uses for database_path.

    hishel puts a bare filename under ``.cache/hishel/`` in the current
    directory and uses any other path as given.
    """
    path = Path(cache_db_path)
    if path.parent == Path('.'):
        return Path('.cache/hishel') / path.name
    return path


def _drop_hishel_v1_cache(cache_db_path):
    """Drop hishel 1.x cache tables so that hishel 2.x can use the database."""
    path = _resolve_cache_db_path(cache_db_path)
    if not path.is_file():
        return
    connection = sqlite3.connect(str(path))
    try:
        columns = {
            row[1] for row in connection.execute('PRAGMA table_info(entries)')
        }
        if columns != HISHEL_V1_ENTRIES_COLUMNS:
            return
        log.warning(
            'clearing incompatible hishel 1.x HTTP cache in %s', str(path)
        )
        with connection:
            connection.execute('DROP TABLE IF EXISTS streams')
            connection.execute('DROP TABLE IF EXISTS entries')
    finally:
        connection.close()


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
        if cache_db_path:
            _drop_hishel_v1_cache(cache_db_path)
            storage = SqliteStorage(database_path=cache_db_path)
        else:
            storage = SqliteStorage(
                connection=sqlite3.connect(
                    ':memory:', check_same_thread=False, isolation_level=None
                )
            )

        # Follow HTTP caching headers (RFC 9111). Without rfc=True, hishel
        # caches every response until the storage TTL expires, and there's
        # no TTL by default, so new PyPI releases would never show up.
        transport = CacheTransport(
            next_transport=httpx.HTTPTransport(), storage=storage, rfc=True
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
