from unittest import TestCase
from unittest.mock import MagicMock, patch

from httpx import Client
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
    def test_cache_hit(self):
        """Test that cached responses are returned without fetching."""
        client = CachingClient()
        url = 'https://example.com/test'

        # Mock the parent get method
        mock_response = MagicMock()
        with patch.object(Client, 'get', return_value=mock_response):
            # First call should fetch
            response1 = client.get(url)
            self.assertEqual(mock_response, response1)

            # Second call should return cached response
            response2 = client.get(url)
            self.assertEqual(mock_response, response2)

            # Verify parent get was only called once
            Client.get.assert_called_once_with(url)

    def test_cache_miss(self):
        """Test that different URLs are not cached together."""
        client = CachingClient()
        url1 = 'https://example.com/test1'
        url2 = 'https://example.com/test2'

        mock_response1 = MagicMock()
        mock_response2 = MagicMock()

        with patch.object(
            Client, 'get', side_effect=[mock_response1, mock_response2]
        ):
            # First URL
            response1 = client.get(url1)
            self.assertEqual(mock_response1, response1)

            # Second URL (different, should fetch)
            response2 = client.get(url2)
            self.assertEqual(mock_response2, response2)

            # Verify parent get was called twice
            self.assertEqual(2, Client.get.call_count)

    def test_cache_key_based_on_url(self):
        """Test that cache key is based on URL."""
        client = CachingClient()
        url = 'https://example.com/test'

        mock_response = MagicMock()

        with patch.object(Client, 'get', return_value=mock_response):
            # First call
            client.get(url)

            # Call with same URL should use cache
            client.get(url)

            # Verify the cache contains the URL
            self.assertIn(url, client._cache)
            self.assertEqual(mock_response, client._cache[url])

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
