import math
from collections import defaultdict
from logging import getLogger
from operator import attrgetter
from pathlib import Path
from tempfile import TemporaryDirectory

from packaging.markers import default_environment
from packaging.metadata import InvalidMetadata, Metadata
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version
from resolvelib import AbstractProvider, BaseReporter
from resolvelib import Resolver as ResolveLibResolver
from unearth import TargetPython
from unearth.auth import MultiDomainBasicAuth

from .builder import (
    build_failure_reason,
    build_sdist_metadata,
    sdist_pkg_info_metadata,
    wheel_metadata,
)
from .series import PythonSeries, SeriesPackageFinder
from .utils import CachingClient

log = getLogger('proviso.resolver')

# The following constant and the narrow_requirement_selection / get_preference
# methods on PyPIProvider are ported from pip's PipProvider implementation:
#   pip 26.1.2  (ported 2026-06-27)
#   pip/_internal/resolution/resolvelib/provider.py
# When upgrading pip, diff that file against these sections to pick up any
# improvements pip has made to its resolution heuristics.

# Ported from pip: pip/_internal/resolution/resolvelib/provider.py
# _CONFLICT_PRIORITY_THRESHOLD
_CONFLICT_PRIORITY_THRESHOLD = 5


# The most rounds resolvelib may take before giving up.  This matches pip's
# limit (pip/_internal/resolution/resolvelib/resolver.py, max_rounds).
MAX_ROUNDS = 200000


def make_identifier(name, extras=None):
    """Return the resolver identifier for a package name and extras.

    A requirement with extras (``name[a,b]``) gets its own identifier, separate
    from the plain package name, so the resolver tracks it as its own node.
    Its candidates depend on the base package at the same version, which pulls
    in the base dependencies and keeps the versions in sync.
    """
    name = canonicalize_name(name)
    if not extras:
        return name
    extras = sorted(canonicalize_name(e) for e in extras)
    return f'{name}[{",".join(extras)}]'


def split_identifier(identifier):
    """Split an identifier into its package name and a frozenset of extras."""
    name, sep, extras = identifier.partition('[')
    if not sep:
        return name, frozenset()
    return name, frozenset(extras.rstrip(']').split(','))


def marker_applies(marker, environment, series=None, extra=''):
    """Return whether a marker applies to a target Python series and extra.

    environment supplies the values that don't depend on the target Python,
    such as the platform. With a series the marker applies when it's true for
    any release in it. Without one the marker is evaluated against
    environment as is, which is the running interpreter's.
    """
    if series is None:
        return marker.evaluate(dict(environment, extra=extra))
    return series.marker_applies(marker, environment, extra)


def filter_requirements(requirements, python_version=None, extras=()):
    """Return the requirements that apply to a target Python and extras.

    A requirement applies when it has no marker, its marker is true with no
    extra selected, or its marker is true with any one of the extras selected.
    Order is preserved and each requirement appears at most once.
    """
    environment = default_environment()
    series = PythonSeries(python_version) if python_version else None
    extras = ('', *extras)

    return [
        r
        for r in requirements
        if r.marker is None
        or any(
            marker_applies(r.marker, environment, series, extra)
            for extra in extras
        )
    ]


class Candidate:
    """Represents a concrete package version."""

    def __init__(
        self, name, version, extras=None, dependencies=None, package=None
    ):
        self.name = canonicalize_name(name)
        self.version = version
        self.extras = frozenset(canonicalize_name(e) for e in extras or ())
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


class MetadataUnavailableError(Exception):
    """A package version's metadata, and so its dependencies, is unavailable."""

    def __init__(self, name, version, reason):
        self.name = name
        self.version = version
        self.reason = reason
        super().__init__(
            f'Unable to get the metadata for {name}=={version}: {reason}'
        )


class PyPIProvider(AbstractProvider):
    """Provider that queries PyPI for packages and their dependencies."""

    def __init__(
        self,
        session,
        index_urls,
        python_version=None,
        exclude_newer_than=None,
        user_requested=None,
        allow_prereleases=False,
        file_requires_dist_cache=None,
        build_sdists=False,
    ):
        self.session = session
        # When True, an sdist's build backend may be run to learn its
        # dependencies.  When False that raises MetadataUnavailableError
        # instead, as it executes the package's build code.
        self.build_sdists = build_sdists
        # When True, pre-releases are candidates for every package (like pip's
        # --pre).  When False, only for packages whose specifiers name one.
        self.allow_prereleases = allow_prereleases
        self._dependencies_cache = {}
        self._requires_dist_cache = {}
        # Requires-Dist read from downloaded files, keyed by file URL.  The
        # Resolver shares this across providers (one per target Python) so a
        # file is downloaded, and an sdist built, at most once per run.
        self._file_requires_dist_cache = (
            {} if file_requires_dist_cache is None else file_requires_dist_cache
        )
        self._matches_cache = {}
        # Conflict-promotion state (mirrors pip's PipProvider)
        self._conflict_counts = defaultdict(int)
        self._conflict_promoted = set()
        # Ordering of user-specified top-level requirements (name → index)
        self._user_requested = user_requested or {}

        # The target Python series, None means the running interpreter
        self.series = PythonSeries(python_version) if python_version else None

        # Create TargetPython if a specific version is requested.  It selects
        # wheel tags; Requires-Python is checked against the whole series by
        # SeriesPackageFinder.
        target_python = None
        if self.series:
            target_python = TargetPython(py_ver=self.series.release)

        # Create PackageFinder with the target Python version and optional date filter
        self.finder = SeriesPackageFinder(
            session=session,
            index_urls=index_urls,
            target_python=target_python,
            exclude_newer_than=exclude_newer_than,
        )

    def identify(self, requirement_or_candidate):
        """Return the identifier: the package name plus any extras."""
        return make_identifier(
            requirement_or_candidate.name, requirement_or_candidate.extras
        )

    # Ported from pip: pip/_internal/resolution/resolvelib/provider.py
    # PipProvider.narrow_requirement_selection
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
            names = [self.identify(info.requirement)]
            if info.parent is not None:
                names.append(self.identify(info.parent))
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

    # Ported from pip: pip/_internal/resolution/resolvelib/provider.py
    # PipProvider.get_preference
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

        name, extras = split_identifier(identifier)
        incompat_candidates = list(incompatibilities.get(identifier, []))
        if extras:
            # An extras candidate pins its base package to the same version,
            # so honor the base package's constraints here too (as pip does).
            reqs.extend(requirements.get(name, []))
            incompat_candidates.extend(incompatibilities.get(name, []))

        # Fetch (and cache) all platform-compatible packages for this name.
        # The specifier/incompatibility filtering below is kept outside the cache
        # because `incompatibilities` changes on every backtracking round.
        if name in self._matches_cache:
            log.debug(f'Matches cache hit for {name}')
            result = self._matches_cache[name]
        else:
            log.debug(f'Finding matches for {name}: {reqs[0]}')
            # Query by name only so one cache entry covers all specifiers and
            # extras seen during resolution.  Always ask for pre-releases and
            # apply the pre-release policy below: with prereleases=None unearth
            # defers to packaging's default, which differs between packaging
            # 25 and 26.  unearth returns a lazy LazySequence so we materialise
            # it immediately before caching.
            result = list(
                self.finder.find_matches(name, allow_prereleases=True)
            )
            self._matches_cache[name] = result

        # Determine whether pre-releases are allowed: always with --pre,
        # otherwise only when a specifier in the requirement set explicitly
        # references a pre-release (e.g. ">=1.0a1"), as pip does.
        # SpecifierSet.prereleases returns True in that case.
        allow_prereleases = self.allow_prereleases or any(
            r.specifier.prereleases for r in reqs
        )

        # Collect incompatible versions for fast O(1) membership tests.
        incompat_versions = {c.version for c in incompat_candidates}

        # Build the filtered, sorted candidate list.
        candidates = []
        for package in result:
            version = Version(package.version)

            # Exclude pre-releases unless a specifier explicitly allows them.
            if version.is_prerelease and not allow_prereleases:
                continue

            # Check if this version satisfies all requirements.  The
            # pre-release decision was made above, so pass prereleases=True
            # rather than relying on packaging's default, which changed in
            # packaging 26 to accept pre-releases for specifiers like ">=1.0".
            if not all(
                r.specifier.contains(version, prereleases=True) for r in reqs
            ):
                continue

            # Skip versions that are known incompatible from prior backtracking.
            if version in incompat_versions:
                continue

            # Carry the unearth package object so get_dependencies can use it
            # directly without a redundant find_best_match lookup.
            candidates.append(
                Candidate(name, version, extras=extras, package=package)
            )

        # Return candidates sorted by version (newest first)
        log.debug(f'Found {len(candidates)} candidates for {identifier}')
        return sorted(candidates, key=attrgetter('version'), reverse=True)

    def is_satisfied_by(self, requirement, candidate):
        """Check if the candidate satisfies the requirement."""
        if canonicalize_name(requirement.name) != candidate.name:
            return False
        # Candidates only come from find_matches, which already applied the
        # pre-release policy, so accept pre-releases here as pip does.
        return requirement.specifier.contains(
            candidate.version, prereleases=True
        )

    def _get_requires_dist(self, candidate):
        """Return the Requires-Dist entries for a candidate's name and version.

        The result is cached by name and version, so a package and its extras
        candidates share a single metadata fetch.
        """
        cache_key = (candidate.name, candidate.version)
        if cache_key in self._requires_dist_cache:
            return self._requires_dist_cache[cache_key]

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
                self._requires_dist_cache[cache_key] = []
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
            try:
                requires_dist = list(metadata.requires_dist or [])
            except InvalidMetadata as e:
                raise MetadataUnavailableError(
                    candidate.name,
                    candidate.version,
                    f'invalid Requires-Dist in the index metadata: {e}',
                ) from e
        else:
            # No metadata from the index, e.g. for sdists, so get it from the
            # file itself as pip does
            requires_dist = self._get_file_requires_dist(
                candidate, package.link
            )

        self._requires_dist_cache[cache_key] = requires_dist
        return requires_dist

    def _get_file_requires_dist(self, candidate, link):
        """Return the Requires-Dist entries read from a candidate's file."""
        url = link.url_without_fragment
        if url not in self._file_requires_dist_cache:
            metadata = self._download_metadata(candidate, link)
            try:
                requires_dist = list(metadata.requires_dist or [])
            except InvalidMetadata as e:
                raise MetadataUnavailableError(
                    candidate.name,
                    candidate.version,
                    f'invalid Requires-Dist in {link.filename}: {e}',
                ) from e
            self._file_requires_dist_cache[url] = requires_dist
        return self._file_requires_dist_cache[url]

    def _download_metadata(self, candidate, link):
        """Download a candidate's file and return its Metadata.

        Wheels carry it in .dist-info/METADATA.  Sdists carry it in PKG-INFO,
        which is used when PEP 643 says it's reliable; otherwise the sdist's
        build backend is asked for it, if build_sdists allows that.
        """
        name, version = candidate.name, candidate.version
        log.info(f'Downloading {link.filename} for {name}=={version} metadata')
        with TemporaryDirectory() as tmpdir:
            location = Path(tmpdir) / 'unpacked'
            location.mkdir()
            try:
                path = self.finder.download_and_unpack(
                    link, location, download_dir=Path(tmpdir) / 'download'
                )
            except Exception as e:
                raise MetadataUnavailableError(
                    name, version, f'downloading {link.redacted} failed: {e}'
                ) from e

            try:
                if link.is_wheel:
                    metadata = wheel_metadata(path, name)
                else:
                    metadata = sdist_pkg_info_metadata(path)
            except Exception as e:
                raise MetadataUnavailableError(
                    name, version, f'reading {link.filename} failed: {e}'
                ) from e

            if metadata is not None:
                return metadata
            if link.is_wheel:
                raise MetadataUnavailableError(
                    name, version, f'no METADATA in {link.filename}'
                )

            if not self.build_sdists:
                raise MetadataUnavailableError(
                    name,
                    version,
                    f'its dependencies can only be learned by building '
                    f'{link.filename}, which runs the package\'s build code; '
                    'pass --build-sdists to allow that',
                )

            log.info(f'Building {name}=={version} to get its metadata')
            try:
                metadata = build_sdist_metadata(path)
            except Exception as e:
                raise MetadataUnavailableError(
                    name,
                    version,
                    f'building {link.filename} failed: {build_failure_reason(e)}',
                ) from e
            # See build_sdist_metadata and #53
            log.warning(
                f'Metadata for {name}=={version} was built under the Python '
                'running proviso and is reused for every target Python version'
            )
            return metadata

    def get_dependencies(self, candidate):
        """Get dependencies for a candidate.

        A candidate without extras returns the dependencies that apply to the
        target environment.  A candidate with extras returns a pin on its base
        package at the same version plus the dependencies that only its extras
        add; the base dependencies come in through the base package.
        """
        cache_key = (candidate.name, candidate.version, candidate.extras)

        if cache_key in self._dependencies_cache:
            log.debug(
                f'Dependencies cache hit for {candidate.name}=={candidate.version} extras={candidate.extras}'
            )
            return self._dependencies_cache[cache_key]

        log.debug(
            f'Getting dependencies for {candidate.name}=={candidate.version} extras={candidate.extras}'
        )

        # Markers are evaluated for the target series on top of the running
        # interpreter's environment
        environment = default_environment()

        dependencies = []
        if candidate.extras:
            dependencies.append(
                Requirement(f'{candidate.name}=={candidate.version}')
            )

        for req in self._get_requires_dist(candidate):
            if req.marker is None or marker_applies(
                req.marker, environment, self.series
            ):
                # Applies without any extra, so it belongs to the base package
                if not candidate.extras:
                    dependencies.append(req)
                continue

            # Not a base dependency, check against requested extras
            for extra in candidate.extras:
                if marker_applies(req.marker, environment, self.series, extra):
                    dependencies.append(req)
                    break

        log.debug(
            f'Found {len(dependencies)} dependencies for {candidate.name}=={candidate.version} extras={candidate.extras}'
        )
        self._dependencies_cache[cache_key] = dependencies
        return dependencies


class Resolver:
    """Resolves package dependencies using PyPI."""

    def __init__(self, index_urls=None, session=None):
        """Initialize the resolver.

        Args:
            index_urls: List of package index URLs. Defaults to PyPI.
            session: Optional httpx2.Client for making requests. If None, creates a new CachingClient.
        """
        if index_urls is None:
            index_urls = ['https://pypi.org/simple/']

        self.index_urls = index_urls

        # Use provided session or create a new CachingClient
        if session is None:
            session = CachingClient()
        self._session = session
        self._session.auth = MultiDomainBasicAuth(index_urls=index_urls)
        # Shared by every provider this resolver creates, see PyPIProvider
        self._file_requires_dist_cache = {}

    def resolve(
        self,
        requirements,
        python_version=None,
        exclude_newer_than=None,
        allow_prereleases=False,
        extras=(),
        build_sdists=False,
    ):
        """Resolve dependencies for the given requirements.

        Args:
            requirements: List of packaging.requirements.Requirement objects
            python_version: Target Python version string. "3.12" stands for
                          every 3.12 release, "3" for every 3.x.y release and
                          "3.12.5" for exactly that release. A dependency is
                          included when any release in the series needs it.
                          Defaults to the running Python.
            exclude_newer_than: Optional datetime to exclude packages uploaded after
                          this date (for dependency cooldowns).
            allow_prereleases: Consider pre-release versions for every package.
                          By default pre-releases are only considered for a
                          package whose specifier names one (e.g. ">=2.0b1").
            extras: Names of the project extras that are selected. Only the
                          requirements that apply to python_version with these
                          extras are resolved.
            build_sdists: Allow running a package's build backend, under the
                          Python running proviso, to learn the dependencies of
                          an sdist when neither the index nor the sdist's
                          PKG-INFO provides them. This executes the package's
                          build code. By default MetadataUnavailableError is
                          raised for such a package instead.

        Returns:
            Dict mapping package names to metadata dicts with 'version' and 'extras'

        Raises:
            resolvelib.ResolutionImpossible: If resolution fails
            MetadataUnavailableError: If a package's metadata can't be obtained
        """
        requirements = filter_requirements(
            requirements, python_version=python_version, extras=extras
        )
        if not requirements:
            return {}

        # Record the user-specified ordering so get_preference can prioritise
        # top-level requirements (mirrors pip's _user_requested).
        user_requested = {}
        for i, r in enumerate(requirements):
            user_requested.setdefault(make_identifier(r.name, r.extras), i)
            # Requested extras also imply the base package
            user_requested.setdefault(make_identifier(r.name), i)

        provider = PyPIProvider(
            session=self._session,
            index_urls=self.index_urls,
            python_version=python_version,
            exclude_newer_than=exclude_newer_than,
            user_requested=user_requested,
            allow_prereleases=allow_prereleases,
            file_requires_dist_cache=self._file_requires_dist_cache,
            build_sdists=build_sdists,
        )
        reporter = BaseReporter()
        resolver = ResolveLibResolver(provider, reporter)

        # Resolve
        result = resolver.resolve(requirements, max_rounds=MAX_ROUNDS)

        # Convert result to our format, folding extras candidates into their
        # base package.  An extras candidate pins its base package to the same
        # version, so the versions always agree.
        resolved = {}
        for candidate in result.mapping.values():
            info = resolved.setdefault(
                candidate.name,
                {'version': str(candidate.version), 'extras': set()},
            )
            info['extras'].update(candidate.extras)

        for info in resolved.values():
            info['extras'] = sorted(info['extras'])

        return resolved
