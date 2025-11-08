from operator import attrgetter

import httpx
from packaging.metadata import Metadata
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version
from resolvelib import AbstractProvider, BaseReporter, Resolver as ResolveLibResolver
from unearth import PackageFinder


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

    def __init__(self, finder):
        self.finder = finder
        self._dependencies_cache = {}

    def identify(self, requirement_or_candidate):
        """Return the package name as the identifier."""
        if isinstance(requirement_or_candidate, Requirement):
            return canonicalize_name(requirement_or_candidate.name)
        return requirement_or_candidate.name

    def get_preference(self, identifier, resolutions, candidates, information, backtrack_causes):
        """Return preference for resolving this requirement."""
        # Simpler preference: prefer already resolved packages, then by number of candidates
        return (identifier not in resolutions, len(list(candidates.get(identifier, []))))

    def find_matches(self, identifier, requirements, incompatibilities):
        """Find all candidates that match the given requirements."""
        # Get all requirements for this identifier
        reqs = list(requirements.get(identifier, []))
        if not reqs:
            return []

        # Use the first requirement to search (they should all have the same name)
        req = reqs[0]

        # Find best match using unearth
        result = self.finder.find_matches(str(req))

        # Get all applicable versions
        candidates = []
        for package in result:
            version = Version(package.version)

            # Check if this version satisfies all requirements
            if all(version in r.specifier for r in reqs):
                # Check if it's not in incompatibilities
                if version not in [c.version for c in incompatibilities.get(identifier, [])]:
                    candidates.append(Candidate(identifier, version))

        # Return candidates sorted by version (newest first)
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
            return self._dependencies_cache[cache_key]

        # Find the package to get its metadata
        result = self.finder.find_best_match(f"{candidate.name}=={candidate.version}")

        if not result.best:
            self._dependencies_cache[cache_key] = []
            return []

        package = result.best

        # Fetch metadata from dist_info_link if available
        if package.link.dist_info_link:
            response = httpx.get(package.link.dist_info_link.url)
            # Disable validation to handle metadata version mismatches
            metadata = Metadata.from_email(response.text, validate=False)
        else:
            # Fallback: no metadata available
            self._dependencies_cache[cache_key] = []
            return []

        # Extract dependencies from metadata
        dependencies = []
        for req in metadata.requires_dist or []:
            # Evaluate markers for current environment
            if req.marker is None or req.marker.evaluate():
                dependencies.append(req)

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

        self.finder = PackageFinder(index_urls=index_urls)

    def resolve(self, requirements):
        """Resolve dependencies for the given requirements.

        Args:
            requirements: List of packaging.requirements.Requirement objects

        Returns:
            Dict mapping package names to metadata dicts with 'version' and 'extras'

        Raises:
            resolvelib.ResolutionImpossible: If resolution fails
        """
        provider = PyPIProvider(self.finder)
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
