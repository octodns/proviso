import sqlite3
from os import chdir, getcwd
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import httpx2
from unearth.fetchers import DEFAULT_SECURE_ORIGINS

from proviso.utils import (
    CachingClient,
    _drop_hishel_v1_cache,
    _resolve_cache_db_path,
    format_python_version_for_markers,
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


class TestFormatPythonVersionForMarkers(TestCase):
    def test_major_minor_version(self):
        """Test formatting with major.minor version."""
        result = format_python_version_for_markers('3.9')
        self.assertEqual('3.9', result['python_version'])
        self.assertEqual('3.9.0', result['python_full_version'])

    def test_major_minor_patch_version(self):
        """Test formatting with major.minor.patch version."""
        result = format_python_version_for_markers('3.10.5')
        self.assertEqual('3.10', result['python_version'])
        self.assertEqual('3.10.5', result['python_full_version'])

    def test_full_version_string(self):
        """Test formatting with full version string."""
        result = format_python_version_for_markers('3.11.0')
        self.assertEqual('3.11', result['python_version'])
        self.assertEqual('3.11.0', result['python_full_version'])

    def test_different_major_versions(self):
        """Test formatting with different major versions."""
        # Python 2.7 (for completeness, though not supported)
        result = format_python_version_for_markers('2.7')
        self.assertEqual('2.7', result['python_version'])
        self.assertEqual('2.7.0', result['python_full_version'])

        # Python 3.12
        result = format_python_version_for_markers('3.12')
        self.assertEqual('3.12', result['python_version'])
        self.assertEqual('3.12.0', result['python_full_version'])

    def test_version_with_higher_patch(self):
        """Test formatting with higher patch numbers."""
        result = format_python_version_for_markers('3.9.18')
        self.assertEqual('3.9', result['python_version'])
        self.assertEqual('3.9.18', result['python_full_version'])


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
