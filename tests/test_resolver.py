from unittest import TestCase
from unittest.mock import MagicMock, patch

from packaging.requirements import Requirement
from packaging.version import Version

from proviso.resolver import Candidate, PyPIProvider, Resolver


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
                session=session, index_urls=index_urls, target_python=None
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

    def test_get_preference(self):
        """Test get_preference method."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.PackageFinder'):
            provider = PyPIProvider(session, index_urls)

            resolutions = {'package-a': MagicMock()}
            candidates = {
                'package-b': [MagicMock(), MagicMock()],
                'package-c': [MagicMock()],
            }

            # Package already resolved should have lower preference
            pref_a = provider.get_preference(
                'package-a', resolutions, candidates, None, None
            )
            self.assertEqual((False, 0), pref_a)

            # Unresolved packages
            pref_b = provider.get_preference(
                'package-b', resolutions, candidates, None, None
            )
            self.assertEqual((True, 2), pref_b)

            pref_c = provider.get_preference(
                'package-c', resolutions, candidates, None, None
            )
            self.assertEqual((True, 1), pref_c)

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

    def test_get_dependencies_cache_hit(self):
        """Test get_dependencies returns cached dependencies."""
        session = MagicMock()
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.PackageFinder'):
            provider = PyPIProvider(session, index_urls)

            candidate = Candidate('requests', Version('2.28.0'))
            cached_deps = [Requirement('urllib3>=1.26.0')]

            # Pre-populate cache
            provider._dependencies_cache[('requests', Version('2.28.0'))] = (
                cached_deps
            )

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
                ('nonexistent', Version('1.0.0')), provider._dependencies_cache
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

    def test_get_dependencies_no_session(self):
        """Test get_dependencies when session is None (uses httpx.get)."""
        index_urls = ['https://pypi.org/simple/']

        with patch('proviso.resolver.PackageFinder') as mock_finder_class:
            mock_finder = MagicMock()
            mock_finder_class.return_value = mock_finder

            # Pass None for session
            provider = PyPIProvider(None, index_urls)

            mock_package = MagicMock()
            mock_link = MagicMock()
            mock_dist_info_link = MagicMock()
            mock_dist_info_link.url = 'https://pypi.org/metadata'
            mock_link.dist_info_link = mock_dist_info_link
            mock_package.link = mock_link

            mock_result = MagicMock()
            mock_result.best = mock_package
            mock_finder.find_best_match.return_value = mock_result

            with patch('proviso.resolver.httpx') as mock_httpx:
                mock_response = MagicMock()
                mock_response.text = '''Metadata-Version: 2.1
Name: test-package
Version: 1.0.0
Requires-Dist: urllib3>=1.26.0
'''
                mock_httpx.get.return_value = mock_response

                candidate = Candidate('test-package', Version('1.0.0'))

                result = provider.get_dependencies(candidate)

                # Should have called httpx.get
                mock_httpx.get.assert_called_once_with(
                    'https://pypi.org/metadata'
                )
                self.assertEqual(1, len(result))

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
