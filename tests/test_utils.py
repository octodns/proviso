from unittest import TestCase
from unittest.mock import patch

from unearth.fetchers import DEFAULT_SECURE_ORIGINS

from proviso.utils import CachingClient, format_python_version_for_markers


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
        client = CachingClient()
        self.assertIsNotNone(client)

    def test_client_instantiation_with_persistent_cache(self):
        """Test that CachingClient can be instantiated with persistent cache."""
        from tempfile import NamedTemporaryFile

        with NamedTemporaryFile(suffix='.db', delete=True) as tmpfile:
            client = CachingClient(cache_db_path=tmpfile.name)
            self.assertIsNotNone(client)

    def test_get_stream(self):
        """Test get_stream method required by Fetcher protocol."""
        client = CachingClient()
        url = 'https://example.com/test'
        headers = {'User-Agent': 'test'}

        with patch.object(client, 'stream') as mock_stream:
            client.get_stream(url, headers=headers)
            mock_stream.assert_called_once_with('GET', url, headers=headers)

    def test_iter_secure_origins(self):
        """Test iter_secure_origins method required by Fetcher protocol."""
        client = CachingClient()
        origins = list(client.iter_secure_origins())

        # Should return DEFAULT_SECURE_ORIGINS
        self.assertEqual(list(DEFAULT_SECURE_ORIGINS), origins)
        self.assertGreater(len(origins), 0)
