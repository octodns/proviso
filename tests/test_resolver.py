import math
from collections import namedtuple
from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import MagicMock, patch

from packaging.requirements import Requirement
from packaging.version import Version

from proviso.resolver import (
    _CONFLICT_PRIORITY_THRESHOLD,
    Candidate,
    PyPIProvider,
    Resolver,
)

# Lightweight stand-in for resolvelib's RequirementInformation namedtuple.
# resolvelib defines RequirementInformation(requirement, parent); we use the
# same field names so tests read naturally.
_RequirementInfo = namedtuple('_RequirementInfo', ['requirement', 'parent'])


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

        with patch('proviso.resolver.PackageFinder') as mock_finder:
            provider = PyPIProvider(session, index_urls)

            # Verify PackageFinder was created correctly
            mock_finder.assert_called_once_with(
                session=session,
                index_urls=index_urls,
                target_python=None,
                exclude_newer_than=None,
            )

            # Environment should be None when no python_version specified
            self.assertIsNone(provider.environment)

    def test_init_with_python_version(self):
        """Test PyPIProvider initialization with python_version."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']
        python_version = '3.10'

        with patch('proviso.resolver.PackageFinder'):
            with patch('proviso.resolver.TargetPython') as mock_target_python:
                with patch('proviso.resolver.default_environment') as mock_env:
                    mock_env.return_value = {'some': 'env'}

                    provider = PyPIProvider(session, index_urls, python_version)

                    # Verify TargetPython was created with correct version
                    mock_target_python.assert_called_once()
                    call_kwargs = mock_target_python.call_args[1]
                    self.assertEqual((3, 10), call_kwargs['py_ver'])

                    # Environment should be updated with Python version info
                    self.assertIsNotNone(provider.environment)
                    self.assertEqual(
                        '3.10', provider.environment['python_version']
                    )
                    self.assertEqual(
                        '3.10.0', provider.environment['python_full_version']
                    )

    def test_identify_with_requirement(self):
        """Test identify method with a Requirement."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.PackageFinder') as mock_finder:
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

        with patch('proviso.resolver.PackageFinder'):
            provider = PyPIProvider(session, index_urls)

            candidate = Candidate('my-package', Version('1.0.0'))
            identifier = provider.identify(candidate)

            self.assertEqual('my-package', identifier)

    def test_is_satisfied_by_matching(self):
        """Test is_satisfied_by with matching candidate."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.PackageFinder'):
            provider = PyPIProvider(session, index_urls)

            req = Requirement('requests>=2.0.0,<3.0.0')
            candidate = Candidate('requests', Version('2.28.0'))

            self.assertTrue(provider.is_satisfied_by(req, candidate))

    def test_is_satisfied_by_not_matching_version(self):
        """Test is_satisfied_by with non-matching version."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.PackageFinder'):
            provider = PyPIProvider(session, index_urls)

            req = Requirement('requests>=2.0.0,<3.0.0')
            candidate = Candidate('requests', Version('1.0.0'))

            self.assertFalse(provider.is_satisfied_by(req, candidate))

    def test_is_satisfied_by_different_package(self):
        """Test is_satisfied_by with different package name."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.PackageFinder'):
            provider = PyPIProvider(session, index_urls)

            req = Requirement('requests>=2.0.0')
            candidate = Candidate('urllib3', Version('2.0.0'))

            self.assertFalse(provider.is_satisfied_by(req, candidate))

    def test_get_preference_free_requirement(self):
        """A free (no specifier) requirement returns the highest-value tuple."""
        with patch('proviso.resolver.PackageFinder'):
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
        with patch('proviso.resolver.PackageFinder'):
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
        with patch('proviso.resolver.PackageFinder'):
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
        with patch('proviso.resolver.PackageFinder'):
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
        with patch('proviso.resolver.PackageFinder'):
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
        with patch('proviso.resolver.PackageFinder'):
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

        with patch('proviso.resolver.PackageFinder'):
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

        with patch('proviso.resolver.PackageFinder') as mock_finder_class:
            mock_finder = MagicMock()
            mock_finder_class.return_value = mock_finder

            provider = PyPIProvider(session, index_urls)

            # Mock package results
            mock_package1 = MagicMock()
            mock_package1.version = '2.28.0'
            mock_package2 = MagicMock()
            mock_package2.version = '2.27.0'
            mock_package3 = MagicMock()
            mock_package3.version = '2.26.0'

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

        with patch('proviso.resolver.PackageFinder') as mock_finder_class:
            mock_finder = MagicMock()
            mock_finder_class.return_value = mock_finder

            provider = PyPIProvider(session, index_urls)

            mock_package1 = MagicMock()
            mock_package1.version = '2.28.0'
            mock_package2 = MagicMock()
            mock_package2.version = '2.27.0'

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

        with patch('proviso.resolver.PackageFinder') as mock_finder_class:
            mock_finder = MagicMock()
            mock_finder_class.return_value = mock_finder

            provider = PyPIProvider(session, index_urls)

            mock_package1 = MagicMock()
            mock_package1.version = '2.28.0'
            mock_package2 = MagicMock()
            mock_package2.version = '2.27.0'

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

        with patch('proviso.resolver.PackageFinder') as mock_finder_class:
            mock_finder = MagicMock()
            mock_finder_class.return_value = mock_finder

            provider = PyPIProvider(session, index_urls)
            mock_finder.find_matches.return_value = []

            provider.find_matches(
                'requests', {'requests': [Requirement('requests>=2.27.0')]}, {}
            )

            # Should be called with the bare identifier, not a specifier string
            mock_finder.find_matches.assert_called_once_with('requests')

    def test_find_matches_incompatibility_filtering_applied_per_call(self):
        """Test that incompatibility filtering is applied on each call, not cached,
        so backtracking correctly excludes previously tried versions."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.PackageFinder') as mock_finder_class:
            mock_finder = MagicMock()
            mock_finder_class.return_value = mock_finder

            provider = PyPIProvider(session, index_urls)

            mock_package1 = MagicMock()
            mock_package1.version = '2.28.0'
            mock_package2 = MagicMock()
            mock_package2.version = '2.27.0'

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

        with patch('proviso.resolver.PackageFinder'):
            provider = PyPIProvider(session, index_urls)

            candidate = Candidate('requests', Version('2.28.0'))
            cached_deps = [Requirement('urllib3>=1.26.0')]

            # Pre-populate cache
            provider._dependencies_cache[
                ('requests', Version('2.28.0'), frozenset())
            ] = cached_deps

            result = provider.get_dependencies(candidate)

            self.assertEqual(cached_deps, result)

    def test_get_dependencies_no_package_found(self):
        """Test get_dependencies when no package is found."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.PackageFinder') as mock_finder_class:
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

        with patch('proviso.resolver.PackageFinder') as mock_finder_class:
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

    def test_get_dependencies_with_markers(self):
        """Test get_dependencies evaluates markers correctly."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.PackageFinder') as mock_finder_class:
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
        """Test get_dependencies when no dist_info_link is available."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.PackageFinder') as mock_finder_class:
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

            result = provider.get_dependencies(candidate)

            self.assertEqual([], result)

    def test_get_dependencies_with_marker_default_environment(self):
        """Test get_dependencies with marker evaluation in default environment."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.PackageFinder') as mock_finder_class:
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

        with patch('proviso.resolver.PackageFinder') as mock_finder_class:
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

        with patch('proviso.resolver.PackageFinder') as mock_finder_class:
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

        with patch('proviso.resolver.PackageFinder') as mock_finder_class:
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

            import httpx

            def raise_for_status():
                if 400 <= mock_response.status_code < 600:
                    raise httpx.HTTPStatusError(
                        message="Client Error",
                        request=MagicMock(),
                        response=mock_response,
                    )

            mock_response.raise_for_status.side_effect = raise_for_status
            session.get.return_value = mock_response

            candidate = Candidate('requests', Version('2.28.0'))

            with self.assertRaises(httpx.HTTPStatusError):
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


class TestPyPIProviderExcludeNewerThan(TestCase):
    def test_init_with_exclude_newer_than(self):
        """Test PyPIProvider initialization with exclude_newer_than."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']
        exclude_newer_than = datetime(2024, 1, 1, tzinfo=timezone.utc)

        with patch('proviso.resolver.PackageFinder') as mock_finder:
            PyPIProvider(
                session, index_urls, exclude_newer_than=exclude_newer_than
            )

            # Verify PackageFinder was created with exclude_newer_than
            mock_finder.assert_called_once()
            call_kwargs = mock_finder.call_args[1]
            self.assertEqual(
                exclude_newer_than, call_kwargs['exclude_newer_than']
            )

    def test_init_without_exclude_newer_than(self):
        """Test PyPIProvider initialization without exclude_newer_than."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.PackageFinder') as mock_finder:
            PyPIProvider(session, index_urls)

            # Verify PackageFinder was created with exclude_newer_than=None
            mock_finder.assert_called_once()
            call_kwargs = mock_finder.call_args[1]
            self.assertIsNone(call_kwargs['exclude_newer_than'])


class TestPyPIProviderExtrasBug(TestCase):
    def test_get_dependencies_with_extras_repro(self):
        """Test that get_dependencies correctly handles extras."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.PackageFinder') as mock_finder_class:
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

        with patch('proviso.resolver.PackageFinder') as mock_finder_class:
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
        with patch('proviso.resolver.PackageFinder'):
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

    def _make_provider_with_packages(self, versions):
        """Helper: creates a provider whose finder returns packages at given versions."""
        session = MagicMock()
        with patch('proviso.resolver.PackageFinder') as mock_finder_class:
            mock_finder = MagicMock()
            mock_finder_class.return_value = mock_finder

            provider = PyPIProvider(session, ['https://pypi.org/simple/'])

            packages = []
            for v in versions:
                pkg = MagicMock()
                pkg.version = v
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


class TestGetDependenciesPackageFastPath(TestCase):
    """Tests for the get_dependencies fast path that uses candidate.package."""

    def test_uses_candidate_package_skips_find_best_match(self):
        """get_dependencies uses candidate.package directly, avoiding find_best_match."""
        session = MagicMock()

        with patch('proviso.resolver.PackageFinder') as mock_finder_class:
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

        with patch('proviso.resolver.PackageFinder') as mock_finder_class:
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
