from logging import getLogger
from operator import attrgetter

import httpx
from packaging.markers import default_environment
from packaging.metadata import Metadata
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version
from resolvelib import AbstractProvider, BaseReporter
from resolvelib import Resolver as ResolveLibResolver
from unearth import PackageFinder, TargetPython
from unearth.auth import MultiDomainBasicAuth

from .utils import CachingClient, format_python_version_for_markers

log = getLogger('proviso.resolver')


class Candidate:
    """Represents a concrete package version."""

    def __init__(self, name, version, extras=None, dependencies=None):
        self.name = canonicalize_name(name)
        self.version = version
        self.extras = extras or frozenset()
        self._dependencies = dependencies

    def __repr__(self):
        return f"Candidate({self.name!r}, {self.version!r})"

    def __eq__(self, other):
        if not isinstance(other, Candidate):
            return NotImplemented
        return (
            self.name == other.name
            and self.version == other.version
            and self.extras == other.extras
        )

    def __hash__(self):
        return hash((self.name, self.version, self.extras))


class PyPIProvider(AbstractProvider):
    """Provider that queries PyPI for packages and their dependencies."""

    def __init__(self, session, index_urls, python_version=None):
        self.session = session
        self._dependencies_cache = {}

        # Create TargetPython if a specific version is requested
        target_python = None
        if python_version:
            target_python = TargetPython(py_ver=Version(python_version).release)

        # Create PackageFinder with the target Python version
        self.finder = PackageFinder(
            session=session, index_urls=index_urls, target_python=target_python
        )

        # Build environment for marker evaluation
        if python_version:
            # Start with default environment and override Python version
            version_info_dict = format_python_version_for_markers(
                python_version
            )

            self.environment = default_environment()
            self.environment.update(version_info_dict)
        else:
            # Use current environment
            self.environment = None

    def identify(self, requirement_or_candidate):
        """Return the package name as the identifier."""
        if isinstance(requirement_or_candidate, Requirement):
            return canonicalize_name(requirement_or_candidate.name)
        return requirement_or_candidate.name

    def get_preference(
        self, identifier, resolutions, candidates, information, backtrack_causes
    ):
        """Return preference for resolving this requirement."""
        # Simpler preference: prefer already resolved packages, then by number of candidates
        return (
            identifier not in resolutions,
            len(list(candidates.get(identifier, []))),
        )

    def find_matches(self, identifier, requirements, incompatibilities):
        """Find all candidates that match the given requirements."""
        # Get all requirements for this identifier
        reqs = list(requirements.get(identifier, []))
        if not reqs:
            return []

        # Use the first requirement to search (they should all have the same name)
        req = reqs[0]

        log.debug(f'Finding matches for {identifier}: {req}')

        # Find best match using unearth
        result = self.finder.find_matches(str(req))

        # Get all applicable versions
        candidates = []
        for package in result:
            version = Version(package.version)

            # Check if this version satisfies all requirements
            if all(version in r.specifier for r in reqs):
                # Check if it's not in incompatibilities
                if version not in [
                    c.version for c in incompatibilities.get(identifier, [])
                ]:
                    candidates.append(Candidate(identifier, version))

        # Return candidates sorted by version (newest first)
        log.debug(f'Found {len(candidates)} candidates for {identifier}')
        return sorted(candidates, key=attrgetter('version'), reverse=True)

    def is_satisfied_by(self, requirement, candidate):
        """Check if the candidate satisfies the requirement."""
        if canonicalize_name(requirement.name) != candidate.name:
            return False
        return candidate.version in requirement.specifier

    def get_dependencies(self, candidate):
        """Get dependencies for a candidate."""
        cache_key = (candidate.name, candidate.version)

        if cache_key in self._dependencies_cache:
            log.debug(
                f'Dependencies cache hit for {candidate.name}=={candidate.version}'
            )
            return self._dependencies_cache[cache_key]

        log.debug(
            f'Getting dependencies for {candidate.name}=={candidate.version}'
        )

        # Find the package to get its metadata
        result = self.finder.find_best_match(
            f"{candidate.name}=={candidate.version}"
        )

        if not result.best:
            log.warning(
                f'No matching package found for {candidate.name}=={candidate.version}'
            )
            self._dependencies_cache[cache_key] = []
            return []

        package = result.best

        # Fetch metadata from dist_info_link if available
        if package.link.dist_info_link:
            url = package.link.dist_info_link.url

            # Fetch (session caches if it's a CachingClient)
            if self.session:
                response = self.session.get(url)
            else:
                response = httpx.get(url)

            # Disable validation to handle metadata version mismatches
            metadata = Metadata.from_email(response.text, validate=False)
        else:
            # Fallback: no metadata available
            self._dependencies_cache[cache_key] = []
            return []

        # Extract dependencies from metadata
        dependencies = []
        for req in metadata.requires_dist or []:
            # Evaluate markers for target environment
            if req.marker is None:
                # No marker means always included
                dependencies.append(req)
            elif self.environment is None:
                # No custom environment, use default
                if req.marker.evaluate():
                    dependencies.append(req)
            else:
                # Use custom environment for evaluation
                if req.marker.evaluate(environment=self.environment):
                    dependencies.append(req)

        log.debug(
            f'Found {len(dependencies)} dependencies for {candidate.name}=={candidate.version}'
        )
        self._dependencies_cache[cache_key] = dependencies
        return dependencies


class Resolver:
    """Resolves package dependencies using PyPI."""

    def __init__(self, index_urls=None):
        """Initialize the resolver.

        Args:
            index_urls: List of package index URLs. Defaults to PyPI.
        """
        if index_urls is None:
            index_urls = ['https://pypi.org/simple/']

        self.index_urls = index_urls

        # Create cached HTTP client shared across all resolutions
        self._session = CachingClient()
        self._session.auth = MultiDomainBasicAuth(index_urls=index_urls)

    def resolve(self, requirements, python_version=None):
        """Resolve dependencies for the given requirements.

        Args:
            requirements: List of packaging.requirements.Requirement objects
            python_version: Target Python version string (e.g., "3.9", "3.10.5").
                          Defaults to current Python version.

        Returns:
            Dict mapping package names to metadata dicts with 'version' and 'extras'

        Raises:
            resolvelib.ResolutionImpossible: If resolution fails
        """
        provider = PyPIProvider(
            session=self._session,
            index_urls=self.index_urls,
            python_version=python_version,
        )
        reporter = BaseReporter()
        resolver = ResolveLibResolver(provider, reporter)

        # Resolve
        result = resolver.resolve(requirements)

        # Convert result to our format
        resolved = {}
        for identifier, candidate in result.mapping.items():
            resolved[identifier] = {
                'version': str(candidate.version),
                'extras': list(candidate.extras),
            }

        return resolved
