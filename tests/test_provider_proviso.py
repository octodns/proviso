#
#
#

from unittest import TestCase
from unittest.mock import MagicMock, patch

from packaging.requirements import Requirement

from proviso import Proviso
from proviso.resolver import (
    PyPIProvider,
    Resolver,
    format_python_version_for_markers,
    parse_python_version,
)


class TestProviso(TestCase):
    # TODO: test provider
    def test_nothing(self):
        self.assertTrue(True)
        Proviso


class TestPythonVersionParsing(TestCase):
    def test_parse_python_version_major_minor(self):
        """Test parsing version string with major.minor only."""
        result = parse_python_version('3.9')
        self.assertEqual(result, (3, 9))

    def test_parse_python_version_full(self):
        """Test parsing version string with major.minor.patch."""
        result = parse_python_version('3.10.5')
        self.assertEqual(result, (3, 10, 5))

    def test_parse_python_version_with_extra(self):
        """Test parsing version string with extra components."""
        result = parse_python_version('3.11.0.0')
        self.assertEqual(result, (3, 11, 0, 0))

    def test_format_python_version_for_markers_major_minor(self):
        """Test formatting version tuple for markers (major.minor)."""
        result = format_python_version_for_markers((3, 9))
        self.assertEqual(result['python_version'], '3.9')
        self.assertEqual(result['python_full_version'], '3.9.0')

    def test_format_python_version_for_markers_full(self):
        """Test formatting version tuple for markers (major.minor.patch)."""
        result = format_python_version_for_markers((3, 10, 5))
        self.assertEqual(result['python_version'], '3.10')
        self.assertEqual(result['python_full_version'], '3.10.5')


class TestResolverWithPythonVersion(TestCase):
    def test_resolver_init(self):
        """Test Resolver initialization."""
        resolver = Resolver()
        self.assertIsNotNone(resolver._session)
        self.assertIsNotNone(resolver.index_urls)


class TestPyPIProviderEnvironment(TestCase):
    def test_provider_without_python_version(self):
        """Test PyPIProvider without custom python_version uses default environment."""
        session = MagicMock()
        provider = PyPIProvider(
            session, index_urls=['https://pypi.org/simple/']
        )
        self.assertIsNone(provider.environment)

    def test_provider_with_python_version(self):
        """Test PyPIProvider with custom python_version creates custom environment."""
        session = MagicMock()
        provider = PyPIProvider(
            session,
            index_urls=['https://pypi.org/simple/'],
            python_version='3.9',
        )

        self.assertIsNotNone(provider.environment)
        self.assertEqual(provider.environment['python_version'], '3.9')
        self.assertEqual(provider.environment['python_full_version'], '3.9.0')

    def test_provider_marker_evaluation_with_custom_environment(self):
        """Test that markers are evaluated with custom environment."""
        session = MagicMock()
        provider = PyPIProvider(
            session,
            index_urls=['https://pypi.org/simple/'],
            python_version='3.9',
        )

        # Create a mock package with metadata that has version-specific dependencies
        mock_package = MagicMock()
        mock_package.version = '1.0.0'
        mock_package.link.dist_info_link.url = 'http://example.com/metadata'

        mock_result = MagicMock()
        mock_result.best = mock_package
        provider.finder.find_best_match = MagicMock(return_value=mock_result)

        # Create metadata with a requirement that has a python_version marker
        metadata_text = """Metadata-Version: 2.1
Name: test-package
Version: 1.0.0
Requires-Dist: typing-extensions; python_version < "3.10"
"""

        with patch('httpx.get') as mock_get:
            mock_response = MagicMock()
            mock_response.text = metadata_text
            mock_get.return_value = mock_response

            from proviso.resolver import Candidate

            candidate = Candidate('test-package', '1.0.0')
            dependencies = provider.get_dependencies(candidate)

            # With Python 3.9, typing-extensions should be included
            # (since 3.9 < 3.10)
            self.assertEqual(len(dependencies), 1)
            self.assertEqual(dependencies[0].name, 'typing-extensions')

        # Now test with Python 3.11 where typing-extensions should be excluded
        provider_311 = PyPIProvider(
            session,
            index_urls=['https://pypi.org/simple/'],
            python_version='3.11',
        )
        provider_311.finder.find_best_match = MagicMock(
            return_value=mock_result
        )

        with patch('httpx.get') as mock_get:
            mock_response = MagicMock()
            mock_response.text = metadata_text
            mock_get.return_value = mock_response

            dependencies = provider_311.get_dependencies(candidate)

            # With Python 3.11, typing-extensions should NOT be included
            # (since 3.11 is not < 3.10)
            self.assertEqual(len(dependencies), 0)


class TestExtrasFiltering(TestCase):
    def test_parse_extras_single(self):
        """Test parsing single extra from comma-separated string."""
        extras_str = 'dev'
        extras = set(e.strip() for e in extras_str.split(',') if e.strip())
        self.assertEqual(extras, {'dev'})

    def test_parse_extras_multiple(self):
        """Test parsing multiple extras from comma-separated string."""
        extras_str = 'dev,test,docs'
        extras = set(e.strip() for e in extras_str.split(',') if e.strip())
        self.assertEqual(extras, {'dev', 'test', 'docs'})

    def test_parse_extras_with_spaces(self):
        """Test parsing extras with spaces."""
        extras_str = 'dev, test , docs'
        extras = set(e.strip() for e in extras_str.split(',') if e.strip())
        self.assertEqual(extras, {'dev', 'test', 'docs'})

    def test_parse_extras_empty(self):
        """Test parsing empty extras string."""
        extras_str = ''
        extras = set(e.strip() for e in extras_str.split(',') if e.strip())
        self.assertEqual(extras, set())

    def test_marker_evaluation_with_extra(self):
        """Test that requirements with extra markers are properly evaluated."""
        # Create a requirement with an extra marker
        req = Requirement('pytest>=6.0; extra == "dev"')

        # Should evaluate to True when extra is 'dev'
        self.assertTrue(req.marker.evaluate({'extra': 'dev'}))

        # Should evaluate to False when extra is 'test'
        self.assertFalse(req.marker.evaluate({'extra': 'test'}))

    def test_marker_evaluation_with_multiple_extras(self):
        """Test filtering requirements with multiple extras."""
        # Create requirements with different extra markers
        req_dev = Requirement('pytest>=6.0; extra == "dev"')
        req_test = Requirement('coverage>=5.0; extra == "test"')
        req_docs = Requirement('sphinx>=4.0; extra == "docs"')
        req_no_marker = Requirement('requests>=2.0')

        all_reqs = [req_dev, req_test, req_docs, req_no_marker]
        requested_extras = {'dev', 'test'}

        # Filter requirements
        filtered = []
        for req in all_reqs:
            if req.marker is None:
                filtered.append(req)
            elif 'extra' in str(req.marker):
                for extra in requested_extras:
                    if req.marker.evaluate({'extra': extra}):
                        filtered.append(req)
                        break
            else:
                filtered.append(req)

        # Should include: pytest (dev), coverage (test), requests (no marker)
        # Should exclude: sphinx (docs not requested)
        self.assertEqual(len(filtered), 3)
        names = {req.name for req in filtered}
        self.assertEqual(names, {'pytest', 'coverage', 'requests'})

    def test_marker_evaluation_combined_markers(self):
        """Test requirements with combined extra and python_version markers."""
        # Requirement with both extra and python_version markers
        req = Requirement(
            'typing-extensions>=4.0; extra == "dev" and python_version < "3.10"'
        )

        # Should evaluate to True when both conditions match
        self.assertTrue(
            req.marker.evaluate({'extra': 'dev', 'python_version': '3.9'})
        )

        # Should evaluate to False when extra doesn't match
        self.assertFalse(
            req.marker.evaluate({'extra': 'test', 'python_version': '3.9'})
        )

        # Should evaluate to False when python_version doesn't match
        self.assertFalse(
            req.marker.evaluate({'extra': 'dev', 'python_version': '3.11'})
        )
