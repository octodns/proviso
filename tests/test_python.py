from datetime import date
from unittest import TestCase
from unittest.mock import MagicMock, PropertyMock, patch

import httpx

from proviso.python import Python


class TestPython(TestCase):
    def setUp(self):
        """Set up test fixtures."""
        # Sample release data similar to what endoflife.date returns
        self.sample_releases = [
            {
                'cycle': '3.12',
                'releaseDate': '2023-10-02',
                'eol': '2028-10-02',
                'latest': '3.12.1',
            },
            {
                'cycle': '3.11',
                'releaseDate': '2022-10-24',
                'eol': '2027-10-24',
                'latest': '3.11.7',
            },
            {
                'cycle': '3.10',
                'releaseDate': '2021-10-04',
                'eol': '2026-10-04',
                'latest': '3.10.13',
            },
            {
                'cycle': '3.9',
                'releaseDate': '2020-10-05',
                'eol': '2025-10-05',
                'latest': '3.9.18',
            },
            {
                'cycle': '3.8',
                'releaseDate': '2019-10-14',
                'eol': '2024-10-14',
                'latest': '3.8.18',
            },
            {
                'cycle': '3.7',
                'releaseDate': '2018-06-27',
                'eol': '2023-06-27',
                'latest': '3.7.17',
            },
        ]

    def test_releases_fetches_from_api(self):
        """Test that releases property fetches from the API."""
        python_obj = Python()

        mock_response = MagicMock()
        mock_response.json.return_value = self.sample_releases

        with patch('httpx.get', return_value=mock_response) as mock_get:
            releases = python_obj.releases

            # Verify API was called
            mock_get.assert_called_once_with(Python.API_URL)
            mock_response.raise_for_status.assert_called_once()

            # Verify data returned
            self.assertEqual(self.sample_releases, releases)

    def test_releases_cached(self):
        """Test that releases property is cached."""
        python_obj = Python()

        mock_response = MagicMock()
        mock_response.json.return_value = self.sample_releases

        with patch('httpx.get', return_value=mock_response) as mock_get:
            # First access
            releases1 = python_obj.releases

            # Second access
            releases2 = python_obj.releases

            # Should only call API once due to caching
            mock_get.assert_called_once()

            # Both should return same data
            self.assertEqual(releases1, releases2)

    def test_releases_raises_on_http_error(self):
        """Test that releases raises exception on HTTP error."""
        python_obj = Python()

        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            'Error', request=MagicMock(), response=MagicMock()
        )

        with patch('httpx.get', return_value=mock_response):
            with self.assertRaises(httpx.HTTPStatusError):
                python_obj.releases

    def test_active_filters_by_date(self):
        """Test that active property filters releases by date."""
        python_obj = Python()

        # Mock today's date to be in the middle of the test data
        test_date = date(2024, 6, 1)

        with patch('proviso.python.date') as mock_date:
            mock_date.today.return_value = test_date
            mock_date.fromisoformat = date.fromisoformat

            # Mock releases using PropertyMock
            with patch.object(
                type(python_obj),
                'releases',
                new_callable=PropertyMock,
                return_value=self.sample_releases,
            ):
                active = python_obj.active

                # Should include 3.12, 3.11, 3.10, 3.9, 3.8
                # Should exclude 3.7 (EOL 2023-06-27)
                active_cycles = [r['cycle'] for r in active]
                self.assertEqual(
                    ['3.8', '3.9', '3.10', '3.11', '3.12'], active_cycles
                )

    def test_active_sorts_by_version(self):
        """Test that active property sorts releases by version."""
        python_obj = Python()

        # Provide releases in random order
        unsorted_releases = [
            self.sample_releases[2],  # 3.10
            self.sample_releases[0],  # 3.12
            self.sample_releases[1],  # 3.11
            self.sample_releases[3],  # 3.9
        ]

        test_date = date(2024, 6, 1)

        with patch('proviso.python.date') as mock_date:
            mock_date.today.return_value = test_date
            mock_date.fromisoformat = date.fromisoformat

            with patch.object(
                type(python_obj),
                'releases',
                new_callable=PropertyMock,
                return_value=unsorted_releases,
            ):
                active = python_obj.active

                # Should be sorted oldest to newest
                active_cycles = [r['cycle'] for r in active]
                self.assertEqual(['3.9', '3.10', '3.11', '3.12'], active_cycles)

    def test_active_excludes_future_releases(self):
        """Test that active excludes releases not yet released."""
        python_obj = Python()

        future_release = {
            'cycle': '3.13',
            'releaseDate': '2025-10-01',
            'eol': '2030-10-01',
            'latest': '3.13.0',
        }

        releases_with_future = self.sample_releases + [future_release]
        test_date = date(2024, 6, 1)

        with patch('proviso.python.date') as mock_date:
            mock_date.today.return_value = test_date
            mock_date.fromisoformat = date.fromisoformat

            with patch.object(
                type(python_obj),
                'releases',
                new_callable=PropertyMock,
                return_value=releases_with_future,
            ):
                active = python_obj.active

                # Should not include 3.13 (future release)
                active_cycles = [r['cycle'] for r in active]
                self.assertNotIn('3.13', active_cycles)

    def test_active_excludes_eol_releases(self):
        """Test that active excludes end-of-life releases."""
        python_obj = Python()

        # Test date after 3.8's EOL
        test_date = date(2024, 12, 1)

        with patch('proviso.python.date') as mock_date:
            mock_date.today.return_value = test_date
            mock_date.fromisoformat = date.fromisoformat

            with patch.object(
                type(python_obj),
                'releases',
                new_callable=PropertyMock,
                return_value=self.sample_releases,
            ):
                active = python_obj.active

                # Should not include 3.8 (EOL 2024-10-14) or 3.7
                active_cycles = [r['cycle'] for r in active]
                self.assertNotIn('3.8', active_cycles)
                self.assertNotIn('3.7', active_cycles)

                # Should include 3.9, 3.10, 3.11, 3.12
                self.assertEqual(['3.9', '3.10', '3.11', '3.12'], active_cycles)

    def test_active_on_release_date_boundary(self):
        """Test active on exact release date."""
        python_obj = Python()

        # Test on exact release date of 3.12
        test_date = date(2023, 10, 2)

        with patch('proviso.python.date') as mock_date:
            mock_date.today.return_value = test_date
            mock_date.fromisoformat = date.fromisoformat

            with patch.object(
                type(python_obj),
                'releases',
                new_callable=PropertyMock,
                return_value=self.sample_releases,
            ):
                active = python_obj.active

                # Should include 3.12 (released on this date)
                active_cycles = [r['cycle'] for r in active]
                self.assertIn('3.12', active_cycles)

    def test_active_on_eol_date_boundary(self):
        """Test active on exact EOL date."""
        python_obj = Python()

        # Test on exact EOL date of 3.7
        test_date = date(2023, 6, 27)

        with patch('proviso.python.date') as mock_date:
            mock_date.today.return_value = test_date
            mock_date.fromisoformat = date.fromisoformat

            with patch.object(
                type(python_obj),
                'releases',
                new_callable=PropertyMock,
                return_value=self.sample_releases,
            ):
                active = python_obj.active

                # Should include 3.7 (EOL on this date, still active)
                active_cycles = [r['cycle'] for r in active]
                self.assertIn('3.7', active_cycles)

    def test_active_version_sorting_with_two_digit_minor(self):
        """Test that version sorting handles two-digit minor versions correctly."""
        python_obj = Python()

        # Create releases with versions that would sort incorrectly as strings
        releases_with_high_versions = [
            {
                'cycle': '3.9',
                'releaseDate': '2020-10-05',
                'eol': '2025-10-05',
                'latest': '3.9.18',
            },
            {
                'cycle': '3.10',
                'releaseDate': '2021-10-04',
                'eol': '2026-10-04',
                'latest': '3.10.13',
            },
            {
                'cycle': '3.11',
                'releaseDate': '2022-10-24',
                'eol': '2027-10-24',
                'latest': '3.11.7',
            },
        ]

        test_date = date(2024, 6, 1)

        with patch('proviso.python.date') as mock_date:
            mock_date.today.return_value = test_date
            mock_date.fromisoformat = date.fromisoformat

            with patch.object(
                type(python_obj),
                'releases',
                new_callable=PropertyMock,
                return_value=releases_with_high_versions,
            ):
                active = python_obj.active

                # Should be sorted as 3.9, 3.10, 3.11 (not 3.10, 3.11, 3.9)
                active_cycles = [r['cycle'] for r in active]
                self.assertEqual(['3.9', '3.10', '3.11'], active_cycles)
