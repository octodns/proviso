#
#
#

import sqlite3
import sys
from logging import getLogger
from os import getenv
from pathlib import Path

import httpx2
from hishel import SqliteStorage
from hishel.httpx2 import CacheTransport
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

CACHE_DB_FILENAME = 'http-cache.db'

# A persistent cache's entries are removed after this many seconds without
# being used, so entries for releases no longer resolved don't pile up until
# hishel's 1 GB size limit. It's an idle time (hishel's TTI), not a TTL
# counted from when the entry was stored, so entries every run uses stay
# cached. With rfc=True it only decides how long hishel keeps an entry, never
# whether one is fresh: an expired entry is a cache miss, and a kept one is
# still revalidated once its max-age has passed.
PERSISTENT_CACHE_TTI = 30 * 24 * 60 * 60


def default_cache_db_path():
    """Return the HTTP cache database in the per-user cache directory.

    That's ``$XDG_CACHE_HOME/proviso`` when XDG_CACHE_HOME is an absolute
    path, otherwise the platform's per-user cache directory.
    """
    xdg_cache_home = getenv('XDG_CACHE_HOME')
    # the XDG spec says to ignore a relative path
    if xdg_cache_home and Path(xdg_cache_home).is_absolute():
        directory = Path(xdg_cache_home) / 'proviso'
    elif sys.platform == 'darwin':
        directory = Path.home() / 'Library' / 'Caches' / 'proviso'
    elif sys.platform == 'win32':
        local_app_data = getenv('LOCALAPPDATA')
        if local_app_data:
            base = Path(local_app_data)
        else:
            base = Path.home() / 'AppData' / 'Local'
        directory = base / 'proviso' / 'Cache'
    else:
        directory = Path.home() / '.cache' / 'proviso'
    return str(directory / CACHE_DB_FILENAME)


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


def _open_cache_db(cache_db_path):
    """Create the cache database if needed and check that it can be used.

    Raises OSError or sqlite3.Error if it can't, which hishel would otherwise
    log for every request.
    """
    path = _resolve_cache_db_path(cache_db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path))
    try:
        # sqlite opens the file on first use, so this creates it, or fails
        # if it can't be created or isn't a database
        connection.execute('PRAGMA schema_version')
    finally:
        connection.close()
    _drop_hishel_v1_cache(cache_db_path)


class CachingClient(httpx2.Client):
    """httpx2.Client wrapper that implements the Fetcher protocol for unearth with caching."""

    def __init__(
        self, *args, cache_db_path=None, fall_back_to_memory=False, **kwargs
    ):
        """Initialize CachingClient with optional persistent caching.

        Args:
            cache_db_path: Optional path to SQLite database for persistent caching.
                          If None, uses in-memory caching (default).
            fall_back_to_memory: If cache_db_path can't be used, log a warning
                          and use in-memory caching instead of raising
                          OSError or sqlite3.Error.
            *args: Passed to httpx2.Client
            **kwargs: Passed to httpx2.Client
        """
        storage = None
        if cache_db_path:
            try:
                _open_cache_db(cache_db_path)
            except (OSError, sqlite3.Error) as e:
                if not fall_back_to_memory:
                    raise
                log.warning(
                    'unable to use the HTTP cache %s, caching in memory '
                    'instead: %s',
                    cache_db_path,
                    e,
                )
            else:
                log.debug('HTTP cache: %s', cache_db_path)
                storage = SqliteStorage(
                    database_path=cache_db_path,
                    default_tti=PERSISTENT_CACHE_TTI,
                )
        if storage is None:
            storage = SqliteStorage(
                connection=sqlite3.connect(
                    ':memory:', check_same_thread=False, isolation_level=None
                )
            )

        # Follow HTTP caching headers (RFC 9111). Without rfc=True, hishel
        # serves every response until the storage expires it, which for an
        # entry used every run is never, so new PyPI releases would never
        # show up.
        transport = CacheTransport(
            next_transport=httpx2.HTTPTransport(), storage=storage, rfc=True
        )
        # Initialize parent with cache transport
        super().__init__(*args, transport=transport, **kwargs)

    def get_stream(self, url, *, headers=None):
        """Required by Fetcher protocol."""
        return self.stream('GET', url, headers=headers)

    def iter_secure_origins(self):
        """Required by Fetcher protocol."""
        yield from DEFAULT_SECURE_ORIGINS
