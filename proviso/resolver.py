import math
from collections import defaultdict
from logging import getLogger
from operator import attrgetter

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

# Mirrors pip's _CONFLICT_PRIORITY_THRESHOLD: after this many rounds where a
# package appears unresolved in a conflict, it gets promoted so its constraints
# take effect before other packages pick a version.
_CONFLICT_PRIORITY_THRESHOLD = 5


class Candidate:
    """Represents a concrete package version."""

    def __init__(
        self, name, version, extras=None, dependencies=None, package=None
    ):
        self.name = canonicalize_name(name)
        self.version = version
        self.extras = extras or frozenset()
        self._dependencies = dependencies
        # unearth package object carried from find_matches; avoids a redundant
        # find_best_match lookup in get_dependencies when present.
        self.package = package

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

    def __init__(
        self,
        session,
        index_urls,
        python_version=None,
        exclude_newer_than=None,
        user_requested=None,
    ):
        self.session = session
        self._dependencies_cache = {}
        self._matches_cache = {}
        # Conflict-promotion state (mirrors pip's PipProvider)
        self._conflict_counts = defaultdict(int)
        self._conflict_promoted = set()
        # Ordering of user-specified top-level requirements (name → index)
        self._user_requested = user_requested or {}

        # Create TargetPython if a specific version is requested
        target_python = None
        if python_version:
            target_python = TargetPython(py_ver=Version(python_version).release)

        # Create PackageFinder with the target Python version and optional date filter
        self.finder = PackageFinder(
            session=session,
            index_urls=index_urls,
            target_python=target_python,
            exclude_newer_than=exclude_newer_than,
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

    def narrow_requirement_selection(
        self,
        identifiers,
        resolutions,
        candidates,
        information,
        backtrack_causes,
    ):
        """Narrow the set of requirement identifiers to resolve next.

        Mirrors pip's PipProvider.narrow_requirement_selection:

        * Packages that are the current source of a conflict (backtrack causes)
          are returned first so the engine resolves them before moving on.
        * Packages that repeatedly appear in conflicts get promoted so their
          constraints take effect before other packages pick a version.
        """
        backtrack_identifiers = set()
        for info in backtrack_causes:
            names = [canonicalize_name(info.requirement.name)]
            if info.parent is not None:
                names.append(
                    info.parent.name
                )  # Candidate.name is already canonical
            for name in names:
                backtrack_identifiers.add(name)
                if name not in resolutions:
                    self._conflict_counts[name] += 1
                    if (
                        self._conflict_counts[name]
                        >= _CONFLICT_PRIORITY_THRESHOLD
                    ):
                        self._conflict_promoted.add(name)

        current_backtrack_causes = []
        promoted = []
        for identifier in identifiers:
            if identifier in backtrack_identifiers:
                current_backtrack_causes.append(identifier)
                continue
            if identifier in self._conflict_promoted:
                promoted.append(identifier)
                continue

        if current_backtrack_causes:
            return current_backtrack_causes

        if promoted:
            return promoted

        return identifiers

    def get_preference(
        self, identifier, resolutions, candidates, information, backtrack_causes
    ):
        """Return sort key for the given requirement based on preference.

        Mirrors pip's PipProvider.get_preference.  The 7-tuple is ordered so
        that lower values are resolved first:

          1. Conflict-promoted packages first.
          2. Direct (URL) requirements first.
          3. Pinned (==x.y.z) requirements first — already fully constrained.
          4. Upper-bounded requirements first — rules out infeasible candidates
             sooner when the resolver tries newest versions first.
          5. User-specified ordering (index in the top-level requirement list).
          6. Non-free (has any specifier) requirements first.
          7. Alphabetical for stability / debuggability.
        """
        operators = []
        direct = False
        for info in information.get(identifier, []):
            req = info.requirement
            if req.url is not None:
                direct = True
            for spec in req.specifier:
                operators.append((spec.operator, spec.version))

        pinned = any(op == '==' and '*' not in ver for op, ver in operators)
        upper_bounded = any(
            op in ('<', '<=', '~=') or (op == '==' and '*' in ver)
            for op, ver in operators
        )
        unfree = bool(operators)
        requested_order = self._user_requested.get(identifier, math.inf)
        conflict_promoted = identifier in self._conflict_promoted

        return (
            not conflict_promoted,
            not direct,
            not pinned,
            not upper_bounded,
            requested_order,
            not unfree,
            identifier,
        )

    def find_matches(self, identifier, requirements, incompatibilities):
        """Find all candidates that match the given requirements."""
        # Get all requirements for this identifier
        reqs = list(requirements.get(identifier, []))
        if not reqs:
            return []

        # Fetch (and cache) all platform-compatible packages for this name.
        # The specifier/incompatibility filtering below is kept outside the cache
        # because `incompatibilities` changes on every backtracking round.
        if identifier in self._matches_cache:
            log.debug(f'Matches cache hit for {identifier}')
            result = self._matches_cache[identifier]
        else:
            log.debug(f'Finding matches for {identifier}: {reqs[0]}')
            # Query by name only so one cache entry covers all specifiers seen
            # during resolution.  unearth returns a lazy LazySequence so we
            # materialise it immediately before caching.
            result = list(self.finder.find_matches(identifier))
            self._matches_cache[identifier] = result

        # Determine whether pre-releases are allowed: pip permits them only when
        # a specifier in the requirement set explicitly references pre-releases
        # (e.g. ">=1.0a1").  SpecifierSet.prereleases returns True in that case.
        allow_prereleases = any(r.specifier.prereleases for r in reqs)

        # Collect incompatible versions for fast O(1) membership tests.
        incompat_versions = {
            c.version for c in incompatibilities.get(identifier, [])
        }

        # Build the filtered, sorted candidate list.
        candidates = []
        for package in result:
            version = Version(package.version)

            # Exclude pre-releases unless a specifier explicitly allows them.
            if version.is_prerelease and not allow_prereleases:
                continue

            # Check if this version satisfies all requirements.
            if not all(version in r.specifier for r in reqs):
                continue

            # Skip versions that are known incompatible from prior backtracking.
            if version in incompat_versions:
                continue

            # Carry the unearth package object so get_dependencies can use it
            # directly without a redundant find_best_match lookup.
            candidates.append(Candidate(identifier, version, package=package))

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
        cache_key = (candidate.name, candidate.version, candidate.extras)

        if cache_key in self._dependencies_cache:
            log.debug(
                f'Dependencies cache hit for {candidate.name}=={candidate.version} extras={candidate.extras}'
            )
            return self._dependencies_cache[cache_key]

        log.debug(
            f'Getting dependencies for {candidate.name}=={candidate.version} extras={candidate.extras}'
        )

        # Use the unearth package object carried from find_matches to avoid a
        # redundant index lookup.  Fall back to find_best_match for candidates
        # not built via find_matches (e.g. synthesised in tests).
        package = candidate.package
        if package is None:
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

            # Fetch using cached session
            response = self.session.get(url)
            response.raise_for_status()

            # Disable validation to handle metadata version mismatches
            metadata = Metadata.from_email(response.text, validate=False)
        else:
            # Fallback: no metadata available
            self._dependencies_cache[cache_key] = []
            return []

        # Extract dependencies from metadata
        dependencies = []
        for req in metadata.requires_dist or []:
            if req.marker is None:
                # No marker means always included
                dependencies.append(req)
                continue

            # Prepare environment for evaluation
            # Use custom environment if set, otherwise default
            base_env = (self.environment or default_environment()).copy()

            # Check if requirement is valid for base environment (no extra)
            # We explicitly set extra to empty string to handle markers that might reference it
            base_env['extra'] = ''
            if req.marker.evaluate(environment=base_env):
                dependencies.append(req)
                continue

            # If not matched yet, check against requested extras
            for extra in candidate.extras:
                env = base_env.copy()
                env['extra'] = extra
                if req.marker.evaluate(environment=env):
                    dependencies.append(req)
                    break

        log.debug(
            f'Found {len(dependencies)} dependencies for {candidate.name}=={candidate.version}'
        )
        self._dependencies_cache[cache_key] = dependencies
        return dependencies


class Resolver:
    """Resolves package dependencies using PyPI."""

    def __init__(self, index_urls=None, session=None):
        """Initialize the resolver.

        Args:
            index_urls: List of package index URLs. Defaults to PyPI.
            session: Optional httpx.Client for making requests. If None, creates a new CachingClient.
        """
        if index_urls is None:
            index_urls = ['https://pypi.org/simple/']

        self.index_urls = index_urls

        # Use provided session or create a new CachingClient
        if session is None:
            session = CachingClient()
        self._session = session
        self._session.auth = MultiDomainBasicAuth(index_urls=index_urls)

    def resolve(
        self, requirements, python_version=None, exclude_newer_than=None
    ):
        """Resolve dependencies for the given requirements.

        Args:
            requirements: List of packaging.requirements.Requirement objects
            python_version: Target Python version string (e.g., "3.9", "3.10.5").
                          Defaults to current Python version.
            exclude_newer_than: Optional datetime to exclude packages uploaded after
                          this date (for dependency cooldowns).

        Returns:
            Dict mapping package names to metadata dicts with 'version' and 'extras'

        Raises:
            resolvelib.ResolutionImpossible: If resolution fails
        """
        # Record the user-specified ordering so get_preference can prioritise
        # top-level requirements (mirrors pip's _user_requested).
        user_requested = {
            canonicalize_name(r.name): i for i, r in enumerate(requirements)
        }

        provider = PyPIProvider(
            session=self._session,
            index_urls=self.index_urls,
            python_version=python_version,
            exclude_newer_than=exclude_newer_than,
            user_requested=user_requested,
        )
        reporter = BaseReporter()
        resolver = ResolveLibResolver(provider, reporter)

        # Resolve
        result = resolver.resolve(requirements, max_rounds=1000)

        # Convert result to our format
        resolved = {}
        for identifier, candidate in result.mapping.items():
            resolved[identifier] = {
                'version': str(candidate.version),
                'extras': list(candidate.extras),
            }

        return resolved
