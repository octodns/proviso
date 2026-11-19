import math
import tarfile
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from functools import partial
from io import BytesIO
from os.path import join
from pathlib import Path
from subprocess import CalledProcessError
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch
from zipfile import ZipFile

import httpx2
from build import BuildBackendException
from packaging.metadata import InvalidMetadata, Metadata
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version
from resolvelib import ResolutionImpossible
from unearth import Link, PackageFinder
from unearth.errors import UnpackError
from unearth.fetchers import DEFAULT_SECURE_ORIGINS

from proviso.main import _resolution_error_message
from proviso.resolver import (
    _CONFLICT_PRIORITY_THRESHOLD,
    MAX_ROUNDS,
    BasePin,
    Candidate,
    ConflictReporter,
    MetadataUnavailableError,
    PyPIProvider,
    Resolver,
    UnsupportedRequirementError,
    filter_requirements,
    is_exact_pin,
    make_identifier,
    redact_requirement,
    split_identifier,
)
from proviso.series import SeriesPackageFinder

# Lightweight stand-in for resolvelib's RequirementInformation namedtuple.
# resolvelib defines RequirementInformation(requirement, parent); we use the
# same field names so tests read naturally.
_RequirementInfo = namedtuple('_RequirementInfo', ['requirement', 'parent'])

NOW = datetime.now(timezone.utc)


def index_file(name, version, days=100, sdist=False, tag='py3', **kwargs):
    """Return a PEP 691 file entry uploaded days ago, with extra keys."""
    if sdist:
        filename = f'{name}-{version}.tar.gz'
    else:
        filename = f'{name}-{version}-{tag}-none-any.whl'
    entry = {
        'filename': filename,
        'url': f'https://files.example.com/{filename}',
        'hashes': {},
        'core-metadata': True,
        'upload-time': (NOW - timedelta(days=days)).isoformat(),
    }
    entry.update(kwargs)
    return entry


class FakeIndex(httpx2.Client):
    """A PEP 691 JSON index serving files and their metadata.

    files maps a package name to its file entries, see index_file.
    requires maps (name, version) to that release's Requires-Dist.
    Index pages on html_hosts use the HTML API, which has no upload times.
    """

    def __init__(self, files, requires=None, html_hosts=()):
        requires = requires or {}

        def handler(request):
            path = request.url.path
            if path.endswith('.metadata'):
                name, version = path.split('/')[-1].split('-')[:2]
                lines = [
                    'Metadata-Version: 2.1',
                    f'Name: {name}',
                    f'Version: {version}',
                ]
                lines.extend(
                    f'Requires-Dist: {r}'
                    for r in requires.get((name, version), ())
                )
                return httpx2.Response(200, text='\n'.join(lines) + '\n')
            name = path.rstrip('/').split('/')[-1]
            if request.url.host in html_hosts:
                anchors = ''.join(
                    f'<a href="{f["url"]}">{f["filename"]}</a>'
                    for f in files.get(name, [])
                )
                return httpx2.Response(
                    200,
                    headers={'Content-Type': 'text/html'},
                    text=f'<html><body>{anchors}</body></html>',
                )
            return httpx2.Response(
                200,
                headers={'Content-Type': 'application/vnd.pypi.simple.v1+json'},
                json={
                    'meta': {'api-version': '1.1'},
                    'name': name,
                    'files': files.get(name, []),
                },
            )

        super().__init__(transport=httpx2.MockTransport(handler))

    def iter_secure_origins(self):
        yield from DEFAULT_SECURE_ORIGINS


class TestCandidate(TestCase):
    def test_init_basic(self):
        """Test Candidate initialization with basic parameters."""
        candidate = Candidate('requests', Version('2.28.0'))

        self.assertEqual('requests', candidate.name)
        self.assertEqual(Version('2.28.0'), candidate.version)
        self.assertEqual(frozenset(), candidate.extras)
        self.assertIsNone(candidate._dependencies)

    def test_init_with_extras(self):
        """Test Candidate initialization with extras."""
        extras = frozenset(['dev', 'test'])
        candidate = Candidate('package', Version('1.0.0'), extras=extras)

        self.assertEqual(extras, candidate.extras)

    def test_init_with_dependencies(self):
        """Test Candidate initialization with dependencies."""
        deps = [Requirement('dep1'), Requirement('dep2')]
        candidate = Candidate('package', Version('1.0.0'), dependencies=deps)

        self.assertEqual(deps, candidate._dependencies)

    def test_init_with_package(self):
        """Test Candidate stores the unearth package object."""
        mock_package = MagicMock()
        candidate = Candidate('package', Version('1.0.0'), package=mock_package)

        self.assertIs(mock_package, candidate.package)

    def test_init_package_defaults_to_none(self):
        """Test Candidate.package defaults to None when not provided."""
        candidate = Candidate('package', Version('1.0.0'))

        self.assertIsNone(candidate.package)

    def test_equality_ignores_package(self):
        """Test that __eq__ and __hash__ ignore the package attribute."""
        pkg_a = MagicMock()
        pkg_b = MagicMock()
        c1 = Candidate('requests', Version('2.28.0'), package=pkg_a)
        c2 = Candidate('requests', Version('2.28.0'), package=pkg_b)

        self.assertEqual(c1, c2)
        self.assertEqual(hash(c1), hash(c2))

    def test_init_canonicalizes_name(self):
        """Test that package names are canonicalized."""
        candidate = Candidate('My_Package.Name', Version('1.0.0'))

        # Should be lowercased and underscores/dots converted to hyphens
        self.assertEqual('my-package-name', candidate.name)

    def test_repr(self):
        """Test Candidate string representation."""
        candidate = Candidate('requests', Version('2.28.0'))

        self.assertEqual(
            "Candidate('requests', <Version('2.28.0')>)", repr(candidate)
        )

    def test_equality_same_candidates(self):
        """Test that identical candidates are equal."""
        candidate1 = Candidate('requests', Version('2.28.0'))
        candidate2 = Candidate('requests', Version('2.28.0'))

        self.assertEqual(candidate1, candidate2)

    def test_equality_different_versions(self):
        """Test that candidates with different versions are not equal."""
        candidate1 = Candidate('requests', Version('2.28.0'))
        candidate2 = Candidate('requests', Version('2.27.0'))

        self.assertNotEqual(candidate1, candidate2)

    def test_equality_different_extras(self):
        """Test that candidates with different extras are not equal."""
        candidate1 = Candidate(
            'package', Version('1.0.0'), extras=frozenset(['dev'])
        )
        candidate2 = Candidate(
            'package', Version('1.0.0'), extras=frozenset(['test'])
        )

        self.assertNotEqual(candidate1, candidate2)

    def test_equality_non_candidate(self):
        """Test that comparing with non-Candidate returns NotImplemented."""
        candidate = Candidate('requests', Version('2.28.0'))

        self.assertEqual(NotImplemented, candidate.__eq__('not a candidate'))

    def test_hash_consistent(self):
        """Test that hash is consistent for equal candidates."""
        candidate1 = Candidate('requests', Version('2.28.0'))
        candidate2 = Candidate('requests', Version('2.28.0'))

        self.assertEqual(hash(candidate1), hash(candidate2))

    def test_hash_different_for_different_candidates(self):
        """Test that hash is different for different candidates."""
        candidate1 = Candidate('requests', Version('2.28.0'))
        candidate2 = Candidate('requests', Version('2.27.0'))

        # They should have different hashes (though collision is possible)
        self.assertNotEqual(hash(candidate1), hash(candidate2))

    def test_candidates_in_set(self):
        """Test that candidates can be used in sets."""
        candidate1 = Candidate('requests', Version('2.28.0'))
        candidate2 = Candidate('requests', Version('2.28.0'))
        candidate3 = Candidate('requests', Version('2.27.0'))

        candidates_set = {candidate1, candidate2, candidate3}

        # Should only have 2 unique candidates
        self.assertEqual(2, len(candidates_set))
        self.assertIn(candidate1, candidates_set)
        self.assertIn(candidate3, candidates_set)


class TestPyPIProvider(TestCase):
    def test_init_without_python_version(self):
        """Test PyPIProvider initialization without python_version."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.SeriesPackageFinder') as mock_finder:
            provider = PyPIProvider(session, index_urls)

            # Verify PackageFinder was created correctly
            mock_finder.assert_called_once_with(
                session=session, index_urls=index_urls, target_python=None
            )

            # No target series when no python_version specified
            self.assertIsNone(provider.series)

    def test_init_with_python_version(self):
        """Test PyPIProvider initialization with python_version."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']
        python_version = '3.10'

        with patch('proviso.resolver.SeriesPackageFinder'):
            with patch('proviso.resolver.TargetPython') as mock_target_python:
                provider = PyPIProvider(session, index_urls, python_version)

                # Verify TargetPython was created with correct version
                mock_target_python.assert_called_once()
                call_kwargs = mock_target_python.call_args[1]
                self.assertEqual((3, 10), call_kwargs['py_ver'])

                # Markers are evaluated for the 3.10 series
                self.assertEqual('3.10', provider.series.version)
                self.assertEqual((3, 10), provider.series.release)

    def test_identify_with_requirement(self):
        """Test identify method with a Requirement."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.SeriesPackageFinder') as mock_finder:
            provider = PyPIProvider(session, index_urls)
            # Ensure finder was created
            self.assertIsNotNone(mock_finder)

            req = Requirement('My_Package>=1.0.0')
            identifier = provider.identify(req)

            # Should return canonicalized name
            self.assertEqual('my-package', identifier)

    def test_identify_with_candidate(self):
        """Test identify method with a Candidate."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.SeriesPackageFinder'):
            provider = PyPIProvider(session, index_urls)

            candidate = Candidate('my-package', Version('1.0.0'))
            identifier = provider.identify(candidate)

            self.assertEqual('my-package', identifier)

    def test_is_satisfied_by_matching(self):
        """Test is_satisfied_by with matching candidate."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.SeriesPackageFinder'):
            provider = PyPIProvider(session, index_urls)

            req = Requirement('requests>=2.0.0,<3.0.0')
            candidate = Candidate('requests', Version('2.28.0'))

            self.assertTrue(provider.is_satisfied_by(req, candidate))

    def test_is_satisfied_by_not_matching_version(self):
        """Test is_satisfied_by with non-matching version."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.SeriesPackageFinder'):
            provider = PyPIProvider(session, index_urls)

            req = Requirement('requests>=2.0.0,<3.0.0')
            candidate = Candidate('requests', Version('1.0.0'))

            self.assertFalse(provider.is_satisfied_by(req, candidate))

    def test_is_satisfied_by_different_package(self):
        """Test is_satisfied_by with different package name."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.SeriesPackageFinder'):
            provider = PyPIProvider(session, index_urls)

            req = Requirement('requests>=2.0.0')
            candidate = Candidate('urllib3', Version('2.0.0'))

            self.assertFalse(provider.is_satisfied_by(req, candidate))

    def test_get_preference_free_requirement(self):
        """A free (no specifier) requirement returns the highest-value tuple."""
        with patch('proviso.resolver.SeriesPackageFinder'):
            provider = PyPIProvider(MagicMock(), ['https://pypi.org/simple/'])

            info = _RequirementInfo(
                requirement=Requirement('requests'), parent=None
            )
            pref = provider.get_preference(
                'requests', {}, {}, {'requests': [info]}, []
            )

            # not conflict_promoted=True, not direct=True, not pinned=True,
            # not upper_bounded=True, requested_order=inf, not unfree=True, name
            self.assertEqual(
                (True, True, True, True, math.inf, True, 'requests'), pref
            )

    def test_get_preference_pinned_before_free(self):
        """A pinned (==x.y.z) requirement sorts before a free one."""
        with patch('proviso.resolver.SeriesPackageFinder'):
            provider = PyPIProvider(MagicMock(), ['https://pypi.org/simple/'])

            info_pinned = _RequirementInfo(
                requirement=Requirement('requests==2.28.0'), parent=None
            )
            info_free = _RequirementInfo(
                requirement=Requirement('urllib3'), parent=None
            )
            information = {'requests': [info_pinned], 'urllib3': [info_free]}
            pref_pinned = provider.get_preference(
                'requests', {}, {}, information, []
            )
            pref_free = provider.get_preference(
                'urllib3', {}, {}, information, []
            )

            self.assertLess(pref_pinned, pref_free)

    def test_get_preference_upper_bounded_before_free(self):
        """An upper-bounded requirement sorts before a free one."""
        with patch('proviso.resolver.SeriesPackageFinder'):
            provider = PyPIProvider(MagicMock(), ['https://pypi.org/simple/'])

            info_bounded = _RequirementInfo(
                requirement=Requirement('requests<3.0'), parent=None
            )
            info_free = _RequirementInfo(
                requirement=Requirement('urllib3'), parent=None
            )
            information = {'requests': [info_bounded], 'urllib3': [info_free]}
            pref_bounded = provider.get_preference(
                'requests', {}, {}, information, []
            )
            pref_free = provider.get_preference(
                'urllib3', {}, {}, information, []
            )

            self.assertLess(pref_bounded, pref_free)

    def test_get_preference_user_requested_order(self):
        """Earlier user-specified requirements sort before later ones."""
        with patch('proviso.resolver.SeriesPackageFinder'):
            user_requested = {'requests': 0, 'urllib3': 1}
            provider = PyPIProvider(
                MagicMock(),
                ['https://pypi.org/simple/'],
                user_requested=user_requested,
            )

            info_a = _RequirementInfo(
                requirement=Requirement('requests'), parent=None
            )
            info_b = _RequirementInfo(
                requirement=Requirement('urllib3'), parent=None
            )
            information = {'requests': [info_a], 'urllib3': [info_b]}

            pref_first = provider.get_preference(
                'requests', {}, {}, information, []
            )
            pref_second = provider.get_preference(
                'urllib3', {}, {}, information, []
            )

            self.assertLess(pref_first, pref_second)

    def test_get_preference_conflict_promoted_sorts_first(self):
        """A conflict-promoted package sorts before all others."""
        with patch('proviso.resolver.SeriesPackageFinder'):
            provider = PyPIProvider(MagicMock(), ['https://pypi.org/simple/'])
            provider._conflict_promoted.add('requests')

            info = _RequirementInfo(
                requirement=Requirement('requests'), parent=None
            )
            info_other = _RequirementInfo(
                requirement=Requirement('urllib3==1.26.0'), parent=None
            )
            information = {'requests': [info], 'urllib3': [info_other]}

            pref_promoted = provider.get_preference(
                'requests', {}, {}, information, []
            )
            pref_pinned = provider.get_preference(
                'urllib3', {}, {}, information, []
            )

            # Promoted always sorts first regardless of pin/bound status
            self.assertLess(pref_promoted, pref_pinned)

    def test_get_preference_direct_url_before_pinned(self):
        """A direct-URL requirement sorts before a pinned specifier."""
        with patch('proviso.resolver.SeriesPackageFinder'):
            provider = PyPIProvider(MagicMock(), ['https://pypi.org/simple/'])

            info_direct = _RequirementInfo(
                requirement=Requirement(
                    'mylib @ https://example.com/mylib.tar.gz'
                ),
                parent=None,
            )
            info_pinned = _RequirementInfo(
                requirement=Requirement('requests==2.28.0'), parent=None
            )
            information = {'mylib': [info_direct], 'requests': [info_pinned]}

            pref_direct = provider.get_preference(
                'mylib', {}, {}, information, []
            )
            pref_pinned = provider.get_preference(
                'requests', {}, {}, information, []
            )

            self.assertLess(pref_direct, pref_pinned)

    def test_find_matches_no_requirements(self):
        """Test find_matches with no requirements."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.SeriesPackageFinder'):
            provider = PyPIProvider(session, index_urls)

            # Empty requirements dict
            requirements = {}
            incompatibilities = {}

            result = provider.find_matches(
                'requests', requirements, incompatibilities
            )

            self.assertEqual([], result)

    def test_find_matches_with_candidates(self):
        """Test find_matches returns matching candidates."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.SeriesPackageFinder') as mock_finder_class:
            mock_finder = MagicMock()
            mock_finder_class.return_value = mock_finder

            provider = PyPIProvider(session, index_urls)

            # Mock package results
            mock_package1 = MagicMock()
            mock_package1.version = '2.28.0'
            mock_package1.link.is_yanked = False
            mock_package2 = MagicMock()
            mock_package2.version = '2.27.0'
            mock_package2.link.is_yanked = False
            mock_package3 = MagicMock()
            mock_package3.version = '2.26.0'
            mock_package3.link.is_yanked = False

            mock_finder.find_matches.return_value = [
                mock_package1,
                mock_package2,
                mock_package3,
            ]

            requirements = {'requests': [Requirement('requests>=2.27.0')]}
            incompatibilities = {}

            result = provider.find_matches(
                'requests', requirements, incompatibilities
            )

            # Should return candidates sorted newest first
            self.assertEqual(2, len(result))
            self.assertEqual('2.28.0', str(result[0].version))
            self.assertEqual('2.27.0', str(result[1].version))

    def test_find_matches_with_incompatibilities(self):
        """Test find_matches filters incompatible versions."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.SeriesPackageFinder') as mock_finder_class:
            mock_finder = MagicMock()
            mock_finder_class.return_value = mock_finder

            provider = PyPIProvider(session, index_urls)

            mock_package1 = MagicMock()
            mock_package1.version = '2.28.0'
            mock_package1.link.is_yanked = False
            mock_package2 = MagicMock()
            mock_package2.version = '2.27.0'
            mock_package2.link.is_yanked = False

            mock_finder.find_matches.return_value = [
                mock_package1,
                mock_package2,
            ]

            requirements = {'requests': [Requirement('requests>=2.27.0')]}
            # Mark 2.28.0 as incompatible
            incompatibilities = {
                'requests': [Candidate('requests', Version('2.28.0'))]
            }

            result = provider.find_matches(
                'requests', requirements, incompatibilities
            )

            # Should only return 2.27.0
            self.assertEqual(1, len(result))
            self.assertEqual('2.27.0', str(result[0].version))

    def test_find_matches_cache_avoids_second_unearth_call(self):
        """Test that a second find_matches call for the same identifier reuses
        the cached result without querying unearth again."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.SeriesPackageFinder') as mock_finder_class:
            mock_finder = MagicMock()
            mock_finder_class.return_value = mock_finder

            provider = PyPIProvider(session, index_urls)

            mock_package1 = MagicMock()
            mock_package1.version = '2.28.0'
            mock_package1.link.is_yanked = False
            mock_package2 = MagicMock()
            mock_package2.version = '2.27.0'
            mock_package2.link.is_yanked = False

            mock_finder.find_matches.return_value = [
                mock_package1,
                mock_package2,
            ]

            requirements = {'requests': [Requirement('requests>=2.27.0')]}
            incompatibilities = {}

            result1 = provider.find_matches(
                'requests', requirements, incompatibilities
            )
            result2 = provider.find_matches(
                'requests', requirements, incompatibilities
            )

            # unearth should only be called once — the second call hits the cache
            mock_finder.find_matches.assert_called_once()
            # Both calls return the same candidates
            self.assertEqual(
                [str(c.version) for c in result1],
                [str(c.version) for c in result2],
            )

    def test_find_matches_cache_keyed_by_name(self):
        """Test that find_matches queries unearth with the bare package name."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.SeriesPackageFinder') as mock_finder_class:
            mock_finder = MagicMock()
            mock_finder_class.return_value = mock_finder

            provider = PyPIProvider(session, index_urls)
            mock_finder.find_matches.return_value = []

            provider.find_matches(
                'requests', {'requests': [Requirement('requests>=2.27.0')]}, {}
            )

            # Should be called with the bare identifier, not a specifier string
            mock_finder.find_matches.assert_called_once_with(
                'requests', allow_yanked=True, allow_prereleases=True
            )

    def test_find_matches_incompatibility_filtering_applied_per_call(self):
        """Test that incompatibility filtering is applied on each call, not cached,
        so backtracking correctly excludes previously tried versions."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.SeriesPackageFinder') as mock_finder_class:
            mock_finder = MagicMock()
            mock_finder_class.return_value = mock_finder

            provider = PyPIProvider(session, index_urls)

            mock_package1 = MagicMock()
            mock_package1.version = '2.28.0'
            mock_package1.link.is_yanked = False
            mock_package2 = MagicMock()
            mock_package2.version = '2.27.0'
            mock_package2.link.is_yanked = False

            mock_finder.find_matches.return_value = [
                mock_package1,
                mock_package2,
            ]

            requirements = {'requests': [Requirement('requests>=2.27.0')]}

            # First call — no incompatibilities; both versions should be returned
            result1 = provider.find_matches('requests', requirements, {})
            self.assertEqual(2, len(result1))

            # Second call — 2.28.0 is now incompatible (simulates a backtrack)
            incompatibilities = {
                'requests': [Candidate('requests', Version('2.28.0'))]
            }
            result2 = provider.find_matches(
                'requests', requirements, incompatibilities
            )

            # unearth was only called once (cache hit on second call)
            mock_finder.find_matches.assert_called_once()
            # But the incompatibility filter still excluded 2.28.0
            self.assertEqual(1, len(result2))
            self.assertEqual('2.27.0', str(result2[0].version))

    def test_get_dependencies_cache_hit(self):
        """Test get_dependencies returns cached dependencies."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.SeriesPackageFinder'):
            provider = PyPIProvider(session, index_urls)

            candidate = Candidate('requests', Version('2.28.0'))
            cached_deps = [Requirement('urllib3>=1.26.0')]

            # Pre-populate cache
            provider._dependencies_cache[
                ('requests', Version('2.28.0'), frozenset())
            ] = cached_deps

            result = provider.get_dependencies(candidate)

            self.assertEqual(cached_deps, result)

    def test_get_dependencies_environment_built_once(self):
        """The marker environment is read once per provider, not per call."""
        with patch('proviso.resolver.SeriesPackageFinder'):
            with patch('proviso.resolver.default_environment') as de:
                de.return_value = {'sys_platform': 'linux'}
                provider = PyPIProvider(
                    MagicMock(), ['https://pypi.org/simple/']
                )
                de.assert_called_once_with()
                provider._get_requires_dist = MagicMock(
                    return_value=[Requirement('six; sys_platform == "linux"')]
                )
                for name in ('a', 'b', 'c'):
                    deps = provider.get_dependencies(
                        Candidate(name, Version('1.0'))
                    )
                    self.assertEqual(['six'], [d.name for d in deps])
                de.assert_called_once_with()

    def test_get_dependencies_no_package_found(self):
        """Test get_dependencies when no package is found."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.SeriesPackageFinder') as mock_finder_class:
            mock_finder = MagicMock()
            mock_finder_class.return_value = mock_finder

            provider = PyPIProvider(session, index_urls)

            # Mock no best match found
            mock_result = MagicMock()
            mock_result.best = None
            mock_finder.find_best_match.return_value = mock_result

            candidate = Candidate('nonexistent', Version('1.0.0'))

            result = provider.get_dependencies(candidate)

            self.assertEqual([], result)
            # Should be cached
            self.assertIn(
                ('nonexistent', Version('1.0.0'), frozenset()),
                provider._dependencies_cache,
            )

    def test_get_dependencies_with_metadata(self):
        """Test get_dependencies extracts dependencies from metadata."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.SeriesPackageFinder') as mock_finder_class:
            mock_finder = MagicMock()
            mock_finder_class.return_value = mock_finder

            provider = PyPIProvider(session, index_urls)

            # Mock package with metadata
            mock_package = MagicMock()
            mock_link = MagicMock()
            mock_dist_info_link = MagicMock()
            mock_dist_info_link.url = 'https://pypi.org/metadata'
            mock_link.dist_info_link = mock_dist_info_link
            mock_package.link = mock_link

            mock_result = MagicMock()
            mock_result.best = mock_package
            mock_finder.find_best_match.return_value = mock_result

            # Mock session response
            mock_response = MagicMock()
            mock_response.text = '''Metadata-Version: 2.1
Name: test-package
Version: 1.0.0
Requires-Dist: urllib3>=1.26.0
Requires-Dist: requests>=2.0.0
'''
            session.get.return_value = mock_response

            candidate = Candidate('test-package', Version('1.0.0'))

            result = provider.get_dependencies(candidate)

            # Should return dependencies
            self.assertEqual(2, len(result))
            self.assertTrue(any('urllib3' in str(r) for r in result))
            self.assertTrue(any('requests' in str(r) for r in result))

    def test_get_dependencies_invalid_index_requires_dist(self):
        """Invalid Requires-Dist in index metadata is MetadataUnavailableError."""
        session = MagicMock()

        with patch('proviso.resolver.SeriesPackageFinder'):
            provider = PyPIProvider(session, ['https://pypi.org/simple/'])

            mock_package = MagicMock()
            mock_package.link.dist_info_link.url = 'https://pypi.org/metadata'
            session.get.return_value.text = (
                'Metadata-Version: 2.1\nName: test-package\n'
                'Version: 1.0.0\nRequires-Dist: not a valid ((requirement\n'
            )

            candidate = Candidate(
                'test-package', Version('1.0.0'), package=mock_package
            )
            with self.assertRaises(MetadataUnavailableError) as ctx:
                provider.get_dependencies(candidate)

        self.assertIsInstance(ctx.exception.__cause__, InvalidMetadata)
        message = str(ctx.exception)
        self.assertIn('test-package==1.0.0', message)
        self.assertIn('invalid Requires-Dist', message)

    def test_get_dependencies_with_markers(self):
        """Test get_dependencies evaluates markers correctly."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.SeriesPackageFinder') as mock_finder_class:
            mock_finder = MagicMock()
            mock_finder_class.return_value = mock_finder

            provider = PyPIProvider(session, index_urls)

            mock_package = MagicMock()
            mock_link = MagicMock()
            mock_dist_info_link = MagicMock()
            mock_dist_info_link.url = 'https://pypi.org/metadata'
            mock_link.dist_info_link = mock_dist_info_link
            mock_package.link = mock_link

            mock_result = MagicMock()
            mock_result.best = mock_package
            mock_finder.find_best_match.return_value = mock_result

            mock_response = MagicMock()
            mock_response.text = '''Metadata-Version: 2.1
Name: test-package
Version: 1.0.0
Requires-Dist: urllib3>=1.26.0
Requires-Dist: pytest>=7.0.0; extra == "test"
'''
            session.get.return_value = mock_response

            candidate = Candidate('test-package', Version('1.0.0'))

            result = provider.get_dependencies(candidate)

            # Should only include urllib3 (no test extra)
            self.assertEqual(1, len(result))
            self.assertIn('urllib3', str(result[0]))

    def test_get_dependencies_no_dist_info_link(self):
        """Without a dist_info_link the metadata comes from the file."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.SeriesPackageFinder') as mock_finder_class:
            mock_finder = MagicMock()
            mock_finder_class.return_value = mock_finder

            provider = PyPIProvider(session, index_urls)

            mock_package = MagicMock()
            mock_link = MagicMock()
            mock_link.dist_info_link = None
            mock_package.link = mock_link

            mock_result = MagicMock()
            mock_result.best = mock_package
            mock_finder.find_best_match.return_value = mock_result

            candidate = Candidate('test-package', Version('1.0.0'))

            with patch.object(
                provider,
                '_get_file_requires_dist',
                return_value=[Requirement('six')],
            ) as mock_file:
                result = provider.get_dependencies(candidate)

            mock_file.assert_called_once_with(candidate, mock_link)
            self.assertEqual([Requirement('six')], result)
            session.get.assert_not_called()

    def test_get_dependencies_with_marker_default_environment(self):
        """Test get_dependencies with marker evaluation in default environment."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.SeriesPackageFinder') as mock_finder_class:
            mock_finder = MagicMock()
            mock_finder_class.return_value = mock_finder

            # No python_version means environment is None
            provider = PyPIProvider(session, index_urls, python_version=None)

            mock_package = MagicMock()
            mock_link = MagicMock()
            mock_dist_info_link = MagicMock()
            mock_dist_info_link.url = 'https://pypi.org/metadata'
            mock_link.dist_info_link = mock_dist_info_link
            mock_package.link = mock_link

            mock_result = MagicMock()
            mock_result.best = mock_package
            mock_finder.find_best_match.return_value = mock_result

            mock_response = MagicMock()
            # Use a marker that evaluates to True in default environment
            mock_response.text = '''Metadata-Version: 2.1
Name: test-package
Version: 1.0.0
Requires-Dist: urllib3>=1.26.0; python_version >= "3.8"
'''
            session.get.return_value = mock_response

            candidate = Candidate('test-package', Version('1.0.0'))

            result = provider.get_dependencies(candidate)

            # Should include urllib3 (marker evaluates to True)
            self.assertEqual(1, len(result))
            self.assertIn('urllib3', str(result[0]))

    def test_get_dependencies_with_marker_custom_environment(self):
        """Test get_dependencies with marker evaluation in custom environment."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.SeriesPackageFinder') as mock_finder_class:
            with patch('proviso.resolver.TargetPython'):
                with patch('proviso.resolver.default_environment') as mock_env:
                    mock_env.return_value = {}

                    mock_finder = MagicMock()
                    mock_finder_class.return_value = mock_finder

                    # With python_version, environment will be set
                    provider = PyPIProvider(
                        session, index_urls, python_version='3.9'
                    )

                    mock_package = MagicMock()
                    mock_link = MagicMock()
                    mock_dist_info_link = MagicMock()
                    mock_dist_info_link.url = 'https://pypi.org/metadata'
                    mock_link.dist_info_link = mock_dist_info_link
                    mock_package.link = mock_link

                    mock_result = MagicMock()
                    mock_result.best = mock_package
                    mock_finder.find_best_match.return_value = mock_result

                    mock_response = MagicMock()
                    # Use a marker that will be evaluated against custom environment
                    mock_response.text = '''Metadata-Version: 2.1
Name: test-package
Version: 1.0.0
Requires-Dist: urllib3>=1.26.0; python_version >= "3.9"
'''
                    session.get.return_value = mock_response

                    candidate = Candidate('test-package', Version('1.0.0'))

                    result = provider.get_dependencies(candidate)

                    # Should include urllib3 (marker evaluates to True for 3.9)
                    self.assertEqual(1, len(result))
                    self.assertIn('urllib3', str(result[0]))

    def test_get_dependencies_with_marker_false_custom_environment(self):
        """Test get_dependencies excludes dependencies when marker evaluates to False."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.SeriesPackageFinder') as mock_finder_class:
            with patch('proviso.resolver.TargetPython'):
                with patch('proviso.resolver.default_environment') as mock_env:
                    mock_env.return_value = {}

                    mock_finder = MagicMock()
                    mock_finder_class.return_value = mock_finder

                    # With python_version, environment will be set
                    provider = PyPIProvider(
                        session, index_urls, python_version='3.9'
                    )

                    mock_package = MagicMock()
                    mock_link = MagicMock()
                    mock_dist_info_link = MagicMock()
                    mock_dist_info_link.url = 'https://pypi.org/metadata'
                    mock_link.dist_info_link = mock_dist_info_link
                    mock_package.link = mock_link

                    mock_result = MagicMock()
                    mock_result.best = mock_package
                    mock_finder.find_best_match.return_value = mock_result

                    mock_response = MagicMock()
                    # Use a marker that will evaluate to False for 3.9
                    mock_response.text = '''Metadata-Version: 2.1
Name: test-package
Version: 1.0.0
Requires-Dist: urllib3>=1.26.0; python_version >= "3.10"
'''
                    session.get.return_value = mock_response

                    candidate = Candidate('test-package', Version('1.0.0'))

                    result = provider.get_dependencies(candidate)

                    # Should NOT include urllib3 (marker evaluates to False for 3.9)
                    self.assertEqual(0, len(result))

    def test_get_dependencies_http_error(self):
        """Test get_dependencies when HTTP request fails."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.SeriesPackageFinder') as mock_finder_class:
            mock_finder = MagicMock()
            mock_finder_class.return_value = mock_finder

            provider = PyPIProvider(session, index_urls)

            mock_package = MagicMock()
            mock_link = MagicMock()
            mock_dist_info_link = MagicMock()
            mock_dist_info_link.url = 'https://pypi.org/metadata'
            mock_link.dist_info_link = mock_dist_info_link
            mock_package.link = mock_link

            mock_result = MagicMock()
            mock_result.best = mock_package
            mock_finder.find_best_match.return_value = mock_result

            # Mock 404 response
            mock_response = MagicMock()
            mock_response.status_code = 404
            mock_response.text = 'Not Found'

            import httpx2

            def raise_for_status():
                if 400 <= mock_response.status_code < 600:
                    raise httpx2.HTTPStatusError(
                        message="Client Error",
                        request=MagicMock(),
                        response=mock_response,
                    )

            mock_response.raise_for_status.side_effect = raise_for_status
            session.get.return_value = mock_response

            candidate = Candidate('requests', Version('2.28.0'))

            with self.assertRaises(httpx2.HTTPStatusError):
                provider.get_dependencies(candidate)


class TestResolver(TestCase):
    def test_init_default_index_urls(self):
        """Test Resolver initialization with default index URLs."""
        with patch('proviso.resolver.CachingClient') as mock_client:
            with patch('proviso.resolver.MultiDomainBasicAuth') as mock_auth:
                resolver = Resolver()

                self.assertEqual(
                    ['https://pypi.org/simple/'], resolver.index_urls
                )
                mock_client.assert_called_once()
                mock_auth.assert_called_once_with(
                    index_urls=['https://pypi.org/simple/']
                )

    def test_init_custom_index_urls(self):
        """Test Resolver initialization with custom index URLs."""
        custom_urls = ['https://custom.pypi.org/simple/']

        with patch('proviso.resolver.CachingClient'):
            with patch('proviso.resolver.MultiDomainBasicAuth') as mock_auth:
                resolver = Resolver(index_urls=custom_urls)

                self.assertEqual(custom_urls, resolver.index_urls)
                mock_auth.assert_called_once_with(index_urls=custom_urls)

    def test_init_creates_caching_session(self):
        """Test that Resolver creates a CachingClient session."""
        with patch('proviso.resolver.CachingClient') as mock_client_class:
            with patch('proviso.resolver.MultiDomainBasicAuth'):
                mock_client_instance = MagicMock()
                mock_client_class.return_value = mock_client_instance

                resolver = Resolver()

                self.assertEqual(mock_client_instance, resolver._session)

    def test_init_with_provided_session(self):
        """Test that Resolver uses provided session."""
        with patch('proviso.resolver.MultiDomainBasicAuth'):
            mock_session = MagicMock()
            resolver = Resolver(session=mock_session)

            self.assertIs(mock_session, resolver._session)

    def test_resolve_returns_dict(self):
        """Test that resolve returns a dictionary."""
        with patch('proviso.resolver.CachingClient'):
            with patch('proviso.resolver.MultiDomainBasicAuth'):
                resolver = Resolver()

                # Mock the resolution process
                mock_result = MagicMock()
                mock_result.mapping = {
                    'requests': Candidate('requests', Version('2.28.0')),
                    'urllib3': Candidate('urllib3', Version('1.26.0')),
                }

                with patch(
                    'proviso.resolver.ResolveLibResolver'
                ) as mock_resolver_class:
                    mock_resolver_instance = MagicMock()
                    mock_resolver_instance.resolve.return_value = mock_result
                    mock_resolver_class.return_value = mock_resolver_instance

                    with patch('proviso.resolver.PyPIProvider'):
                        requirements = [Requirement('requests>=2.0.0')]
                        result = resolver.resolve(requirements)

                        # Verify result structure
                        self.assertIsInstance(result, dict)
                        self.assertIn('requests', result)
                        self.assertIn('urllib3', result)

                        self.assertEqual(
                            '2.28.0', result['requests']['version']
                        )
                        self.assertEqual('1.26.0', result['urllib3']['version'])

    def test_resolve_with_python_version(self):
        """Test resolve with specific Python version."""
        with patch('proviso.resolver.CachingClient'):
            with patch('proviso.resolver.MultiDomainBasicAuth'):
                resolver = Resolver()

                mock_result = MagicMock()
                mock_result.mapping = {}

                with patch(
                    'proviso.resolver.ResolveLibResolver'
                ) as mock_resolver_class:
                    mock_resolver_instance = MagicMock()
                    mock_resolver_instance.resolve.return_value = mock_result
                    mock_resolver_class.return_value = mock_resolver_instance

                    with patch(
                        'proviso.resolver.PyPIProvider'
                    ) as mock_provider:
                        requirements = [Requirement('requests>=2.0.0')]
                        resolver.resolve(requirements, python_version='3.9')

                        # Verify PyPIProvider was called with python_version
                        mock_provider.assert_called_once()
                        call_kwargs = mock_provider.call_args[1]
                        self.assertEqual('3.9', call_kwargs['python_version'])

    def test_resolve_with_exclude_newer_than(self):
        """Test resolve with exclude_newer_than parameter."""
        with patch('proviso.resolver.CachingClient'):
            with patch('proviso.resolver.MultiDomainBasicAuth'):
                resolver = Resolver()

                mock_result = MagicMock()
                mock_result.mapping = {}

                with patch(
                    'proviso.resolver.ResolveLibResolver'
                ) as mock_resolver_class:
                    mock_resolver_instance = MagicMock()
                    mock_resolver_instance.resolve.return_value = mock_result
                    mock_resolver_class.return_value = mock_resolver_instance

                    with patch(
                        'proviso.resolver.PyPIProvider'
                    ) as mock_provider:
                        requirements = [Requirement('requests>=2.0.0')]
                        exclude_newer_than = datetime(
                            2024, 1, 1, tzinfo=timezone.utc
                        )
                        resolver.resolve(
                            requirements, exclude_newer_than=exclude_newer_than
                        )

                        # Verify PyPIProvider was called with exclude_newer_than
                        mock_provider.assert_called_once()
                        call_kwargs = mock_provider.call_args[1]
                        self.assertEqual(
                            exclude_newer_than,
                            call_kwargs['exclude_newer_than'],
                        )


class TestMaxRounds(TestCase):
    def test_round_limit_matches_pip(self):
        """The resolver is given pip's round limit."""
        self.assertEqual(200000, MAX_ROUNDS)
        with patch('proviso.resolver.CachingClient'):
            resolver = Resolver()
        with patch('proviso.resolver.ResolveLibResolver') as mock_class:
            mock_class.return_value.resolve.return_value.mapping = {}
            with patch('proviso.resolver.PyPIProvider'):
                resolver.resolve([Requirement('foo')])
        mock_class.return_value.resolve.assert_called_once()
        self.assertEqual(
            MAX_ROUNDS,
            mock_class.return_value.resolve.call_args.kwargs['max_rounds'],
        )


class TestPyPIProviderExcludeNewerThan(TestCase):
    def test_init_with_exclude_newer_than(self):
        """Test PyPIProvider initialization with exclude_newer_than."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']
        exclude_newer_than = datetime(2024, 1, 1, tzinfo=timezone.utc)

        with patch('proviso.resolver.SeriesPackageFinder') as mock_finder:
            provider = PyPIProvider(
                session, index_urls, exclude_newer_than=exclude_newer_than
            )

            # The provider applies the cooldown to each release, so the
            # finder, which would apply it to each file, doesn't get it
            mock_finder.assert_called_once()
            self.assertNotIn('exclude_newer_than', mock_finder.call_args[1])
            self.assertEqual(exclude_newer_than, provider.exclude_newer_than)

    def test_init_without_exclude_newer_than(self):
        """Test PyPIProvider initialization without exclude_newer_than."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.SeriesPackageFinder'):
            provider = PyPIProvider(session, index_urls)

            self.assertIsNone(provider.exclude_newer_than)


class TestPyPIProviderExtrasBug(TestCase):
    def test_get_dependencies_with_extras_repro(self):
        """Test that get_dependencies correctly handles extras."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.SeriesPackageFinder') as mock_finder_class:
            mock_finder = MagicMock()
            mock_finder_class.return_value = mock_finder

            provider = PyPIProvider(session, index_urls)

            mock_package = MagicMock()
            mock_link = MagicMock()
            mock_dist_info_link = MagicMock()
            mock_dist_info_link.url = 'https://pypi.org/metadata'
            mock_link.dist_info_link = mock_dist_info_link
            mock_package.link = mock_link

            mock_result = MagicMock()
            mock_result.best = mock_package
            mock_finder.find_best_match.return_value = mock_result

            mock_response = MagicMock()
            # "requests[security]" depends on "cryptography"
            # "requests" does not (by default)
            mock_response.text = '''Metadata-Version: 2.1
Name: requests
Version: 2.28.0
Requires-Dist: urllib3<1.27,>=1.21.1
Requires-Dist: certifi>=2017.4.17
Requires-Dist: cryptography>=1.3.4; extra == 'security'
'''
            session.get.return_value = mock_response

            # Case 1: Candidate with NO extras
            candidate_no_extra = Candidate('requests', Version('2.28.0'))
            deps_no_extra = provider.get_dependencies(candidate_no_extra)

            # Should have urllib3 and certifi, but NOT cryptography
            deps_names_no_extra = [d.name for d in deps_no_extra]
            self.assertIn('urllib3', deps_names_no_extra)
            self.assertIn('certifi', deps_names_no_extra)
            self.assertNotIn('cryptography', deps_names_no_extra)

            # Case 2: Candidate WITH 'security' extra
            candidate_security = Candidate(
                'requests', Version('2.28.0'), extras=frozenset(['security'])
            )

            deps_security = provider.get_dependencies(candidate_security)
            deps_names_security = [d.name for d in deps_security]

            self.assertIn(
                'cryptography',
                deps_names_security,
                "cryptography should be present when security extra is requested",
            )


class TestPyPIProviderCoverage(TestCase):
    def test_get_dependencies_extras_no_match(self):
        """Test get_dependencies when extras are present but don't match marker."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.SeriesPackageFinder') as mock_finder_class:
            mock_finder = MagicMock()
            mock_finder_class.return_value = mock_finder

            provider = PyPIProvider(session, index_urls)

            mock_package = MagicMock()
            mock_link = MagicMock()
            mock_dist_info_link = MagicMock()
            mock_dist_info_link.url = 'https://pypi.org/metadata'
            mock_link.dist_info_link = mock_dist_info_link
            mock_package.link = mock_link

            mock_result = MagicMock()
            mock_result.best = mock_package
            mock_finder.find_best_match.return_value = mock_result

            mock_response = MagicMock()
            # Requirement depends on 'other' extra
            mock_response.text = '''Metadata-Version: 2.1
Name: requests
Version: 2.28.0
Requires-Dist: cryptography>=1.3.4; extra == 'other'
'''
            session.get.return_value = mock_response

            # Candidate has 'security' extra, which does NOT match 'other'
            candidate = Candidate(
                'requests', Version('2.28.0'), extras=frozenset(['security'])
            )

            deps = provider.get_dependencies(candidate)

            # Should NOT include cryptography
            deps_names = [d.name for d in deps]
            self.assertNotIn('cryptography', deps_names)


class TestNarrowRequirementSelection(TestCase):
    """Tests for PyPIProvider.narrow_requirement_selection."""

    def _make_provider(self):
        with patch('proviso.resolver.SeriesPackageFinder'):
            return PyPIProvider(MagicMock(), ['https://pypi.org/simple/'])

    def test_no_backtrack_causes_returns_all(self):
        """With no backtrack causes, all identifiers are returned unchanged."""
        provider = self._make_provider()
        identifiers = ['requests', 'urllib3', 'certifi']
        result = list(
            provider.narrow_requirement_selection(identifiers, {}, {}, {}, [])
        )
        self.assertEqual(identifiers, result)

    def test_backtrack_cause_returned_first(self):
        """Identifiers that are a backtrack cause are returned ahead of others."""
        provider = self._make_provider()

        cause_req = Requirement('urllib3>=1.26')
        cause = _RequirementInfo(requirement=cause_req, parent=None)

        result = list(
            provider.narrow_requirement_selection(
                ['requests', 'urllib3', 'certifi'], {}, {}, {}, [cause]
            )
        )
        # Only the cause identifier should be returned
        self.assertEqual(['urllib3'], result)

    def test_parent_name_also_collected(self):
        """The parent Candidate's name is also treated as a backtrack cause."""
        provider = self._make_provider()

        parent = Candidate('requests', Version('2.28.0'))
        cause_req = Requirement('urllib3>=1.26')
        cause = _RequirementInfo(requirement=cause_req, parent=parent)

        result = list(
            provider.narrow_requirement_selection(
                ['requests', 'urllib3', 'certifi'], {}, {}, {}, [cause]
            )
        )
        # Both the requirement name and the parent name are causes
        self.assertIn('urllib3', result)
        self.assertIn('requests', result)
        self.assertNotIn('certifi', result)

    def test_conflict_count_increments(self):
        """Unresolved conflict names increment _conflict_counts."""
        provider = self._make_provider()

        cause = _RequirementInfo(
            requirement=Requirement('urllib3>=1.26'), parent=None
        )
        provider.narrow_requirement_selection(['urllib3'], {}, {}, {}, [cause])
        self.assertEqual(1, provider._conflict_counts['urllib3'])

    def test_resolved_name_not_incremented(self):
        """Conflict counts are not incremented for already-resolved packages."""
        provider = self._make_provider()

        cause = _RequirementInfo(
            requirement=Requirement('urllib3>=1.26'), parent=None
        )
        resolutions = {'urllib3': Candidate('urllib3', Version('1.26.0'))}
        provider.narrow_requirement_selection(
            ['urllib3'], resolutions, {}, {}, [cause]
        )
        self.assertEqual(0, provider._conflict_counts['urllib3'])

    def test_promoted_after_threshold(self):
        """A name is added to _conflict_promoted after _CONFLICT_PRIORITY_THRESHOLD conflicts."""
        provider = self._make_provider()

        cause = _RequirementInfo(
            requirement=Requirement('urllib3>=1.26'), parent=None
        )
        for _ in range(_CONFLICT_PRIORITY_THRESHOLD):
            provider.narrow_requirement_selection(
                ['urllib3'], {}, {}, {}, [cause]
            )

        self.assertIn('urllib3', provider._conflict_promoted)

    def test_promoted_returned_when_no_current_causes(self):
        """Promoted identifiers are returned when there are no current backtrack causes."""
        provider = self._make_provider()
        provider._conflict_promoted.add('urllib3')

        result = list(
            provider.narrow_requirement_selection(
                ['requests', 'urllib3', 'certifi'], {}, {}, {}, []
            )
        )
        self.assertEqual(['urllib3'], result)

    def test_current_causes_take_precedence_over_promoted(self):
        """Current backtrack causes are returned ahead of promoted identifiers."""
        provider = self._make_provider()
        provider._conflict_promoted.add('urllib3')

        cause = _RequirementInfo(
            requirement=Requirement('certifi>=2017'), parent=None
        )
        result = list(
            provider.narrow_requirement_selection(
                ['requests', 'urllib3', 'certifi'], {}, {}, {}, [cause]
            )
        )
        # Current cause wins over promoted
        self.assertEqual(['certifi'], result)


class TestFindMatchesPrerelease(TestCase):
    """Tests for pip-aligned prerelease handling in find_matches."""

    def _make_provider_with_packages(self, versions, allow_prereleases=False):
        """Helper: creates a provider whose finder returns packages at given versions."""
        session = MagicMock()
        with patch('proviso.resolver.SeriesPackageFinder') as mock_finder_class:
            mock_finder = MagicMock()
            mock_finder_class.return_value = mock_finder

            provider = PyPIProvider(
                session,
                ['https://pypi.org/simple/'],
                allow_prereleases=allow_prereleases,
            )

            packages = []
            for v in versions:
                pkg = MagicMock()
                pkg.version = v
                pkg.link.is_yanked = False
                packages.append(pkg)

            mock_finder.find_matches.return_value = packages
            return provider

    def test_prerelease_excluded_by_default(self):
        """Pre-release versions are excluded when no specifier explicitly allows them."""
        provider = self._make_provider_with_packages(
            ['2.28.0', '2.29.0a1', '2.27.0']
        )
        result = provider.find_matches(
            'requests', {'requests': [Requirement('requests>=2.0.0')]}, {}
        )
        versions = [str(c.version) for c in result]
        self.assertIn('2.28.0', versions)
        self.assertIn('2.27.0', versions)
        self.assertNotIn('2.29.0a1', versions)

    def test_prerelease_included_when_specifier_allows(self):
        """Pre-release versions are included when a specifier references a pre-release."""
        provider = self._make_provider_with_packages(
            ['2.28.0', '2.29.0a1', '2.27.0']
        )
        # Specifier explicitly references a pre-release → prereleases=True
        result = provider.find_matches(
            'requests', {'requests': [Requirement('requests>=2.29.0a1')]}, {}
        )
        versions = [str(c.version) for c in result]
        self.assertIn('2.29.0a1', versions)

    def test_stable_versions_always_included(self):
        """Non-pre-release versions are always included regardless of the flag."""
        provider = self._make_provider_with_packages(['2.28.0', '2.27.0'])
        result = provider.find_matches(
            'requests', {'requests': [Requirement('requests>=2.27.0')]}, {}
        )
        versions = [str(c.version) for c in result]
        self.assertIn('2.28.0', versions)
        self.assertIn('2.27.0', versions)

    def test_prerelease_excluded_for_lower_bound(self):
        """A newer pre-release isn't picked for a plain lower bound.

        packaging 26 changed ``Version('10.0.0b3') in SpecifierSet('>=5.11.5')``
        to return True, which led to pinning isort 10.0.0b3.  The default must
        not depend on that.
        """
        provider = self._make_provider_with_packages(
            ['10.0.0b3', '9.0.2', '5.11.5']
        )
        result = provider.find_matches(
            'isort', {'isort': [Requirement('isort>=5.11.5')]}, {}
        )
        self.assertEqual(['9.0.2', '5.11.5'], [str(c.version) for c in result])

    def test_prerelease_excluded_with_no_specifier(self):
        """A bare requirement doesn't pick up pre-releases."""
        provider = self._make_provider_with_packages(['2.0.0b1', '1.0.0'])
        result = provider.find_matches('foo', {'foo': [Requirement('foo')]}, {})
        self.assertEqual(['1.0.0'], [str(c.version) for c in result])

    def test_prerelease_included_with_allow_prereleases(self):
        """allow_prereleases (--pre) includes pre-releases for any specifier."""
        provider = self._make_provider_with_packages(
            ['10.0.0b3', '9.0.2'], allow_prereleases=True
        )
        result = provider.find_matches(
            'isort', {'isort': [Requirement('isort>=5.11.5')]}, {}
        )
        self.assertEqual(
            ['10.0.0b3', '9.0.2'], [str(c.version) for c in result]
        )

    def test_allow_prereleases_still_applies_specifiers(self):
        """allow_prereleases doesn't bypass upper bounds."""
        provider = self._make_provider_with_packages(
            ['10.0.0b3', '9.1.0rc1', '9.0.2'], allow_prereleases=True
        )
        result = provider.find_matches(
            'isort', {'isort': [Requirement('isort<10')]}, {}
        )
        self.assertEqual(
            ['9.1.0rc1', '9.0.2'], [str(c.version) for c in result]
        )

    def test_prerelease_allowed_when_any_requirement_names_one(self):
        """A pre-release specifier on one requirement allows pre-releases
        that also satisfy the other requirements for the same package."""
        provider = self._make_provider_with_packages(
            ['2.1.0b1', '2.0.0', '1.0.0']
        )
        result = provider.find_matches(
            'foo',
            {'foo': [Requirement('foo>=1.0'), Requirement('foo>=2.1.0b1')]},
            {},
        )
        self.assertEqual(['2.1.0b1'], [str(c.version) for c in result])

    def test_is_satisfied_by_prerelease_candidate(self):
        """is_satisfied_by accepts a pre-release candidate that matches the
        specifier; the pre-release policy is applied in find_matches."""
        provider = self._make_provider_with_packages([])
        candidate = Candidate('foo', Version('2.1.0b1'))
        self.assertTrue(
            provider.is_satisfied_by(Requirement('foo>=1.0'), candidate)
        )
        self.assertFalse(
            provider.is_satisfied_by(Requirement('foo<2'), candidate)
        )

    def test_resolve_passes_allow_prereleases_to_provider(self):
        """Resolver.resolve threads allow_prereleases into the provider."""
        resolver = Resolver(session=MagicMock())
        for kwargs, expected in (
            ({}, False),
            ({'allow_prereleases': True}, True),
        ):
            with patch('proviso.resolver.PyPIProvider') as mock_provider:
                with patch('proviso.resolver.ResolveLibResolver') as mock_rl:
                    mock_rl.return_value.resolve.return_value.mapping = {}
                    resolver.resolve([Requirement('foo')], **kwargs)
                self.assertEqual(
                    expected,
                    mock_provider.call_args.kwargs['allow_prereleases'],
                )


class TestPrereleaseWithRealFinder(TestCase):
    """Pre-release handling with a real SeriesPackageFinder.

    The finder reads a local find-links directory instead of being mocked, so
    unearth's own filtering runs.  These tests catch unearth dropping
    pre-releases before proviso's policy is applied, which happens with
    packaging < 26 if the finder isn't asked for every version.
    """

    VERSIONS = ('0.1', '1.0', '2.0b1')

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        for version in self.VERSIONS:
            filename = f'foo-{version}-py3-none-any.whl'
            with ZipFile(join(self._tmp.name, filename), 'w') as zf:
                zf.writestr(
                    f'foo-{version}.dist-info/METADATA',
                    f'Metadata-Version: 2.1\nName: foo\nVersion: {version}\n',
                )
        finder = partial(SeriesPackageFinder, find_links=[self._tmp.name])
        patcher = patch('proviso.resolver.SeriesPackageFinder', finder)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _versions(self, spec, allow_prereleases=False):
        provider = PyPIProvider(
            None, [], python_version='3.12', allow_prereleases=allow_prereleases
        )
        result = provider.find_matches('foo', {'foo': [Requirement(spec)]}, {})
        return [str(c.version) for c in result]

    def test_exact_prerelease_pin_without_pre(self):
        """Pinning a pre-release with == selects it without --pre."""
        self.assertEqual(['2.0b1'], self._versions('foo==2.0b1'))

    def test_prerelease_lower_bound_without_pre(self):
        """A lower bound that names a pre-release allows pre-releases."""
        self.assertEqual(['2.0b1'], self._versions('foo>=2.0b1'))

    def test_plain_specifier_skips_prerelease(self):
        """A plain specifier skips pre-releases by default."""
        self.assertEqual(['1.0'], self._versions('foo>=0.5'))

    def test_plain_specifier_with_pre(self):
        """--pre includes pre-releases for a plain specifier."""
        self.assertEqual(
            ['2.0b1', '1.0'], self._versions('foo>=0.5', allow_prereleases=True)
        )

    def test_resolve_exact_prerelease_pin_without_pre(self):
        """Resolver.resolve pins a requested pre-release without --pre."""
        resolver = Resolver(session=MagicMock())
        result = resolver.resolve(
            [Requirement('foo==2.0b1')], python_version='3.12'
        )
        self.assertEqual({'foo': {'version': '2.0b1'}}, result)


class TestGetDependenciesPackageFastPath(TestCase):
    """Tests for the get_dependencies fast path that uses candidate.package."""

    def test_uses_candidate_package_skips_find_best_match(self):
        """get_dependencies uses candidate.package directly, avoiding find_best_match."""
        session = MagicMock()

        with patch('proviso.resolver.SeriesPackageFinder') as mock_finder_class:
            mock_finder = MagicMock()
            mock_finder_class.return_value = mock_finder

            provider = PyPIProvider(session, ['https://pypi.org/simple/'])

            # Set up a package object as if carried from find_matches
            mock_package = MagicMock()
            mock_dist_info_link = MagicMock()
            mock_dist_info_link.url = 'https://pypi.org/metadata/requests'
            mock_package.link.dist_info_link = mock_dist_info_link

            mock_response = MagicMock()
            mock_response.text = '''Metadata-Version: 2.1
Name: requests
Version: 2.28.0
Requires-Dist: urllib3>=1.21.1
'''
            session.get.return_value = mock_response

            # Candidate WITH package set — this is the fast path
            candidate = Candidate(
                'requests', Version('2.28.0'), package=mock_package
            )
            result = provider.get_dependencies(candidate)

            # find_best_match must NOT have been called
            mock_finder.find_best_match.assert_not_called()
            # The dependency was still resolved correctly
            self.assertEqual(1, len(result))
            self.assertIn('urllib3', str(result[0]))

    def test_falls_back_to_find_best_match_when_no_package(self):
        """get_dependencies falls back to find_best_match when candidate.package is None."""
        session = MagicMock()

        with patch('proviso.resolver.SeriesPackageFinder') as mock_finder_class:
            mock_finder = MagicMock()
            mock_finder_class.return_value = mock_finder

            provider = PyPIProvider(session, ['https://pypi.org/simple/'])

            mock_package = MagicMock()
            mock_dist_info_link = MagicMock()
            mock_dist_info_link.url = 'https://pypi.org/metadata/requests'
            mock_package.link.dist_info_link = mock_dist_info_link

            mock_result = MagicMock()
            mock_result.best = mock_package
            mock_finder.find_best_match.return_value = mock_result

            mock_response = MagicMock()
            mock_response.text = '''Metadata-Version: 2.1
Name: requests
Version: 2.28.0
Requires-Dist: urllib3>=1.21.1
'''
            session.get.return_value = mock_response

            # Candidate WITHOUT package set — fallback path
            candidate = Candidate('requests', Version('2.28.0'))
            result = provider.get_dependencies(candidate)

            mock_finder.find_best_match.assert_called_once_with(
                'requests==2.28.0'
            )
            self.assertEqual(1, len(result))


class TestResolverUserRequested(TestCase):
    """Tests for user_requested wiring in Resolver.resolve."""

    def test_user_requested_passed_to_provider(self):
        """Resolver.resolve builds a user_requested dict and passes it to PyPIProvider."""
        with patch('proviso.resolver.CachingClient'):
            with patch('proviso.resolver.MultiDomainBasicAuth'):
                resolver = Resolver()

                mock_result = MagicMock()
                mock_result.mapping = {}

                with patch(
                    'proviso.resolver.ResolveLibResolver'
                ) as mock_resolver_class:
                    mock_resolver_instance = MagicMock()
                    mock_resolver_instance.resolve.return_value = mock_result
                    mock_resolver_class.return_value = mock_resolver_instance

                    with patch(
                        'proviso.resolver.PyPIProvider'
                    ) as mock_provider_class:
                        requirements = [
                            Requirement('requests>=2.0.0'),
                            Requirement('urllib3>=1.26.0'),
                        ]
                        resolver.resolve(requirements)

                        call_kwargs = mock_provider_class.call_args[1]
                        user_requested = call_kwargs['user_requested']

                        # Both top-level packages should be present with correct order
                        self.assertIn('requests', user_requested)
                        self.assertIn('urllib3', user_requested)
                        self.assertEqual(0, user_requested['requests'])
                        self.assertEqual(1, user_requested['urllib3'])


class TestRootRequirementMarkers(TestCase):
    """Root requirement markers are evaluated per target Python."""

    REQS = [
        Requirement('a'),
        Requirement("tomli; python_version < '3.11'"),
        Requirement("newpy; python_version >= '3.12'"),
        Requirement("win; sys_platform == 'win32'"),
        Requirement("d; extra == 'dev'"),
        Requirement("e; extra == 'dev' and python_version < '3.11'"),
        Requirement("f; extra == 'test'"),
        Requirement("g; python_version < '3.11' or extra == 'dev'"),
    ]

    def _names(self, python_version, extras=()):
        return [
            r.name
            for r in filter_requirements(
                self.REQS, python_version=python_version, extras=extras
            )
        ]

    def test_no_extras(self):
        with patch('proviso.resolver.default_environment') as de:
            de.return_value = {'sys_platform': 'linux'}
            self.assertEqual(['a', 'tomli', 'g'], self._names('3.10'))
            self.assertEqual(['a', 'newpy'], self._names('3.12'))

    def test_host_python_does_not_matter(self):
        host = {'python_version': '3.14', 'sys_platform': 'linux'}
        with patch('proviso.resolver.default_environment') as de:
            de.side_effect = lambda: dict(host)
            self.assertIn('tomli', self._names('3.10'))
            self.assertNotIn('tomli', self._names('3.14'))

    def test_extras(self):
        with patch('proviso.resolver.default_environment') as de:
            de.return_value = {'sys_platform': 'linux'}
            self.assertEqual(
                ['a', 'tomli', 'd', 'e', 'g'], self._names('3.10', ['dev'])
            )
            self.assertEqual(
                ['a', 'newpy', 'd', 'g'], self._names('3.12', ['dev'])
            )

    def test_included_once_with_many_extras(self):
        with patch('proviso.resolver.default_environment') as de:
            de.return_value = {'sys_platform': 'linux'}
            names = self._names('3.10', ['dev', 'test', 'other'])
            self.assertEqual(['a', 'tomli', 'd', 'e', 'f', 'g'], names)

    def test_python_version_none_uses_host(self):
        with patch('proviso.resolver.default_environment') as de:
            de.return_value = {
                'python_version': '3.10',
                'sys_platform': 'win32',
            }
            names = [
                r.name for r in filter_requirements(self.REQS, extras=['dev'])
            ]
            self.assertEqual(['a', 'tomli', 'win', 'd', 'e', 'g'], names)

    def _resolve(self, **kwargs):
        with patch('proviso.resolver.CachingClient'):
            with patch('proviso.resolver.MultiDomainBasicAuth'):
                resolver = Resolver()
        mock_result = MagicMock()
        mock_result.mapping = {}
        with patch('proviso.resolver.ResolveLibResolver') as rl:
            rl.return_value.resolve.return_value = mock_result
            with patch('proviso.resolver.PyPIProvider') as provider:
                with patch('proviso.resolver.default_environment') as de:
                    de.return_value = {'sys_platform': 'linux'}
                    result = resolver.resolve(self.REQS, **kwargs)
        return result, rl, provider

    def test_resolve_passes_filtered_requirements(self):
        _, rl, provider = self._resolve(python_version='3.10', extras=['dev'])
        passed = rl.return_value.resolve.call_args.args[0]
        names = [r.name for r in passed]
        self.assertEqual(['a', 'tomli', 'd', 'e', 'g'], names)
        user_requested = provider.call_args.kwargs['user_requested']
        self.assertEqual(
            {'a': 0, 'tomli': 1, 'd': 2, 'e': 3, 'g': 4}, user_requested
        )

    def test_resolve_all_filtered_out(self):
        with patch('proviso.resolver.CachingClient'):
            with patch('proviso.resolver.MultiDomainBasicAuth'):
                resolver = Resolver()
        with patch('proviso.resolver.ResolveLibResolver') as rl:
            with patch('proviso.resolver.PyPIProvider'):
                result = resolver.resolve(
                    [Requirement("x; python_version < '3.0'")],
                    python_version='3.12',
                )
        self.assertEqual({}, result)
        rl.return_value.resolve.assert_not_called()


class TestExtras(TestCase):
    """Tests for resolving requirements that request extras."""

    # Requires-Dist metadata for the fake index used by _make_provider
    METADATA = {
        'hishel': (
            'msgpack>=1.0',
            'httpx>=0.28; extra == "httpx"',
            'anysqlite>=0.0.5; extra == "async"',
        ),
        'httpx': ('anyio',),
        'anyio': (),
        'anysqlite': (),
        'msgpack': (),
        'app': ('hishel[httpx]', 'hishel>=1.0'),
    }
    VERSIONS = {
        'hishel': ('1.0.0', '2.0.0'),
        'httpx': ('0.28.1',),
        'anyio': ('4.16.0',),
        'anysqlite': ('0.0.5',),
        'msgpack': ('1.2.4',),
        'app': ('1.0.0',),
    }

    def _make_package(self, name, version):
        package = MagicMock()
        package.version = version
        package.link.is_yanked = False
        package.link.dist_info_link.url = f'https://pypi/{name}/{version}'
        return package

    def _make_provider(self):
        session = MagicMock()

        def get(url):
            name = url.split('/')[-2]
            lines = [
                'Metadata-Version: 2.1',
                f'Name: {name}',
                f'Version: {url.split("/")[-1]}',
            ]
            lines.extend(f'Requires-Dist: {req}' for req in self.METADATA[name])
            response = MagicMock()
            response.text = '\n'.join(lines) + '\n'
            return response

        session.get.side_effect = get

        with patch('proviso.resolver.SeriesPackageFinder') as mock_finder_class:
            mock_finder = MagicMock()
            mock_finder.find_matches.side_effect = lambda name, **kwargs: [
                self._make_package(name, v) for v in self.VERSIONS[name]
            ]
            mock_finder_class.return_value = mock_finder
            provider = PyPIProvider(
                session, ['https://pypi.org/simple/'], python_version='3.13'
            )
        return provider

    def test_make_identifier(self):
        self.assertEqual('hishel', make_identifier('Hishel'))
        self.assertEqual('hishel', make_identifier('hishel', frozenset()))
        self.assertEqual(
            'hishel[async,httpx]', make_identifier('Hishel', {'HTTPX', 'async'})
        )

    def test_split_identifier(self):
        self.assertEqual(('hishel', frozenset()), split_identifier('hishel'))
        self.assertEqual(
            ('hishel', frozenset(['async', 'httpx'])),
            split_identifier('hishel[async,httpx]'),
        )

    def test_candidate_canonicalizes_extras(self):
        candidate = Candidate('pkg', Version('1.0'), extras=['Foo_Bar'])
        self.assertEqual(frozenset(['foo-bar']), candidate.extras)

    def test_identify_with_extras(self):
        provider = self._make_provider()
        self.assertEqual(
            'hishel[httpx]', provider.identify(Requirement('Hishel[HTTPX]'))
        )
        self.assertEqual(
            'hishel[httpx]',
            provider.identify(
                Candidate('hishel', Version('2.0.0'), extras={'httpx'})
            ),
        )

    def test_find_matches_extras_identifier(self):
        provider = self._make_provider()
        result = provider.find_matches(
            'hishel[httpx]',
            {'hishel[httpx]': [Requirement('hishel[httpx]')]},
            {},
        )
        self.assertEqual(
            [
                Candidate('hishel', Version('2.0.0'), extras={'httpx'}),
                Candidate('hishel', Version('1.0.0'), extras={'httpx'}),
            ],
            result,
        )

    def test_find_matches_extras_honors_base_constraints(self):
        provider = self._make_provider()
        result = provider.find_matches(
            'hishel[httpx]',
            {
                'hishel[httpx]': [Requirement('hishel[httpx]')],
                'hishel': [Requirement('hishel<2')],
            },
            {},
        )
        self.assertEqual([Version('1.0.0')], [c.version for c in result])

        result = provider.find_matches(
            'hishel[httpx]',
            {'hishel[httpx]': [Requirement('hishel[httpx]')]},
            {'hishel': [Candidate('hishel', Version('2.0.0'))]},
        )
        self.assertEqual([Version('1.0.0')], [c.version for c in result])

    def test_find_matches_cache_shared_with_base(self):
        provider = self._make_provider()
        provider.find_matches('hishel', {'hishel': [Requirement('hishel')]}, {})
        provider.find_matches(
            'hishel[httpx]',
            {'hishel[httpx]': [Requirement('hishel[httpx]')]},
            {},
        )
        provider.finder.find_matches.assert_called_once_with(
            'hishel', allow_yanked=True, allow_prereleases=True
        )

    def test_get_dependencies_extras_candidate(self):
        provider = self._make_provider()
        package = self._make_package('hishel', '2.0.0')

        base = Candidate('hishel', Version('2.0.0'), package=package)
        self.assertEqual(
            ['msgpack>=1.0'], [str(d) for d in provider.get_dependencies(base)]
        )

        extras = Candidate(
            'hishel', Version('2.0.0'), extras={'httpx'}, package=package
        )
        # Pins the base package and adds only the extra's dependencies
        self.assertEqual(
            ['hishel==2.0.0', 'httpx>=0.28; extra == "httpx"'],
            [str(d) for d in provider.get_dependencies(extras)],
        )

        # Metadata is fetched once for both candidates
        provider.session.get.assert_called_once_with(
            'https://pypi/hishel/2.0.0'
        )

    def test_narrow_requirement_selection_with_extras(self):
        provider = self._make_provider()
        parent = Candidate('app', Version('1.0.0'), extras={'cache'})
        cause = _RequirementInfo(
            requirement=Requirement('hishel[httpx]'), parent=parent
        )
        result = list(
            provider.narrow_requirement_selection(
                ['hishel', 'hishel[httpx]', 'app[cache]', 'msgpack'],
                {},
                {},
                {},
                [cause],
            )
        )
        self.assertEqual(['hishel[httpx]', 'app[cache]'], result)

    def test_resolve_with_extras(self):
        provider = self._make_provider()
        with patch('proviso.resolver.PyPIProvider', return_value=provider):
            resolver = Resolver(session=MagicMock())
            result = resolver.resolve([Requirement('app')])

        self.assertEqual(
            {
                'app': {'version': '1.0.0'},
                'hishel': {'version': '2.0.0'},
                'httpx': {'version': '0.28.1'},
                'anyio': {'version': '4.16.0'},
                'msgpack': {'version': '1.2.4'},
            },
            result,
        )

    def test_resolve_extras_follow_base_constraints(self):
        provider = self._make_provider()
        with patch('proviso.resolver.PyPIProvider', return_value=provider):
            resolver = Resolver(session=MagicMock())
            result = resolver.resolve(
                [Requirement('hishel[httpx,async]'), Requirement('hishel<2')]
            )

        self.assertEqual({'version': '1.0.0'}, result['hishel'])
        self.assertIn('anysqlite', result)
        self.assertIn('httpx', result)

    def test_resolve_user_requested_includes_base(self):
        with patch('proviso.resolver.PyPIProvider') as mock_provider_class:
            with patch('proviso.resolver.ResolveLibResolver'):
                Resolver(session=MagicMock()).resolve(
                    [Requirement('requests'), Requirement('hishel[httpx]')]
                )
        self.assertEqual(
            {'requests': 0, 'hishel[httpx]': 1, 'hishel': 1},
            mock_provider_class.call_args.kwargs['user_requested'],
        )


class TestFileMetadata(TestCase):
    """Metadata read from a downloaded file when the index serves none.

    Files are local file:// links, so unearth's real download_and_unpack
    runs without touching the network.  The build backend is mocked.
    """

    METADATA = '''Metadata-Version: {version}
Name: foo
Version: 1.0
{extra}Requires-Dist: six
Requires-Dist: pytest; extra == "test"
'''

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        patcher = patch('proviso.resolver.build_sdist_metadata')
        self.mock_build = patcher.start()
        self.addCleanup(patcher.stop)
        self.mock_build.return_value = Metadata.from_raw(
            {'requires_dist': ['built-dep']}, validate=False
        )

    def _metadata(self, version='2.2', extra=''):
        return self.METADATA.format(version=version, extra=extra)

    def _wheel(self, metadata=None, contents=None):
        path = self.dir / 'foo-1.0-py3-none-any.whl'
        if contents is not None:
            path.write_bytes(contents)
            return path
        with ZipFile(path, 'w') as zf:
            zf.writestr('foo/__init__.py', '')
            if metadata is not None:
                zf.writestr('foo-1.0.dist-info/METADATA', metadata)
        return path

    def _sdist(self, pkg_info=None):
        path = self.dir / 'foo-1.0.tar.gz'
        files = {'foo-1.0/setup.py': b''}
        if pkg_info is not None:
            files['foo-1.0/PKG-INFO'] = (
                pkg_info.encode() if isinstance(pkg_info, str) else pkg_info
            )
        with tarfile.open(path, 'w:gz') as tf:
            for name, data in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, BytesIO(data))
        return path

    def _provider(self, cache=None, build_sdists=True):
        return PyPIProvider(
            None,
            [],
            python_version='3.12',
            file_requires_dist_cache=cache,
            build_sdists=build_sdists,
        )

    def _candidate(self, path):
        package = MagicMock()
        package.link = Link.from_path(path)
        return Candidate('foo', Version('1.0'), package=package)

    def _dependencies(self, path, provider=None):
        provider = provider or self._provider()
        return provider.get_dependencies(self._candidate(path))

    def test_wheel(self):
        path = self._wheel(self._metadata(version='2.1'))
        self.assertEqual([Requirement('six')], self._dependencies(path))
        self.mock_build.assert_not_called()

    def test_wheel_without_metadata(self):
        path = self._wheel()
        with self.assertRaises(MetadataUnavailableError) as ctx:
            self._dependencies(path)
        self.assertIn('foo==1.0', str(ctx.exception))
        self.assertIn('no METADATA', str(ctx.exception))
        self.assertEqual('foo', ctx.exception.name)
        self.assertEqual(Version('1.0'), ctx.exception.version)

    def _wheel_with(self, files):
        path = self.dir / 'foo-1.0-py3-none-any.whl'
        with ZipFile(path, 'w') as zf:
            for name, data in files:
                zf.writestr(name, data)
        return path

    def test_wheel_multiple_dist_infos(self):
        path = self._wheel_with(
            [
                ('vendored-2.0.dist-info/METADATA', self._metadata('2.1')),
                ('foo-1.0.dist-info/METADATA', self._metadata('2.1')),
            ]
        )
        with self.assertRaises(MetadataUnavailableError) as ctx:
            self._dependencies(path)
        message = str(ctx.exception)
        self.assertIn('foo==1.0', message)
        self.assertIn('multiple .dist-info directories', message)
        self.assertIn('foo-1.0.dist-info', message)
        self.assertIn('vendored-2.0.dist-info', message)

    def test_wheel_mismatched_dist_info(self):
        path = self._wheel_with(
            [('other-1.0.dist-info/METADATA', self._metadata('2.1'))]
        )
        with self.assertRaises(MetadataUnavailableError) as ctx:
            self._dependencies(path)
        message = str(ctx.exception)
        self.assertIn('foo==1.0', message)
        self.assertIn(
            "'other-1.0.dist-info' does not start with 'foo'", message
        )

    def test_wheel_nested_dist_info_ignored(self):
        path = self._wheel_with(
            [
                ('foo/vendor/other-2.0.dist-info/METADATA', 'Requires-Dist: x'),
                ('foo-1.0.dist-info/METADATA', self._metadata('2.1')),
            ]
        )
        self.assertEqual([Requirement('six')], self._dependencies(path))

    def test_wheel_dist_info_without_metadata(self):
        path = self._wheel_with([('foo-1.0.dist-info/WHEEL', 'x')])
        with self.assertRaises(MetadataUnavailableError) as ctx:
            self._dependencies(path)
        self.assertIn('no METADATA', str(ctx.exception))

    def test_wheel_undecodable_requires_dist(self):
        data = self._metadata('2.1').encode() + b'Requires-Dist: attrs\xe9\n'
        path = self._wheel_with([('foo-1.0.dist-info/METADATA', data)])
        with self.assertRaises(MetadataUnavailableError) as ctx:
            self._dependencies(path)
        self.assertIn('foo==1.0', str(ctx.exception))
        self.assertIn('could not be decoded', str(ctx.exception))

    def test_wheel_invalid_requires_dist(self):
        path = self._wheel(self._metadata('2.1', 'Requires-Dist: six>=1.*.3\n'))
        with self.assertRaises(MetadataUnavailableError) as ctx:
            self._dependencies(path)
        self.assertIn('foo==1.0', str(ctx.exception))
        self.assertIn("'six>=1.*.3' is invalid", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, InvalidMetadata)

    def test_sdist_invalid_requires_dist(self):
        path = self._sdist(self._metadata('2.2', 'Requires-Dist: six>=1.*.3\n'))
        with self.assertRaises(MetadataUnavailableError) as ctx:
            self._dependencies(path)
        self.assertIn('foo==1.0', str(ctx.exception))
        self.assertIn("'six>=1.*.3' is invalid", str(ctx.exception))
        self.mock_build.assert_not_called()

    def test_built_invalid_requires_dist(self):
        self.mock_build.return_value = Metadata.from_raw(
            {'requires_dist': ['six>=1.*.3']}, validate=False
        )
        provider = self._provider()
        with self.assertRaises(MetadataUnavailableError) as ctx:
            self._dependencies(self._sdist(), provider)
        self.assertIn('foo==1.0', str(ctx.exception))
        self.assertIn("'six>=1.*.3' is invalid", str(ctx.exception))
        self.assertEqual({}, provider._file_requires_dist_cache)

    def test_sdist_undecodable_requires_dist_builds(self):
        pkg_info = (
            self._metadata('2.2').encode() + b'Requires-Dist: attrs\xe9\n'
        )
        path = self._sdist(pkg_info)
        self.assertEqual([Requirement('built-dep')], self._dependencies(path))
        self.mock_build.assert_called_once()

    def test_sdist_undecodable_dynamic_builds(self):
        pkg_info = (
            self._metadata('2.4').encode()
            + b'Dynamic: Requires-Dist\nDynamic: Summ\xe9ry\n'
        )
        path = self._sdist(pkg_info)
        self.assertEqual([Requirement('built-dep')], self._dependencies(path))
        self.mock_build.assert_called_once()

    def test_sdist_undecodable_requires_dist_fails_without_build_sdists(self):
        pkg_info = (
            self._metadata('2.2').encode() + b'Requires-Dist: attrs\xe9\n'
        )
        with self.assertRaises(MetadataUnavailableError) as ctx:
            self._dependencies(
                self._sdist(pkg_info), self._provider(build_sdists=False)
            )
        self.assertIn('--build-sdists', str(ctx.exception))

    def test_wheel_unreadable(self):
        path = self._wheel(contents=b'not a zip file')
        with self.assertRaises(MetadataUnavailableError) as ctx:
            self._dependencies(path)
        self.assertIn('foo==1.0', str(ctx.exception))
        self.assertIn('reading foo-1.0-py3-none-any.whl', str(ctx.exception))

    def test_sdist_reliable_pkg_info(self):
        path = self._sdist(self._metadata(version='2.2'))
        self.assertEqual([Requirement('six')], self._dependencies(path))
        self.mock_build.assert_not_called()

    def test_sdist_old_pkg_info_builds(self):
        path = self._sdist(self._metadata(version='2.1'))
        self.assertEqual([Requirement('built-dep')], self._dependencies(path))
        self.mock_build.assert_called_once()
        # The build gets the unpacked sdist, which is cleaned up afterwards
        source_dir = self.mock_build.call_args[0][0]
        self.assertEqual('unpacked', source_dir.name)
        self.assertFalse(source_dir.exists())

    def test_sdist_dynamic_requires_dist_builds(self):
        # Dynamic is compared case-insensitively
        path = self._sdist(
            self._metadata(version='2.4', extra='Dynamic: requires-dist\n')
        )
        self.assertEqual([Requirement('built-dep')], self._dependencies(path))
        self.mock_build.assert_called_once()

    def test_sdist_other_dynamic_field_uses_pkg_info(self):
        path = self._sdist(
            self._metadata(version='2.2', extra='Dynamic: Classifier\n')
        )
        self.assertEqual([Requirement('six')], self._dependencies(path))
        self.mock_build.assert_not_called()

    def test_sdist_without_pkg_info_builds(self):
        path = self._sdist()
        self.assertEqual([Requirement('built-dep')], self._dependencies(path))
        self.mock_build.assert_called_once()

    def test_sdist_build_failure(self):
        error = CalledProcessError(
            1, ['python'], output=b'backend stdout', stderr=b'backend stderr'
        )
        self.mock_build.side_effect = BuildBackendException(
            error, 'Backend subprocess exited'
        )
        path = self._sdist()
        with self.assertRaises(MetadataUnavailableError) as ctx:
            self._dependencies(path)
        message = str(ctx.exception)
        self.assertIn('foo==1.0', message)
        self.assertIn('building foo-1.0.tar.gz failed', message)
        self.assertIn('Backend subprocess exited', message)
        self.assertIn('backend stdout', message)
        self.assertIn('backend stderr', message)

    def test_build_disabled_by_default(self):
        provider = PyPIProvider(None, [], python_version='3.12')
        self.assertFalse(provider.build_sdists)

    def test_sdist_without_pkg_info_fails_without_build_sdists(self):
        path = self._sdist()
        provider = self._provider(build_sdists=False)
        with self.assertLogs('proviso.resolver', level='INFO') as logs:
            with self.assertRaises(MetadataUnavailableError) as ctx:
                self._dependencies(path, provider)
        message = str(ctx.exception)
        self.assertIn('foo==1.0', message)
        self.assertIn('can only be learned by building', message)
        self.assertIn('foo-1.0.tar.gz', message)
        self.assertIn('--build-sdists', message)
        self.assertEqual('foo', ctx.exception.name)
        self.assertEqual(Version('1.0'), ctx.exception.version)
        self.assertIn('--build-sdists', ctx.exception.reason)
        self.mock_build.assert_not_called()
        # Nothing is cached, so a later attempt fails the same way
        self.assertEqual({}, provider._file_requires_dist_cache)
        self.assertFalse([r for r in logs.records if r.levelname == 'WARNING'])

    def test_sdist_unreliable_pkg_info_fails_without_build_sdists(self):
        for pkg_info in (
            self._metadata(version='2.1'),
            self._metadata(version='2.4', extra='Dynamic: Requires-Dist\n'),
        ):
            path = self._sdist(pkg_info)
            with self.assertRaises(MetadataUnavailableError) as ctx:
                self._dependencies(path, self._provider(build_sdists=False))
            self.assertIn('--build-sdists', str(ctx.exception))
        self.mock_build.assert_not_called()

    def test_sdist_reliable_pkg_info_without_build_sdists(self):
        path = self._sdist(self._metadata(version='2.2'))
        provider = self._provider(build_sdists=False)
        self.assertEqual(
            [Requirement('six')], self._dependencies(path, provider)
        )
        self.mock_build.assert_not_called()

    def test_wheel_without_build_sdists(self):
        path = self._wheel(self._metadata(version='2.1'))
        provider = self._provider(build_sdists=False)
        self.assertEqual(
            [Requirement('six')], self._dependencies(path, provider)
        )
        self.mock_build.assert_not_called()

    def test_wheel_without_metadata_does_not_mention_build_sdists(self):
        path = self._wheel()
        with self.assertRaises(MetadataUnavailableError) as ctx:
            self._dependencies(path, self._provider(build_sdists=False))
        self.assertNotIn('--build-sdists', str(ctx.exception))

    def test_download_failure(self):
        provider = self._provider()
        candidate = self._candidate(self.dir / 'foo-1.0.tar.gz')
        with patch.object(
            provider.finder,
            'download_and_unpack',
            side_effect=UnpackError('Download failed: 404'),
        ):
            with self.assertRaises(MetadataUnavailableError) as ctx:
                provider.get_dependencies(candidate)
        message = str(ctx.exception)
        self.assertIn('foo==1.0', message)
        self.assertIn('Download failed: 404', message)
        self.mock_build.assert_not_called()

    def test_second_call_does_not_download_or_build_again(self):
        path = self._sdist()
        provider = self._provider()
        with patch.object(
            PackageFinder,
            'download_and_unpack',
            autospec=True,
            side_effect=PackageFinder.download_and_unpack,
        ) as mock_download:
            # The base candidate and an extras candidate share one fetch
            first = self._dependencies(path, provider)
            extras = Candidate(
                'foo', Version('1.0'), extras={'test'}, package=MagicMock()
            )
            provider.get_dependencies(extras)
        self.assertEqual([Requirement('built-dep')], first)
        mock_download.assert_called_once()
        self.mock_build.assert_called_once()

    def test_shared_cache_downloads_and_builds_once(self):
        path = self._sdist()
        cache = {}
        providers = [self._provider(cache), self._provider(cache)]
        with patch.object(
            PackageFinder,
            'download_and_unpack',
            autospec=True,
            side_effect=PackageFinder.download_and_unpack,
        ) as mock_download:
            for provider in providers:
                self.assertEqual(
                    [Requirement('built-dep')],
                    self._dependencies(path, provider),
                )
        mock_download.assert_called_once()
        self.mock_build.assert_called_once()
        self.assertEqual(
            {Link.from_path(path).url: [Requirement('built-dep')]}, cache
        )

    def test_build_logs_warning(self):
        path = self._sdist()
        with self.assertLogs('proviso.resolver', level='INFO') as logs:
            self._dependencies(path)
        warnings = [r for r in logs.records if r.levelname == 'WARNING']
        self.assertEqual(1, len(warnings))
        self.assertIn('foo==1.0', warnings[0].getMessage())
        self.assertIn('every target Python', warnings[0].getMessage())
        self.assertIn(
            'under the Python running proviso', warnings[0].getMessage()
        )

    def test_pkg_info_does_not_log_warning(self):
        path = self._sdist(self._metadata(version='2.2'))
        with self.assertLogs('proviso.resolver', level='INFO') as logs:
            self._dependencies(path)
        self.assertEqual(['INFO'], sorted({r.levelname for r in logs.records}))

    def test_resolver_shares_cache_with_providers(self):
        resolver = Resolver(session=MagicMock())
        with patch('proviso.resolver.ResolveLibResolver'):
            with patch('proviso.resolver.PyPIProvider') as mock_provider:
                resolver.resolve([Requirement('foo')], python_version='3.12')
                resolver.resolve([Requirement('foo')], python_version='3.13')
        caches = [
            c.kwargs['file_requires_dist_cache']
            for c in mock_provider.call_args_list
        ]
        self.assertEqual(2, len(caches))
        self.assertIs(resolver._file_requires_dist_cache, caches[0])
        self.assertIs(caches[0], caches[1])

    def test_resolve_passes_build_sdists_to_provider(self):
        resolver = Resolver(session=MagicMock())
        for kwargs, expected in (({}, False), ({'build_sdists': True}, True)):
            with patch('proviso.resolver.PyPIProvider') as mock_provider:
                with patch('proviso.resolver.ResolveLibResolver') as mock_rl:
                    mock_rl.return_value.resolve.return_value.mapping = {}
                    resolver.resolve([Requirement('foo')], **kwargs)
                self.assertEqual(
                    expected, mock_provider.call_args.kwargs['build_sdists']
                )

    def test_dist_info_link_does_not_download(self):
        session = MagicMock()
        session.get.return_value.text = self._metadata()
        provider = PyPIProvider(session, [], python_version='3.12')
        package = MagicMock()
        package.link = Link(
            'https://files.example/foo-1.0.tar.gz', dist_info_metadata=True
        )
        candidate = Candidate('foo', Version('1.0'), package=package)
        with patch.object(provider.finder, 'download_and_unpack') as mock_dl:
            result = provider.get_dependencies(candidate)
        self.assertEqual([Requirement('six')], result)
        session.get.assert_called_once_with(
            'https://files.example/foo-1.0.tar.gz.metadata'
        )
        mock_dl.assert_not_called()
        self.mock_build.assert_not_called()
        self.assertEqual({}, provider._file_requires_dist_cache)


class TestTargetPythonSeries(TestCase):
    """A target Python version stands for its whole release series."""

    REQUIRES_DIST = (
        'old; python_full_version < "3.12.4"',
        'new; python_full_version >= "3.12.4"',
        'next; python_version >= "3.13"',
        'legacy; python_version < "3.11"',
        'fast; extra == "speed" and python_full_version >= "3.12.2"',
        'slow; extra == "speed" and python_full_version < "3.12"',
    )

    def _dependencies(self, python_version, extras=()):
        session = MagicMock()
        response = MagicMock()
        response.text = (
            'Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n'
            + ''.join(f'Requires-Dist: {r}\n' for r in self.REQUIRES_DIST)
        )
        session.get.return_value = response
        with patch('proviso.resolver.SeriesPackageFinder'):
            provider = PyPIProvider(
                session, ['https://pypi.org/simple/'], python_version
            )
        package = MagicMock()
        package.link.dist_info_link.url = 'https://pypi/pkg/1.0'
        candidate = Candidate(
            'pkg', Version('1.0'), extras=extras, package=package
        )
        return [r.name for r in provider.get_dependencies(candidate)]

    def test_get_dependencies_series(self):
        self.assertEqual(['old', 'new'], self._dependencies('3.12'))
        self.assertEqual(['new'], self._dependencies('3.12.5'))
        self.assertEqual(['old'], self._dependencies('3.12.0'))
        self.assertEqual(
            ['old', 'new', 'next', 'legacy'], self._dependencies('3')
        )

    def test_get_dependencies_extras(self):
        self.assertEqual(
            ['pkg', 'fast'], self._dependencies('3.12', extras=['speed'])
        )
        self.assertEqual(
            ['pkg'], self._dependencies('3.12.1', extras=['speed'])
        )
        self.assertEqual(
            ['pkg', 'fast', 'slow'], self._dependencies('3', extras=['speed'])
        )

    def test_get_dependencies_without_target_uses_host(self):
        host = {'python_version': '3.12', 'python_full_version': '3.12.1'}
        with patch('proviso.resolver.default_environment') as de:
            de.side_effect = lambda: dict(host)
            self.assertEqual(['old'], self._dependencies(None))

    def _filtered(self, python_version, extras=()):
        requirements = [Requirement(r) for r in self.REQUIRES_DIST]
        return [
            r.name
            for r in filter_requirements(
                requirements, python_version=python_version, extras=extras
            )
        ]

    def test_filter_requirements_series(self):
        self.assertEqual(['old', 'new'], self._filtered('3.12'))
        self.assertEqual(['new'], self._filtered('3.12.5'))
        self.assertEqual(
            ['old', 'new', 'fast'], self._filtered('3.12', extras=['speed'])
        )
        self.assertEqual(
            ['old', 'new', 'next', 'legacy', 'fast', 'slow'],
            self._filtered('3', extras=['speed']),
        )

    def test_filter_requirements_without_target_uses_host(self):
        host = {'python_version': '3.12', 'python_full_version': '3.12.5'}
        with patch('proviso.resolver.default_environment') as de:
            de.side_effect = lambda: dict(host)
            self.assertEqual(['new'], self._filtered(None))

    def test_requires_python_series(self):
        """The provider's finder accepts files that need a later patch."""
        provider = PyPIProvider(None, [], python_version='3.12')
        evaluator = provider.finder.build_evaluator('foo')
        link = Link(
            'https://example.com/foo-1.0.tar.gz', requires_python='>=3.12.1'
        )
        self.assertEqual('1.0', evaluator.evaluate_link(link).version)
        link = Link(
            'https://example.com/foo-1.0.tar.gz', requires_python='>=3.13'
        )
        self.assertIsNone(evaluator.evaluate_link(link))


class TestDirectReferences(TestCase):
    """Direct references (name @ url) aren't supported."""

    def _provider(self, requires_dist):
        session = MagicMock()
        response = MagicMock()
        response.text = (
            'Metadata-Version: 2.1\nName: pkg\nVersion: 1.0\n'
            + ''.join(f'Requires-Dist: {r}\n' for r in requires_dist)
        )
        session.get.return_value = response
        with patch('proviso.resolver.SeriesPackageFinder'):
            return PyPIProvider(session, ['https://pypi.org/simple/'], '3.12')

    def _candidate(self, extras=()):
        package = MagicMock()
        package.link.dist_info_link.url = 'https://pypi/pkg/1.0'
        return Candidate('pkg', Version('1.0'), extras=extras, package=package)

    def test_redact_requirement(self):
        for written, expected in (
            (
                'foo @ https://user:token@example.com/foo-1.0.whl',
                'foo @ https://example.com/foo-1.0.whl',
            ),
            (
                'foo @ https://token@example.com:8443/foo-1.0.whl',
                'foo @ https://example.com:8443/foo-1.0.whl',
            ),
            (
                'foo[a,b] @ git+https://u:p@example.com/foo.git@v1.0 ; '
                'python_version < "3.11"',
                'foo[a,b] @ git+https://example.com/foo.git@v1.0 ; '
                'python_version < "3.11"',
            ),
            (
                'foo @ https://example.com/foo-1.0.whl',
                'foo @ https://example.com/foo-1.0.whl',
            ),
            ('foo @ file:///tmp/foo-1.0.whl', 'foo @ file:///tmp/foo-1.0.whl'),
            (
                'foo @ https://example.com/foo.whl?token=SECRET#sha256=SECRET',
                'foo @ https://example.com/foo.whl',
            ),
            (
                'foo @ git+https://example.com/foo.git@v1.0#subdirectory=x',
                'foo @ git+https://example.com/foo.git@v1.0',
            ),
        ):
            requirement = Requirement(written)
            self.assertEqual(expected, redact_requirement(requirement))
            # The requirement itself is unchanged
            self.assertEqual(written, str(requirement))

    # URLs packaging accepts, but urlsplit can't parse
    UNPARSEABLE = (
        'https://u:SECRET@[::1/x.whl',
        'https://u:SECRET@[notip]/x.whl',
        'https://u:SECRET@host]/x.whl',
        'https://u:SECRET@[::1]x/x.whl',
    )
    # URLs with credentials outside of the userinfo
    SECRET_URLS = (
        'https://example.com/x.whl?token=SECRET',
        'https://example.com/x.whl?X-Amz-Credential=SECRET&X-Amz-Signature=SECRET',
        'https://example.com/x.whl#token=SECRET',
        'https://example.com/x.whl#sha256=SECRET',
        'https://u:SECRET@example.com/x.whl?a=SECRET#b=SECRET',
    )

    def test_redact_requirement_unparseable(self):
        for url in self.UNPARSEABLE:
            requirement = Requirement(
                f'foo[a,b] @ {url} ; python_version < "3.11"'
            )
            self.assertEqual(
                'foo[a,b] @ <unparseable URL> ; python_version < "3.11"',
                redact_requirement(requirement),
            )
            error = UnsupportedRequirementError(
                requirement, 'pkg', Version('1.0')
            )
            self.assertEqual(
                'pkg==1.0 requires a direct reference, which proviso does not '
                'support: foo[a,b] @ <unparseable URL> ; '
                'python_version < "3.11"',
                str(error),
            )

    def test_redact_requirement_query_and_fragment(self):
        for url in self.SECRET_URLS:
            requirement = Requirement(f'foo @ {url}')
            self.assertEqual(
                'foo @ https://example.com/x.whl',
                redact_requirement(requirement),
            )
            error = UnsupportedRequirementError(
                requirement, 'pkg', Version('1.0')
            )
            self.assertNotIn('SECRET', str(error))
            self.assertNotIn('Amz', str(error))

    def test_dependency_with_url_raises(self):
        provider = self._provider(
            ['ok>=1', 'bar @ https://user:token@example.com/bar-1.0.whl']
        )
        with self.assertRaises(UnsupportedRequirementError) as ctx:
            provider.get_dependencies(self._candidate())
        error = ctx.exception
        self.assertEqual('bar', error.requirement.name)
        self.assertEqual('pkg', error.name)
        self.assertEqual(Version('1.0'), error.version)
        self.assertEqual(
            'pkg==1.0 requires a direct reference, which proviso does not '
            'support: bar @ https://example.com/bar-1.0.whl',
            str(error),
        )
        self.assertNotIn('token', str(error))
        self.assertNotIn('user', str(error))

    def test_dependency_with_url_not_applying_is_ignored(self):
        provider = self._provider(
            [
                'ok>=1',
                'bar @ https://example.com/bar.whl ; python_version < "3.11"',
                'baz @ https://example.com/baz.whl ; extra == "fast"',
            ]
        )
        self.assertEqual(
            ['ok>=1'],
            [str(r) for r in provider.get_dependencies(self._candidate())],
        )

    def test_extra_dependency_with_url_raises(self):
        provider = self._provider(
            ['ok>=1', 'baz @ https://example.com/baz.whl ; extra == "fast"']
        )
        with self.assertRaises(UnsupportedRequirementError) as ctx:
            provider.get_dependencies(self._candidate(extras={'fast'}))
        self.assertEqual('baz', ctx.exception.requirement.name)

    def test_resolve_dependency_on_project_with_url_raises(self):
        """Only the project's requirements on itself are exempt."""
        provider = self._provider(['mypkg @ https://example.com/mypkg.whl'])
        package = MagicMock()
        package.version = '1.0'
        package.link.dist_info_link.url = 'https://pypi/pkg/1.0'
        package.link.is_yanked = False
        provider.finder.find_matches.return_value = [package]
        with patch('proviso.resolver.PyPIProvider', return_value=provider):
            resolver = Resolver(session=MagicMock())
            with self.assertRaises(UnsupportedRequirementError) as ctx:
                resolver.resolve(
                    [Requirement('pkg')],
                    python_version='3.12',
                    project_name='mypkg',
                )
        self.assertEqual('mypkg', ctx.exception.requirement.name)
        self.assertEqual('pkg', ctx.exception.name)

    def test_extras_candidate_base_pin_is_not_a_direct_reference(self):
        # The pin an extras candidate adds on its base package isn't a
        # direct reference
        provider = self._provider(
            [
                'ok>=1',
                'fast-dep>=2 ; extra == "fast"',
                'baz @ https://example.com/baz.whl ; extra == "other"',
            ]
        )
        dependencies = provider.get_dependencies(
            self._candidate(extras={'fast'})
        )
        self.assertIsInstance(dependencies[0], BasePin)
        self.assertEqual(
            ['pkg==1.0', 'fast-dep>=2; extra == "fast"'],
            [str(r) for r in dependencies],
        )

    def test_resolve_dependency_with_url_raises(self):
        provider = self._provider(['bar @ https://example.com/bar-1.0.whl'])
        package = MagicMock()
        package.version = '1.0'
        package.link.dist_info_link.url = 'https://pypi/pkg/1.0'
        package.link.is_yanked = False
        provider.finder.find_matches.return_value = [package]
        with patch('proviso.resolver.PyPIProvider', return_value=provider):
            resolver = Resolver(session=MagicMock())
            with self.assertRaises(UnsupportedRequirementError):
                resolver.resolve([Requirement('pkg')], python_version='3.12')
        # The direct reference was never looked up by name
        provider.finder.find_matches.assert_called_once_with(
            'pkg', allow_yanked=True, allow_prereleases=True
        )


class TestSelfReferentialExtras(TestCase):
    """A project requirement on the project itself selects its extras."""

    METADATA = (
        'Metadata-Version: 2.4\n'
        'Name: mypkg\n'
        'Version: 1.0\n'
        'Provides-Extra: test\n'
        'Provides-Extra: lint\n'
        'Provides-Extra: dev\n'
        'Requires-Dist: a\n'
        'Requires-Dist: pytest; extra == "test"\n'
        'Requires-Dist: black; extra == "lint"\n'
        'Requires-Dist: mypkg[test,lint]; extra == "dev"\n'
        'Requires-Dist: ipython; extra == "dev"\n'
    )

    def _names(self, requirements, **kwargs):
        with patch('proviso.resolver.default_environment') as de:
            de.return_value = {'sys_platform': 'linux'}
            return [
                str(r)
                for r in filter_requirements(
                    [Requirement(r) for r in requirements], **kwargs
                )
            ]

    def test_pyproject_example(self):
        metadata = Metadata.from_email(self.METADATA)
        with patch('proviso.resolver.default_environment') as de:
            de.return_value = {'sys_platform': 'linux'}
            filtered = filter_requirements(
                metadata.requires_dist,
                python_version='3.12',
                extras=['dev'],
                project_name=metadata.name,
            )
        self.assertEqual(
            ['a', 'pytest', 'black', 'ipython'], [r.name for r in filtered]
        )

    def test_extra_not_selected(self):
        metadata = Metadata.from_email(self.METADATA)
        for extras, expected in (
            ((), ['a']),
            (['test'], ['a', 'pytest']),
            (['lint', 'test'], ['a', 'pytest', 'black']),
        ):
            with patch('proviso.resolver.default_environment') as de:
                de.return_value = {'sys_platform': 'linux'}
                filtered = filter_requirements(
                    metadata.requires_dist,
                    python_version='3.12',
                    extras=extras,
                    project_name='mypkg',
                )
            self.assertEqual(expected, [r.name for r in filtered])

    def test_nested(self):
        requirements = [
            'mypkg[test]; extra == "dev"',
            'mypkg[lint]; extra == "test"',
            'pytest; extra == "test"',
            'black; extra == "lint"',
            'ipython; extra == "dev"',
            'other; extra == "other"',
        ]
        self.assertEqual(
            [
                'pytest; extra == "test"',
                'black; extra == "lint"',
                'ipython; extra == "dev"',
            ],
            self._names(
                requirements,
                python_version='3.12',
                extras=['dev'],
                project_name='mypkg',
            ),
        )

    def test_cycle(self):
        requirements = [
            'mypkg[b]; extra == "a"',
            'mypkg[a]; extra == "b"',
            'x; extra == "a"',
            'y; extra == "b"',
        ]
        for selected in ('a', 'b'):
            self.assertEqual(
                ['x; extra == "a"', 'y; extra == "b"'],
                self._names(
                    requirements,
                    python_version='3.12',
                    extras=[selected],
                    project_name='mypkg',
                ),
            )

    def test_python_marker(self):
        requirements = [
            'mypkg[test]; extra == "dev" and python_version < "3.11"',
            'pytest; extra == "test"',
            'ipython; extra == "dev"',
        ]
        kwargs = {'extras': ['dev'], 'project_name': 'mypkg'}
        self.assertEqual(
            ['pytest; extra == "test"', 'ipython; extra == "dev"'],
            self._names(requirements, python_version='3.10', **kwargs),
        )
        self.assertEqual(
            ['ipython; extra == "dev"'],
            self._names(requirements, python_version='3.12', **kwargs),
        )

    def test_spelling(self):
        requirements = [
            'My.Pkg[Test_Extra]; extra == "Dev"',
            'pytest; extra == "test-extra"',
            'ipython; extra == "dev"',
        ]
        for project_name, extras in (
            ('my_pkg', ['dev']),
            ('MY-PKG', ['DEV']),
            ('my--pkg', ['Dev']),
        ):
            self.assertEqual(
                ['pytest; extra == "test-extra"', 'ipython; extra == "dev"'],
                self._names(
                    requirements,
                    python_version='3.12',
                    extras=extras,
                    project_name=project_name,
                ),
            )

    def test_without_extras_or_unknown_extra(self):
        requirements = [
            'a',
            'mypkg>=99',
            'mypkg[nope]; extra == "dev"',
            'pytest; extra == "test"',
        ]
        self.assertEqual(
            ['a'],
            self._names(
                requirements,
                python_version='3.12',
                extras=['dev'],
                project_name='mypkg',
            ),
        )

    def test_project_name_none_unchanged(self):
        requirements = [
            'a',
            'mypkg[test]; extra == "dev"',
            'pytest; extra == "test"',
        ]
        self.assertEqual(
            ['a', 'mypkg[test]; extra == "dev"'],
            self._names(requirements, python_version='3.12', extras=['dev']),
        )

    def test_other_package_name_unaffected(self):
        requirements = [
            'mypkg-extra[test]; extra == "dev"',
            'pytest; extra == "test"',
        ]
        self.assertEqual(
            ['mypkg-extra[test]; extra == "dev"'],
            self._names(
                requirements,
                python_version='3.12',
                extras=['dev'],
                project_name='mypkg',
            ),
        )

    def test_self_reference_direct_reference(self):
        """A self-reference with a URL is left out and its extras expanded."""
        requirements = [
            'a',
            'pytest; extra == "test"',
            'black; extra == "lint"',
            'mypkg[test] @ file:///src/mypkg ; extra == "dev"',
        ]
        self.assertEqual(
            ['a', 'pytest; extra == "test"'],
            self._names(
                requirements,
                python_version='3.12',
                extras=['dev'],
                project_name='mypkg',
            ),
        )

    def test_resolve_passes_project_name(self):
        metadata = Metadata.from_email(self.METADATA)
        with patch('proviso.resolver.CachingClient'):
            with patch('proviso.resolver.MultiDomainBasicAuth'):
                resolver = Resolver()
        for kwargs, expected in (
            # A requirement with extras is passed after the ones without
            ({}, ['a', 'ipython', 'mypkg']),
            ({'project_name': 'mypkg'}, ['a', 'pytest', 'black', 'ipython']),
        ):
            mock_result = MagicMock()
            mock_result.mapping = {}
            with patch('proviso.resolver.ResolveLibResolver') as rl:
                rl.return_value.resolve.return_value = mock_result
                with patch('proviso.resolver.PyPIProvider') as provider:
                    resolver.resolve(
                        metadata.requires_dist,
                        python_version='3.12',
                        extras=['dev'],
                        **kwargs,
                    )
            passed = rl.return_value.resolve.call_args.args[0]
            self.assertEqual(expected, [r.name for r in passed])
            user_requested = provider.call_args.kwargs['user_requested']
            self.assertEqual(
                'mypkg' in expected, 'mypkg[lint,test]' in user_requested
            )
            # The cooldown's exemption only sees the filtered requirements
            project_requirements = provider.call_args.kwargs[
                'project_requirements'
            ]
            self.assertEqual(
                sorted(expected), sorted(r.name for r in project_requirements)
            )

    def test_dependency_on_its_own_extras_unchanged(self):
        """A dependency that requires its own extras goes to the resolver."""
        session = MagicMock()
        response = MagicMock()
        response.text = (
            'Metadata-Version: 2.1\nName: foo\nVersion: 1.0\n'
            'Requires-Dist: foo[baz]; extra == "bar"\n'
            'Requires-Dist: qux; extra == "baz"\n'
        )
        session.get.return_value = response
        with patch('proviso.resolver.SeriesPackageFinder'):
            provider = PyPIProvider(
                session, ['https://pypi.org/simple/'], '3.12'
            )
        package = MagicMock()
        package.link.dist_info_link.url = 'https://pypi/foo/1.0'
        candidate = Candidate(
            'foo', Version('1.0'), extras={'bar'}, package=package
        )
        self.assertEqual(
            ['foo==1.0', 'foo[baz]; extra == "bar"'],
            [str(r) for r in provider.get_dependencies(candidate)],
        )


class TestYanked(TestCase):
    """Yanked releases are candidates only for an exact pin (PEP 592)."""

    FILES = {
        'foo': [
            index_file('foo', '1.0'),
            index_file('foo', '1.1', yanked='broken build'),
            index_file('foo', '1.2', yanked=True),
        ],
        'app': [index_file('app', '1.0')],
    }
    REQUIRES = {('app', '1.0'): ['foo==1.1']}

    def _provider(self):
        return PyPIProvider(
            FakeIndex(self.FILES, self.REQUIRES),
            ['https://pypi.org/simple/'],
            python_version='3.12',
        )

    def _versions(self, identifier, requirements, provider=None):
        provider = provider or self._provider()
        requirements = {
            k: [r if isinstance(r, Requirement) else Requirement(r) for r in v]
            for k, v in requirements.items()
        }
        result = provider.find_matches(identifier, requirements, {})
        return [str(c.version) for c in result]

    def _resolve(self, *requirements):
        resolver = Resolver(session=FakeIndex(self.FILES, self.REQUIRES))
        return {
            name: info['version']
            for name, info in resolver.resolve(
                [Requirement(r) for r in requirements], python_version='3.12'
            ).items()
        }

    def test_not_offered_without_exact_pin(self):
        expected = {
            'foo': ['1.0'],
            'foo>=1.0': ['1.0'],
            'foo==1.*': ['1.0'],
            'foo>=1.1': [],
            'foo~=1.1': [],
            'foo!=1.0': [],
        }
        for spec, versions in expected.items():
            with self.subTest(spec=spec):
                with self.assertNoLogs('proviso.resolver', level='WARNING'):
                    self.assertEqual(
                        versions, self._versions('foo', {'foo': [spec]})
                    )

    def test_offered_for_exact_pin(self):
        for spec in ('foo==1.1', 'foo==1.1.0', 'foo===1.1', 'foo>=1.0,==1.1'):
            with self.subTest(spec=spec):
                self.assertEqual(
                    ['1.1'], self._versions('foo', {'foo': [spec]})
                )

    def test_exact_pin_on_another_version(self):
        self.assertEqual(['1.0'], self._versions('foo', {'foo': ['foo==1.0']}))

    def test_exact_pin_on_another_package(self):
        self.assertEqual(
            [],
            self._versions('foo', {'foo': ['foo>=1.1'], 'bar': ['bar==1.1']}),
        )

    def test_not_offered_for_extras_base_pin(self):
        """The pin an extras candidate adds isn't a real exact pin."""
        self.assertEqual(
            [], self._versions('foo', {'foo': [BasePin('foo==1.1')]})
        )

    def test_offered_for_exact_pin_with_extras(self):
        """A real pin on the package with extras counts for the base too."""
        requirements = {'foo[x]': ['foo[x]==1.1'], 'foo': [BasePin('foo==1.1')]}
        self.assertEqual(['1.1'], self._versions('foo', requirements))
        self.assertEqual(['1.1'], self._versions('foo[x]', requirements))

    def test_get_dependencies_extras_pin(self):
        provider = self._provider()
        candidate = Candidate('foo', Version('1.1'), extras={'x'})
        candidate.package = MagicMock()
        candidate.package.link.dist_info_link = None
        with patch.object(provider, '_get_requires_dist', return_value=[]):
            (pin,) = provider.get_dependencies(candidate)
        self.assertIsInstance(pin, BasePin)
        self.assertEqual('foo==1.1', str(pin))

    def test_resolve_project_pin(self):
        with self.assertLogs('proviso.resolver', level='WARNING') as logs:
            self.assertEqual({'foo': '1.1'}, self._resolve('foo==1.1'))
        self.assertEqual(
            [
                'WARNING:proviso.resolver:foo==1.1 is yanked (broken build); '
                'using it because it is pinned exactly'
            ],
            logs.output,
        )

    def test_resolve_project_pin_with_extras(self):
        self.assertEqual({'foo': '1.1'}, self._resolve('foo[x]==1.1'))

    def test_resolve_transitive_pin(self):
        self.assertEqual({'app': '1.0', 'foo': '1.1'}, self._resolve('app'))

    def test_resolve_without_pin(self):
        self.assertEqual({'foo': '1.0'}, self._resolve('foo'))

    def test_warning_without_reason(self):
        with self.assertLogs('proviso.resolver', level='WARNING') as logs:
            self.assertEqual({'foo': '1.2'}, self._resolve('foo==1.2'))
        self.assertIn('foo==1.2 is yanked (no reason given)', logs.output[0])

    def test_warning_logged_once_per_run(self):
        resolver = Resolver(session=FakeIndex(self.FILES, self.REQUIRES))
        with self.assertLogs('proviso.resolver', level='WARNING') as logs:
            for python_version in ('3.12', '3.13'):
                resolver.resolve(
                    [Requirement('foo==1.1')], python_version=python_version
                )
            # A separate resolver is a separate run
            Resolver(session=FakeIndex(self.FILES, self.REQUIRES)).resolve(
                [Requirement('foo==1.1')], python_version='3.12'
            )
        yanked = [line for line in logs.output if 'is yanked' in line]
        self.assertEqual(2, len(yanked))

    def test_warning_once_for_extras_and_base(self):
        resolver = Resolver(session=FakeIndex(self.FILES, self.REQUIRES))
        with self.assertLogs('proviso.resolver', level='WARNING') as logs:
            result = resolver.resolve(
                [Requirement('foo[x]'), Requirement('foo==1.1')],
                python_version='3.12',
            )
        self.assertEqual({'foo': {'version': '1.1'}}, result)
        self.assertEqual(1, len(logs.output))
        self.assertIn('foo==1.1 is yanked', logs.output[0])

    def test_no_warning_when_backtracked_away(self):
        files = {
            'foo': self.FILES['foo'],
            'app': [index_file('app', '1.0'), index_file('app', '2.0')],
        }
        requires = {
            ('app', '1.0'): ['foo>=1.0'],
            ('app', '2.0'): ['foo==1.1', 'missing'],
        }
        resolver = Resolver(session=FakeIndex(files, requires))
        with self.assertNoLogs('proviso.resolver', level='WARNING'):
            result = resolver.resolve(
                [Requirement('app')], python_version='3.12'
            )
        self.assertEqual(
            {'app': {'version': '1.0'}, 'foo': {'version': '1.0'}}, result
        )

    def test_no_warning_when_unyanked_file_selected(self):
        """Only some of a release's files are yanked."""
        files = {
            'foo': [
                index_file('foo', '1.0'),
                index_file('foo', '1.1', yanked='broken wheel'),
                index_file('foo', '1.1', sdist=True),
            ]
        }
        resolver = Resolver(session=FakeIndex(files))
        with self.assertNoLogs('proviso.resolver', level='WARNING'):
            result = resolver.resolve(
                [Requirement('foo==1.1')], python_version='3.12'
            )
        self.assertEqual({'foo': {'version': '1.1'}}, result)

    def test_warning_when_yanked_file_selected(self):
        resolver = Resolver(session=FakeIndex(self.FILES, self.REQUIRES))
        with self.assertLogs('proviso.resolver', level='WARNING') as logs:
            resolver.resolve([Requirement('app')], python_version='3.12')
        self.assertEqual(
            [
                'WARNING:proviso.resolver:foo==1.1 is yanked (broken build); '
                'using it because it is pinned exactly'
            ],
            logs.output,
        )

    def test_candidate_without_package(self):
        resolver = Resolver(session=FakeIndex(self.FILES, self.REQUIRES))
        result = MagicMock()
        result.mapping = {'foo': Candidate('foo', Version('1.0'))}
        with patch('proviso.resolver.ResolveLibResolver') as rl:
            rl.return_value.resolve.return_value = result
            with self.assertNoLogs('proviso.resolver', level='WARNING'):
                self.assertEqual(
                    {'foo': {'version': '1.0'}},
                    resolver.resolve(
                        [Requirement('foo')], python_version='3.12'
                    ),
                )

    def test_resolve_extras_first_with_later_pin(self):
        for order in (['foo[x]', 'foo==1.1'], ['foo==1.1', 'foo[x]']):
            with self.subTest(order=order):
                resolver = Resolver(
                    session=FakeIndex(self.FILES, self.REQUIRES)
                )
                result = resolver.resolve(
                    [Requirement(r) for r in order], python_version='3.12'
                )
                self.assertEqual({'foo': {'version': '1.1'}}, result)

    def test_extras_requirements_ordered_last(self):
        resolver = Resolver(session=FakeIndex(self.FILES, self.REQUIRES))
        result = MagicMock()
        result.mapping = {}
        with patch('proviso.resolver.ResolveLibResolver') as rl:
            rl.return_value.resolve.return_value = result
            with patch('proviso.resolver.PyPIProvider') as provider:
                resolver.resolve(
                    [
                        Requirement('foo[x]'),
                        Requirement('bar'),
                        Requirement('baz[y]'),
                        Requirement('foo==1.1'),
                    ],
                    python_version='3.12',
                )
        passed = rl.return_value.resolve.call_args.args[0]
        self.assertEqual(['bar', 'foo', 'foo', 'baz'], [r.name for r in passed])
        self.assertEqual(
            ['foo[x]', 'baz[y]'], [str(r) for r in passed if r.extras]
        )
        # The user's order is what's preferred, not the order passed on
        self.assertEqual(
            {'foo[x]': 0, 'foo': 0, 'bar': 1, 'baz[y]': 2, 'baz': 2},
            provider.call_args.kwargs['user_requested'],
        )

    def test_yanked_files_sorted_last(self):
        """Asking unearth for yanked files doesn't reorder the others."""
        finder = self._provider().finder
        with_yanked = list(
            finder.find_matches(
                'foo', allow_yanked=True, allow_prereleases=True
            )
        )
        without = list(finder.find_matches('foo', allow_prereleases=True))
        self.assertEqual(without, with_yanked[: len(without)])
        self.assertEqual(
            [False, True, True], [p.link.is_yanked for p in with_yanked]
        )


class TestExtrasBaseConflict(TestCase):
    """A base package constraint that rules out an extras requirement."""

    FILES = {
        'foo': [index_file('foo', '1.0'), index_file('foo', '2.0')],
        'bar': [index_file('bar', '1.0')],
        'baz': [index_file('baz', '1.0')],
    }
    REQUIRES = {('bar', '1.0'): ['foo<2'], ('baz', '1.0'): ['foo[x]>=2']}

    def _error(self, *requirements, files=None, requires=None):
        resolver = Resolver(
            session=FakeIndex(files or self.FILES, requires or self.REQUIRES)
        )
        with self.assertRaises(ResolutionImpossible) as caught:
            resolver.resolve(
                [Requirement(r) for r in requirements], python_version='3.12'
            )
        return caught.exception

    def _message(self, *requirements, files=None, requires=None):
        return _resolution_error_message(
            self._error(*requirements, files=files, requires=requires),
            '3.12',
            cooldown=True,
        )

    @staticmethod
    def _files(**versions):
        return {
            name: [index_file(name, v) for v in vs]
            for name, vs in versions.items()
        }

    def test_project_requirements(self):
        for requirements in (('foo[x]>=2', 'foo<2'), ('foo<2', 'foo[x]>=2')):
            with self.subTest(requirements=requirements):
                self.assertEqual(
                    'Unable to resolve dependencies for Python 3.12, these '
                    'requirements conflict:\n'
                    '  foo[x]>=2 (from the project)\n'
                    '  foo<2 (from the project)',
                    self._message(*requirements),
                )

    def test_from_dependency(self):
        """The extras candidate's base pin conflicts with the dependency."""
        self.assertEqual(
            'Unable to resolve dependencies for Python 3.12, these '
            'requirements conflict:\n'
            '  foo<2 (from bar==1.0)\n'
            '  foo==2.0 (from foo[x]==2.0)',
            self._message('bar', 'foo[x]>=2'),
        )

    def test_from_dependency_merged_first(self):
        """The base constraint is merged before the extras requirement."""
        self.assertEqual(
            'Unable to resolve dependencies for Python 3.12, these '
            'requirements conflict:\n'
            '  foo[x]>=2 (from baz==1.0)\n'
            '  foo<2 (from bar==1.0)',
            self._message('bar==1.0', 'baz==1.0'),
        )

    def test_only_excluding_requirements(self):
        """A base requirement that excludes nothing isn't listed."""
        self.assertEqual(
            'Unable to resolve dependencies for Python 3.12, these '
            'requirements conflict:\n'
            '  foo[x] (from the project)\n'
            '  foo[x]>=2 (from the project)\n'
            '  foo<2 (from the project)',
            self._message('foo', 'foo<2', 'foo[x]', 'foo[x]>=2'),
        )

    def test_no_matching_version(self):
        """Without a base constraint nothing else is blamed."""
        error = self._error('foo[x]>=3')
        self.assertEqual(
            ['foo[x]>=3'], [str(c.requirement) for c in error.causes]
        )
        self.assertEqual(
            'Unable to resolve dependencies for Python 3.12: no version of '
            'foo satisfies foo[x]>=3 (from the project); releases inside the '
            '--cooldown-days window are excluded',
            _resolution_error_message(error, '3.12', cooldown=True),
        )

    def test_stale_conflict(self):
        """A recorded conflict only applies to the requirements it saw."""
        provider = PyPIProvider(
            FakeIndex(self.FILES, self.REQUIRES),
            ['https://pypi.org/simple/'],
            python_version='3.12',
        )
        requirements = {
            'foo[x]': [Requirement('foo[x]>=2')],
            'foo': [Requirement('foo<2')],
        }
        self.assertEqual([], provider.find_matches('foo[x]', requirements, {}))
        recorded = _RequirementInfo(requirements['foo[x]'][0], None)
        self.assertEqual(
            [recorded, _RequirementInfo(requirements['foo'][0], None)],
            provider.base_conflict_causes([recorded]),
        )
        other = _RequirementInfo(Requirement('foo[x]>=2'), None)
        self.assertEqual([other], provider.base_conflict_causes([other]))
        # Candidates found later clear it
        requirements['foo'] = []
        provider.find_matches('foo[x]', requirements, {})
        self.assertEqual([recorded], provider.base_conflict_causes([recorded]))

    def test_conflict_found_while_backtracking(self):
        """Only the records from when the conflict was found are used.

        resolvelib calls find_matches again while it backtracks from the
        conflict, after it has worked out its causes. Here those calls record
        foo<3 for foo[x], although foo[x]<3 and foo[x]>=2 leave only 2.0,
        which foo<3 allows.
        """
        self.assertEqual(
            'Unable to resolve dependencies for Python 3.12, these '
            'requirements conflict:\n'
            '  foo[x] (from the project)\n'
            '  foo[x]<3 (from c==2.0)\n'
            '  foo[x]>=2 (from c==2.0)',
            self._message(
                'foo[y]',
                'foo<3',
                'foo[x]',
                'b<2',
                files=self._files(
                    foo=('1.0', '2.0', '3.0'),
                    a=('1.0', '2.0'),
                    b=('1.0', '2.0'),
                    c=('1.0', '2.0'),
                ),
                requires={
                    ('a', '1.0'): ['foo[x]>=2'],
                    ('b', '1.0'): ['foo<2'],
                    ('b', '2.0'): ['c<2', 'foo>=2'],
                    ('c', '2.0'): ['foo[x]<3', 'foo[x]>=2'],
                    ('foo', '1.0'): ['c>=2; extra == "x"'],
                    ('foo', '3.0'): ['b>=2; extra == "x"'],
                },
            ),
        )

    def test_record_replaced_while_backtracking(self):
        """A record from when the conflict was found isn't replaced later.

        The conflict leaves a correct record for foo[y]: foo<3 and the pin
        foo==2.0 exclude 3.0, the only version foo[y]>=3 allows. While
        backtracking, find_matches replaces it with one that names foo>=2,
        which allows 3.0.
        """
        self.assertEqual(
            'Unable to resolve dependencies for Python 3.12, these '
            'requirements conflict:\n'
            '  foo[y] (from the project)\n'
            '  foo[y] (from b==1.0)\n'
            '  foo[y]>=3 (from c==2.0)\n'
            '  foo<3 (from the project)\n'
            '  foo==2.0 (from foo[y]==2.0)',
            self._message(
                'b<2',
                'foo[y]',
                'foo<3',
                files=self._files(
                    foo=('1.0', '2.0', '3.0'),
                    a=('1.0', '2.0'),
                    b=('1.0', '2.0'),
                    c=('1.0', '2.0'),
                ),
                requires={
                    ('a', '1.0'): ['foo[x,y]', 'foo>=2'],
                    ('a', '2.0'): ['c<2', 'foo[x,y]'],
                    ('b', '1.0'): ['a<2', 'foo[y]'],
                    ('b', '2.0'): ['c<2'],
                    ('c', '1.0'): ['foo[x]<3', 'foo[y]'],
                    ('c', '2.0'): ['foo[x,y]', 'foo[y]>=3'],
                    ('foo', '1.0'): ['a<2; extra == "y"'],
                    ('foo', '2.0'): ['c>=2; extra == "y"'],
                },
            ),
        )

    def test_shared_requires_dist_parent(self):
        """A dependency names the extras candidate it came from.

        foo[x]==1.0 and foo[x,y]==1.0 both get bar<2 from the same
        Requires-Dist entry. q==2.0, which requires foo[x,y], is backtracked
        away, so the bar<2 in the conflict comes from foo[x]==1.0.
        """
        self.assertEqual(
            'Unable to resolve dependencies for Python 3.12, these '
            'requirements conflict:\n'
            '  bar[z]>=2 (from zz==1.0)\n'
            '  bar<2; extra == "x" (from foo[x]==1.0)',
            self._message(
                'foo[x]',
                'q',
                'r',
                files=self._files(
                    foo=('1.0',),
                    bar=('1.0', '2.0'),
                    q=('1.0', '2.0'),
                    w=('1.0',),
                    v=('1.0',),
                    r=('1.0',),
                    zz=('1.0',),
                ),
                requires={
                    ('foo', '1.0'): ['bar<2; extra == "x"'],
                    ('q', '2.0'): ['foo[x,y]', 'w'],
                    ('w', '1.0'): ['v>=5'],
                    ('r', '1.0'): ['zz'],
                    ('zz', '1.0'): ['bar[z]>=2'],
                },
            ),
        )

    def test_dependencies_not_shared(self):
        """Each candidate gets its own copies of its Requires-Dist entries."""
        provider = PyPIProvider(
            FakeIndex(self.FILES, {('foo', '1.0'): ['bar; extra == "x"']}),
            ['https://pypi.org/simple/'],
            python_version='3.12',
        )
        x = Candidate('foo', Version('1.0'), extras=['x'])
        xy = Candidate('foo', Version('1.0'), extras=['x', 'y'])
        x_deps = provider.get_dependencies(x)
        xy_deps = provider.get_dependencies(xy)
        self.assertEqual(x_deps, xy_deps)
        self.assertIsInstance(x_deps[0], BasePin)
        self.assertIsNot(x_deps[1], xy_deps[1])
        self.assertIsNot(
            provider._requires_dist_cache[('foo', Version('1.0'))][0], x_deps[1]
        )
        # A cache hit returns the same list, so the parent stays recorded
        self.assertIs(x_deps, provider.get_dependencies(x))
        for deps, candidate in ((x_deps, x), (xy_deps, xy)):
            for req in deps:
                self.assertEqual((req, candidate), provider._parents[id(req)])

    def test_reported_conflict(self):
        """A conflict's records are kept from when resolvelib reports it."""
        provider = PyPIProvider(
            FakeIndex(self.FILES, self.REQUIRES),
            ['https://pypi.org/simple/'],
            python_version='3.12',
        )
        reporter = ConflictReporter(provider)
        extras = Requirement('foo[x]>=2')
        below_two = Requirement('foo<2')
        one = Requirement('foo==1.0')
        cause = _RequirementInfo(extras, None)
        requirements = {'foo[x]': [extras], 'foo': [below_two]}
        self.assertEqual([], provider.find_matches('foo[x]', requirements, {}))
        expected = [cause, _RequirementInfo(below_two, None)]
        # Without a reported conflict the current records are used
        self.assertEqual(expected, provider.base_conflict_causes([cause]))
        reporter.resolving_conflicts([cause])
        # Backtracking finds candidates, or excludes them with something
        # else, after the conflict was reported
        requirements['foo'] = []
        self.assertTrue(provider.find_matches('foo[x]', requirements, {}))
        self.assertEqual(expected, provider.base_conflict_causes([cause]))
        requirements['foo'] = [one]
        self.assertEqual([], provider.find_matches('foo[x]', requirements, {}))
        self.assertEqual(expected, provider.base_conflict_causes([cause]))
        # A later conflict replaces it
        reporter.resolving_conflicts([cause])
        self.assertEqual(
            [cause, _RequirementInfo(one, None)],
            provider.base_conflict_causes([cause]),
        )
        requirements['foo'] = []
        provider.find_matches('foo[x]', requirements, {})
        reporter.resolving_conflicts([cause])
        self.assertEqual([cause], provider.base_conflict_causes([cause]))


class TestIsExactPin(TestCase):
    def test_is_exact_pin(self):
        def pin(text):
            return is_exact_pin(next(iter(SpecifierSet(text))))

        self.assertTrue(pin('==1.0'))
        self.assertTrue(pin('===1.0'))
        self.assertFalse(pin('==1.*'))
        self.assertFalse(pin('>=1.0'))
        self.assertFalse(pin('~=1.0'))
        self.assertFalse(pin('!=1.0'))


class TestCooldown(TestCase):
    """The cooldown dates a release by its first upload (#42)."""

    CUTOFF = NOW - timedelta(days=7)

    FILES = {
        # 1.0 is old, a wheel was added to it inside the window
        'foo': [
            index_file('foo', '1.0', days=100, sdist=True),
            index_file('foo', '1.0', days=2),
            index_file('foo', '2.0', days=2, sdist=True),
            index_file('foo', '2.0', days=2),
            # Another project's file is ignored
            index_file('other', '3.0', days=100),
        ],
        # 1.0 has only wheels; its first is incompatible with the target
        'bar': [
            index_file('bar', '1.0', days=100, tag='py2'),
            index_file('bar', '1.0', days=2),
        ],
        # 1.0 has no upload times
        'baz': [
            index_file('baz', '1.0', **{'upload-time': None}),
            index_file('baz', '0.9'),
        ],
        # 1.1 is new and preferred, but inside the window
        'qux': [index_file('qux', '1.0'), index_file('qux', '1.1', days=1)],
        'app': [index_file('app', '1.0')],
    }
    REQUIRES = {('app', '1.0'): ['qux==1.1']}

    def _provider(self, **kwargs):
        return PyPIProvider(
            FakeIndex(self.FILES, self.REQUIRES),
            ['https://pypi.org/simple/'],
            python_version='3.12',
            exclude_newer_than=self.CUTOFF,
            **kwargs,
        )

    def _files(self, name, provider=None):
        provider = provider or self._provider()
        result = provider.find_matches(name, {name: [Requirement(name)]}, {})
        return [c.package.link.filename for c in result]

    def _resolve(self, *requirements):
        resolver = Resolver(session=FakeIndex(self.FILES, self.REQUIRES))
        return {
            name: info['version']
            for name, info in resolver.resolve(
                [Requirement(r) for r in requirements],
                python_version='3.12',
                exclude_newer_than=self.CUTOFF,
            ).items()
        }

    def test_late_file_on_old_release(self):
        self.assertEqual(
            ['foo-1.0-py3-none-any.whl', 'foo-1.0.tar.gz'], self._files('foo')
        )

    def test_first_upload_of_incompatible_file(self):
        """A file that isn't compatible with the target still dates it."""
        self.assertEqual(['bar-1.0-py3-none-any.whl'], self._files('bar'))

    def test_release_without_upload_time(self):
        self.assertEqual(['baz-0.9-py3-none-any.whl'], self._files('baz'))

    def test_new_release_excluded(self):
        self.assertEqual(['qux-1.0-py3-none-any.whl'], self._files('qux'))
        self.assertEqual({'qux': '1.0'}, self._resolve('qux'))

    def test_without_cooldown(self):
        provider = self._provider()
        provider.exclude_newer_than = None
        self.assertEqual(
            ['qux-1.1-py3-none-any.whl', 'qux-1.0-py3-none-any.whl'],
            self._files('qux', provider),
        )

    def test_first_upload(self):
        provider = self._provider()
        self._files('foo', provider)
        self.assertEqual(
            NOW - timedelta(days=100),
            provider.finder.first_upload('Foo', Version('1.0')),
        )
        self.assertEqual(
            NOW - timedelta(days=2),
            provider.finder.first_upload('foo', Version('2.0')),
        )
        self.assertIsNone(provider.finder.first_upload('foo', Version('3.0')))
        self.assertIsNone(provider.finder.first_upload('other', Version('3.0')))

    def test_project_pin_inside_window(self):
        for spec in ('qux==1.1', 'qux===1.1', 'qux[x]==1.1', 'qux==1.1.0'):
            with self.subTest(spec=spec):
                with self.assertLogs('proviso.resolver', level='INFO') as logs:
                    self.assertEqual({'qux': '1.1'}, self._resolve(spec))
                self.assertIn(
                    'INFO:proviso.resolver:qux==1.1 is within the dependency '
                    'cooldown; using it because the project pins it exactly',
                    logs.output,
                )

    def test_project_pin_without_upload_time(self):
        self.assertEqual({'baz': '1.0'}, self._resolve('baz==1.0'))

    def test_project_pin_logged_once_per_run(self):
        resolver = Resolver(session=FakeIndex(self.FILES, self.REQUIRES))
        with self.assertLogs('proviso.resolver', level='INFO') as logs:
            for python_version in ('3.12', '3.13'):
                resolver.resolve(
                    [Requirement('qux==1.1')],
                    python_version=python_version,
                    exclude_newer_than=self.CUTOFF,
                )
        self.assertEqual(
            1, len([line for line in logs.output if 'cooldown' in line])
        )

    def test_project_pin_not_exact(self):
        for spec in ('qux>=1.1', 'qux~=1.1', 'qux==1.1.*'):
            with self.subTest(spec=spec):
                with self.assertRaises(ResolutionImpossible):
                    self._resolve(spec)
        self.assertEqual({'qux': '1.0'}, self._resolve('qux==1.*'))

    def test_project_pin_on_another_package(self):
        provider = self._provider(
            project_requirements=[Requirement('foo==1.1')]
        )
        self.assertEqual(
            ['qux-1.0-py3-none-any.whl'], self._files('qux', provider)
        )

    def test_transitive_pin_inside_window(self):
        with self.assertRaises(ResolutionImpossible):
            self._resolve('app')

    def test_extras_base_pin_inside_window(self):
        """The pin an extras candidate adds on its base isn't exempt."""
        provider = self._provider()
        requirements = {
            'qux': [BasePin('qux==1.1')],
            'qux[x]': [Requirement('qux[x]')],
        }
        self.assertEqual([], provider.find_matches('qux', requirements, {}))


class TestCooldownWithoutUploadTimes(TestCase):
    """An index that gives no upload times is reported once per run."""

    CUTOFF = NOW - timedelta(days=7)
    JSON_INDEX = 'https://pypi.org/simple/'
    HTML_INDEX = 'https://html.example.com/simple/'

    FILES = {
        'foo': [index_file('foo', '1.0'), index_file('foo', '2.0')],
        'bar': [index_file('bar', '1.0')],
        # Only 1.0 lacks an upload time
        'baz': [
            index_file('baz', '1.0', **{'upload-time': None}),
            index_file('baz', '0.9'),
        ],
    }

    def _session(self):
        return FakeIndex(self.FILES, html_hosts=('html.example.com',))

    def _provider(self, index_urls=None, **kwargs):
        kwargs.setdefault('exclude_newer_than', self.CUTOFF)
        return PyPIProvider(
            self._session(),
            index_urls or [self.HTML_INDEX],
            python_version='3.12',
            **kwargs,
        )

    def _versions(self, provider, name):
        result = provider.find_matches(name, {name: [Requirement(name)]}, {})
        return [str(c.version) for c in result]

    def _warnings(self, logs):
        return [line for line in logs.output if 'no upload times' in line]

    def test_warning(self):
        with self.assertLogs('proviso.resolver', level='WARNING') as logs:
            self.assertEqual([], self._versions(self._provider(), 'foo'))
        self.assertEqual(
            [
                'WARNING:proviso.resolver:https://html.example.com/simple/ '
                'provided no upload times for foo, which happens when an '
                'index only serves the HTML "simple" API and not the JSON '
                "API; its releases can't be dated, so the dependency "
                'cooldown excludes all of them. Other packages may be '
                'affected too; --cooldown-days 0 disables the cooldown'
            ],
            logs.output,
        )

    def test_credentials_redacted(self):
        provider = self._provider(
            ['https://user:secret@html.example.com/simple/']
        )
        with self.assertLogs('proviso.resolver', level='WARNING') as logs:
            self._versions(provider, 'foo')
        self.assertIn('***@html.example.com/simple/ provided', logs.output[0])
        self.assertNotIn('secret', logs.output[0])

    def test_once_per_run(self):
        resolver = Resolver(
            index_urls=[self.HTML_INDEX], session=self._session()
        )
        with self.assertLogs('proviso.resolver', level='WARNING') as logs:
            for python_version in ('3.12', '3.13'):
                for name in ('foo', 'bar'):
                    with self.assertRaises(ResolutionImpossible):
                        resolver.resolve(
                            [Requirement(name)],
                            python_version=python_version,
                            exclude_newer_than=self.CUTOFF,
                        )
        warnings = self._warnings(logs)
        self.assertEqual(1, len(warnings))
        self.assertIn('no upload times for foo', warnings[0])

    def test_once_per_index(self):
        """Only the HTML index is reported when it's mixed with a JSON one.

        Both serve the same files.  foo's releases are dated by the JSON
        index, baz 1.0 has no upload time on either.
        """
        resolver = Resolver(
            index_urls=[self.JSON_INDEX, self.HTML_INDEX],
            session=self._session(),
        )
        with self.assertLogs('proviso.resolver', level='WARNING') as logs:
            for name in ('foo', 'baz'):
                resolver.resolve(
                    [Requirement(name)],
                    python_version='3.12',
                    exclude_newer_than=self.CUTOFF,
                )
        warnings = self._warnings(logs)
        self.assertEqual(1, len(warnings))
        self.assertIn(
            f'{self.HTML_INDEX} provided no upload times for baz', warnings[0]
        )

    def test_project_pin(self):
        """A pin is still exempt, other releases are excluded and reported."""
        provider = self._provider(
            project_requirements=[Requirement('foo==1.0')]
        )
        with self.assertLogs('proviso.resolver', level='INFO') as logs:
            self.assertEqual(['1.0'], self._versions(provider, 'foo'))
        self.assertEqual(1, len(self._warnings(logs)))

    def test_project_pin_only_release(self):
        """Nothing is excluded when the pin is the only release."""
        provider = self._provider(
            project_requirements=[Requirement('bar==1.0')]
        )
        with self.assertLogs('proviso.resolver', level='INFO') as logs:
            self.assertEqual(['1.0'], self._versions(provider, 'bar'))
        self.assertEqual([], self._warnings(logs))

    def test_some_upload_times(self):
        provider = self._provider([self.JSON_INDEX])
        with self.assertNoLogs('proviso.resolver', level='WARNING'):
            self.assertEqual(['0.9'], self._versions(provider, 'baz'))

    def test_no_files(self):
        with self.assertNoLogs('proviso.resolver', level='WARNING'):
            self.assertEqual([], self._versions(self._provider(), 'missing'))

    def test_without_cooldown(self):
        provider = self._provider(exclude_newer_than=None)
        with self.assertNoLogs('proviso.resolver', level='WARNING'):
            self.assertEqual(['2.0', '1.0'], self._versions(provider, 'foo'))
