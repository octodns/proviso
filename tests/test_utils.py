import sqlite3
from email.utils import formatdate
from os import chdir, getcwd
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import httpx2
from unearth.fetchers import DEFAULT_SECURE_ORIGINS

from proviso.utils import (
    PERSISTENT_CACHE_TTI,
    CachingClient,
    _drop_hishel_v1_cache,
    _open_cache_db,
    _resolve_cache_db_path,
    default_cache_db_path,
)

URL = 'https://pypi.org/pypi/example/json'


class RecordingTransport(httpx2.MockTransport):
    """MockTransport that counts the requests that reach it."""

    def __init__(self, headers):
        self.calls = 0

        def handler(request):
            self.calls += 1
            return httpx2.Response(200, headers=headers, json={'n': self.calls})

        super().__init__(handler)


def mock_upstream(client, headers=None):
    """Replace the client's network transport, keeping hishel in front of it."""
    if headers is None:
        headers = {'Cache-Control': 'max-age=900, public'}
    upstream = RecordingTransport(headers)
    client._transport.next_transport = upstream
    return upstream


class Clock:
    """A settable time.time() for hishel's freshness and expiry checks."""

    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


class RevalidatingTransport(httpx2.MockTransport):
    """MockTransport for a PyPI index page: max-age=600 and an ETag.

    Answers a matching If-None-Match with a 304 and records the headers of
    the requests that reach it. hishel only adds its revalidation headers
    while sending, so they're copied.
    """

    def __init__(self, clock):
        self.requests = []

        def handler(request):
            self.requests.append(dict(request.headers))
            headers = {
                'Cache-Control': 'max-age=600, public',
                'Date': formatdate(clock(), usegmt=True),
                'ETag': '"v1"',
            }
            if request.headers.get('If-None-Match') == '"v1"':
                return httpx2.Response(304, headers=headers)
            return httpx2.Response(200, headers=headers, json={'v': 1})

        super().__init__(handler)


def create_hishel_v1_cache(path):
    """Create a database with the hishel 1.x cache schema."""
    connection = sqlite3.connect(str(path))
    with connection:
        connection.execute(
            'CREATE TABLE entries (id BLOB PRIMARY KEY, cache_key BLOB, '
            'data BLOB, created_at REAL, deleted_at REAL)'
        )
        connection.execute(
            'CREATE TABLE streams (entry_id BLOB, chunk_number INTEGER, '
            'chunk_data BLOB)'
        )
    connection.close()


def table_columns(path, table):
    connection = sqlite3.connect(str(path))
    try:
        return {
            row[1] for row in connection.execute(f'PRAGMA table_info({table})')
        }
    finally:
        connection.close()


class TestCachingClient(TestCase):
    def test_client_instantiation(self):
        """Test that CachingClient can be instantiated."""
        with CachingClient() as client:
            self.assertIsNotNone(client)

    def test_client_instantiation_with_persistent_cache(self):
        """Test that CachingClient can be instantiated with persistent cache."""
        from tempfile import NamedTemporaryFile

        with NamedTemporaryFile(suffix='.db', delete=True) as tmpfile:
            with CachingClient(cache_db_path=tmpfile.name) as client:
                self.assertIsNotNone(client)

    def test_get_stream(self):
        """Test get_stream method required by Fetcher protocol."""
        url = 'https://example.com/test'
        headers = {'User-Agent': 'test'}

        with CachingClient() as client:
            with patch.object(client, 'stream') as mock_stream:
                client.get_stream(url, headers=headers)
                mock_stream.assert_called_once_with('GET', url, headers=headers)

    def test_iter_secure_origins(self):
        """Test iter_secure_origins method required by Fetcher protocol."""
        with CachingClient() as client:
            origins = list(client.iter_secure_origins())

        # Should return DEFAULT_SECURE_ORIGINS
        self.assertEqual(list(DEFAULT_SECURE_ORIGINS), origins)
        self.assertGreater(len(origins), 0)

    def test_in_memory_cache_hit(self):
        """Responses with max-age are served from the in-memory cache."""
        with CachingClient() as client:
            upstream = mock_upstream(client)
            first = client.get(URL)
            second = client.get(URL)

        self.assertEqual(1, upstream.calls)
        self.assertFalse(first.extensions['caching']['from_cache'])
        self.assertTrue(second.extensions['caching']['from_cache'])
        self.assertEqual({'n': 1}, second.json())

    def test_in_memory_cache_writes_no_files(self):
        """The default in-memory cache doesn't create files."""
        cwd = getcwd()
        with TemporaryDirectory() as tmpdir:
            chdir(tmpdir)
            try:
                with CachingClient() as client:
                    mock_upstream(client)
                    client.get(URL)
                self.assertEqual([], list(Path(tmpdir).iterdir()))
            finally:
                chdir(cwd)

    def test_respects_cache_headers(self):
        """Responses that forbid caching are fetched every time."""
        with CachingClient() as client:
            upstream = mock_upstream(client, {'Cache-Control': 'no-store'})
            client.get(URL)
            second = client.get(URL)

        self.assertEqual(2, upstream.calls)
        self.assertFalse(second.extensions['caching']['from_cache'])

    def test_persistent_cache(self):
        """A persistent cache is shared across clients."""
        with TemporaryDirectory() as tmpdir:
            db = f'{tmpdir}/cache.db'
            with CachingClient(cache_db_path=db) as client:
                mock_upstream(client)
                client.get(URL)
            with CachingClient(cache_db_path=db) as client:
                upstream = mock_upstream(client)
                response = client.get(URL)

            self.assertEqual(0, upstream.calls)
            self.assertTrue(response.extensions['caching']['from_cache'])

    def test_persistent_cache_from_hishel_v1(self):
        """A hishel 1.x cache is cleared and reused."""
        with TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / 'cache.db'
            create_hishel_v1_cache(db)

            with self.assertLogs('proviso.utils', level='WARNING') as logs:
                client = CachingClient(cache_db_path=str(db))
            self.assertIn('hishel 1.x', logs.output[0])

            with client:
                upstream = mock_upstream(client)
                client.get(URL)
                response = client.get(URL)

            self.assertEqual(1, upstream.calls)
            self.assertTrue(response.extensions['caching']['from_cache'])
            self.assertNotIn('created_at', table_columns(db, 'entries'))

    def test_persistent_cache_revalidates_after_max_age(self):
        """A persistent cache revalidates a stale entry, never serves it."""
        clock = Clock()
        with TemporaryDirectory() as tmpdir, patch('time.time', clock):
            with CachingClient(cache_db_path=f'{tmpdir}/cache.db') as client:
                upstream = RevalidatingTransport(clock)
                client._transport.next_transport = upstream
                client.get(URL)

                # fresh, served from the cache
                clock.now += 599
                response = client.get(URL)
                self.assertTrue(response.extensions['caching']['from_cache'])
                self.assertEqual(1, len(upstream.requests))

                # stale, revalidated with the ETag
                clock.now += 2
                response = client.get(URL)
                self.assertEqual(2, len(upstream.requests))
                self.assertEqual('"v1"', upstream.requests[1]['if-none-match'])
                self.assertTrue(response.extensions['caching']['revalidated'])
                self.assertEqual({'v': 1}, response.json())

    def test_persistent_cache_expires_idle_entries(self):
        """An entry unused for PERSISTENT_CACHE_TTI is fetched again."""
        clock = Clock()
        with TemporaryDirectory() as tmpdir, patch('time.time', clock):
            with CachingClient(cache_db_path=f'{tmpdir}/cache.db') as client:
                upstream = RevalidatingTransport(clock)
                client._transport.next_transport = upstream
                client.get(URL)

                # used again before it's idle for the TTI, so it's kept
                clock.now += PERSISTENT_CACHE_TTI - 1
                client.get(URL)
                self.assertEqual('"v1"', upstream.requests[1]['if-none-match'])

                clock.now += PERSISTENT_CACHE_TTI - 1
                client.get(URL)
                self.assertEqual('"v1"', upstream.requests[2]['if-none-match'])

                # unused for longer than the TTI, so it's gone
                clock.now += PERSISTENT_CACHE_TTI + 1
                response = client.get(URL)
                self.assertEqual(4, len(upstream.requests))
                self.assertNotIn('if-none-match', upstream.requests[3])
                self.assertFalse(response.extensions['caching']['revalidated'])

    def test_in_memory_cache_has_no_tti(self):
        """The in-memory cache only lasts one run, so it has no TTI."""
        with CachingClient() as client:
            self.assertIsNone(client._transport.config['storage'].default_tti)

    def test_unusable_cache_raises(self):
        """A cache database that can't be used is an error by default."""
        with TemporaryDirectory() as tmpdir:
            corrupt = Path(tmpdir) / 'corrupt.db'
            corrupt.write_bytes(b'not a database' * 100)
            with self.assertRaises(sqlite3.DatabaseError):
                CachingClient(cache_db_path=str(corrupt))

            # a directory can't be created under a file
            with self.assertRaises(OSError):
                CachingClient(cache_db_path=f'{corrupt}/sub/cache.db')

    def test_unusable_cache_falls_back_to_memory(self):
        """With fall_back_to_memory, an unusable cache is replaced by memory."""
        with TemporaryDirectory() as tmpdir:
            corrupt = Path(tmpdir) / 'corrupt.db'
            corrupt.write_bytes(b'not a database' * 100)
            for path in (str(corrupt), f'{corrupt}/sub/cache.db'):
                with self.assertLogs('proviso.utils', level='WARNING') as logs:
                    client = CachingClient(
                        cache_db_path=path, fall_back_to_memory=True
                    )
                self.assertIn('caching in memory instead', logs.output[0])
                self.assertIn(path, logs.output[0])

                with client:
                    upstream = mock_upstream(client)
                    client.get(URL)
                    response = client.get(URL)
                self.assertEqual(1, upstream.calls)
                self.assertTrue(response.extensions['caching']['from_cache'])

            # the corrupt file was left alone and nothing else was created
            self.assertEqual([corrupt], list(Path(tmpdir).iterdir()))
            self.assertEqual(b'not a database' * 100, corrupt.read_bytes())

    def test_usable_cache_with_fall_back_to_memory(self):
        """With fall_back_to_memory, a usable cache is still persistent."""
        with TemporaryDirectory() as tmpdir:
            db = f'{tmpdir}/proviso/cache.db'
            with CachingClient(
                cache_db_path=db, fall_back_to_memory=True
            ) as client:
                mock_upstream(client)
                client.get(URL)
            with CachingClient(cache_db_path=db) as client:
                upstream = mock_upstream(client)
                client.get(URL)
            self.assertEqual(0, upstream.calls)


class TestOpenCacheDb(TestCase):
    def test_creates_directories_and_file(self):
        """The database and any missing parent directories are created."""
        with TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / 'a' / 'b' / 'cache.db'
            _open_cache_db(str(db))
            self.assertTrue(db.is_file())

    def test_bare_filename(self):
        """A bare filename is created where hishel looks for it."""
        cwd = getcwd()
        with TemporaryDirectory() as tmpdir:
            chdir(tmpdir)
            try:
                _open_cache_db('cache.db')
                self.assertTrue(Path('.cache/hishel/cache.db').is_file())
            finally:
                chdir(cwd)

    def test_drops_v1_tables(self):
        """A hishel 1.x cache is cleared."""
        with TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / 'cache.db'
            create_hishel_v1_cache(db)
            with self.assertLogs('proviso.utils', level='WARNING'):
                _open_cache_db(str(db))
            self.assertEqual(set(), table_columns(db, 'entries'))

    def test_unopenable(self):
        """A file that can't be created raises."""
        with TemporaryDirectory() as tmpdir:
            # a directory where the database file should be
            db = Path(tmpdir) / 'cache.db'
            db.mkdir()
            with self.assertRaises(sqlite3.OperationalError):
                _open_cache_db(str(db))


class TestDefaultCacheDbPath(TestCase):
    def path(self, platform, environ):
        with (
            patch('proviso.utils.sys.platform', platform),
            patch.dict('os.environ', environ, clear=True),
            patch('proviso.utils.Path.home', return_value=Path('/home/u')),
        ):
            return default_cache_db_path()

    def test_xdg_cache_home(self):
        """XDG_CACHE_HOME is used on every platform."""
        for platform in ('linux', 'darwin', 'win32'):
            self.assertEqual(
                '/xdg/proviso/http-cache.db',
                self.path(platform, {'XDG_CACHE_HOME': '/xdg'}),
            )

    def test_relative_xdg_cache_home(self):
        """A relative or empty XDG_CACHE_HOME is ignored."""
        for value in ('relative', ''):
            self.assertEqual(
                '/home/u/.cache/proviso/http-cache.db',
                self.path('linux', {'XDG_CACHE_HOME': value}),
            )

    def test_linux(self):
        self.assertEqual(
            '/home/u/.cache/proviso/http-cache.db', self.path('linux', {})
        )

    def test_macos(self):
        self.assertEqual(
            '/home/u/Library/Caches/proviso/http-cache.db',
            self.path('darwin', {}),
        )

    def test_windows(self):
        self.assertEqual(
            '/appdata/proviso/Cache/http-cache.db',
            self.path('win32', {'LOCALAPPDATA': '/appdata'}),
        )
        self.assertEqual(
            '/home/u/AppData/Local/proviso/Cache/http-cache.db',
            self.path('win32', {}),
        )


class TestDropHishelV1Cache(TestCase):
    def test_missing_file(self):
        """A missing database is left alone."""
        with TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / 'cache.db'
            _drop_hishel_v1_cache(str(db))
            self.assertFalse(db.exists())

    def test_drops_v1_tables(self):
        """hishel 1.x tables are dropped."""
        with TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / 'cache.db'
            create_hishel_v1_cache(db)
            with self.assertLogs('proviso.utils', level='WARNING'):
                _drop_hishel_v1_cache(str(db))
            self.assertEqual(set(), table_columns(db, 'entries'))
            self.assertEqual(set(), table_columns(db, 'streams'))

    def test_leaves_other_tables(self):
        """Tables that aren't a hishel 1.x cache are left alone."""
        with TemporaryDirectory() as tmpdir:
            db = Path(tmpdir) / 'cache.db'
            connection = sqlite3.connect(str(db))
            with connection:
                connection.execute('CREATE TABLE entries (other TEXT)')
            connection.close()

            _drop_hishel_v1_cache(str(db))
            self.assertEqual({'other'}, table_columns(db, 'entries'))


class TestResolveCacheDbPath(TestCase):
    def test_bare_filename(self):
        """hishel puts a bare filename under .cache/hishel."""
        self.assertEqual(
            Path('.cache/hishel/cache.db'), _resolve_cache_db_path('cache.db')
        )

    def test_path_with_directory(self):
        """hishel uses a path with a directory as given."""
        self.assertEqual(
            Path('/tmp/x/cache.db'), _resolve_cache_db_path('/tmp/x/cache.db')
        )
        self.assertEqual(
            Path('x/cache.db'), _resolve_cache_db_path('x/cache.db')
        )
