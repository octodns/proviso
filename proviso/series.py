#
#
#

import re
from dataclasses import dataclass, fields
from functools import cache

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version
from unearth import PackageFinder
from unearth.collector import collect_links_from_location
from unearth.evaluator import Evaluator, LinkMismatchError
from unearth.link import Link
from unearth.utils import fix_legacy_specifier

# Matches the contents of a single or double quoted string
_QUOTED = re.compile(r'"([^"]*)"|\'([^\']*)\'')


def _parse_release(literal):
    """Return the release of a version literal padded to three parts.

    A trailing ``.*`` is ignored. Returns None when the literal isn't a
    version.
    """
    if literal.endswith('.*'):
        literal = literal[:-2]
    try:
        release = Version(literal).release
    except InvalidVersion:
        return None
    return (release + (0, 0))[:3]


class PythonSeries:
    """A target Python version and the releases it stands for.

    ``3.12`` stands for every 3.12 release, ``3`` for every 3.x.y release and
    ``3.12.5`` for exactly that release. A marker or ``Requires-Python``
    applies to a series when it's true for at least one release in it.

    The comparisons in markers and specifiers can only change result at the
    versions they mention, so checking the first release of the series and
    the releases around each mentioned version is enough to cover the whole
    series.
    """

    def __init__(self, version):
        parsed = Version(version)
        release = parsed.release
        if len(release) > 3 or str(parsed) != '.'.join(map(str, release)):
            raise ValueError(
                f'Invalid target Python version {version}, expected X, X.Y or X.Y.Z'
            )
        self.version = str(parsed)
        self.release = release

    def __repr__(self):
        return f'PythonSeries({self.version!r})'

    def contains(self, release):
        """Return whether a three part release tuple is in the series."""
        return release[: len(self.release)] == self.release

    def overlaps(self, other):
        """Return whether the series have any release in common.

        Series are nested, so they overlap when one release is a prefix of
        the other, e.g. ``3.12`` and ``3.12.5``, or two identical targets.
        """
        n = min(len(self.release), len(other.release))
        return self.release[:n] == other.release[:n]

    def releases(self, literals=()):
        """Return the releases to check for markers mentioning literals.

        These are the first release of the series plus, for each literal that
        is a version, that release and its neighbours. Only releases in the
        series are returned, sorted.
        """
        candidates = {(self.release + (0, 0))[:3]}
        for literal in literals:
            release = _parse_release(literal)
            if release is None:
                continue
            major, minor, patch = release
            if len(self.release) == 1:
                minors = (minor - 1, minor, minor + 1)
            else:
                minors = (minor,)
            for m in minors:
                for p in (patch - 1, patch, patch + 1):
                    candidate = (major, m, p)
                    if m >= 0 and p >= 0 and self.contains(candidate):
                        candidates.add(candidate)
        return sorted(candidates)

    @staticmethod
    def release_environment(release):
        """Return the marker environment values for a three part release."""
        full_version = '.'.join(map(str, release))
        return {
            'python_version': '.'.join(map(str, release[:2])),
            'python_full_version': full_version,
            'implementation_version': full_version,
        }

    def marker_applies(self, marker, environment, extra=''):
        """Return whether marker is true for any release in the series.

        environment supplies the values that don't depend on the Python
        version, such as the platform.
        """
        # Split quoted strings so that each word of an ``in`` operand is
        # checked too, e.g. python_version in "3.10 3.11"
        literals = [
            word
            for a, b in _QUOTED.findall(str(marker))
            for word in re.split(r'[\s,]+', a or b)
        ]
        # One copy for every release, each overwrites the Python values
        env = dict(environment, extra=extra)
        for release in self.releases(literals):
            env.update(self.release_environment(release))
            if marker.evaluate(env):
                return True
        return False

    def specifier_satisfiable(self, specifier):
        """Return whether any release in the series satisfies specifier."""
        literals = [s.version for s in specifier]
        return any(
            specifier.contains('.'.join(map(str, release)), prereleases=True)
            for release in self.releases(literals)
        )

    def match_marker(self):
        """Return a requirements.txt marker that matches the series."""
        if len(self.release) == 1:
            major = self.release[0]
            return f"python_version>='{major}' and python_version<'{major + 1}'"
        if len(self.release) == 2:
            return f"python_version=='{self.version}'"
        return f"python_full_version=='{self.version}'"

    def guard_marker(self):
        """Return a requirements.txt marker that excludes the series.

        It's safe to join with ``and``.
        """
        if len(self.release) == 1:
            major = self.release[0]
            return (
                f"(python_version<'{major}' or python_version>='{major + 1}')"
            )
        if len(self.release) == 2:
            return f"python_version!='{self.version}'"
        return f"python_full_version!='{self.version}'"


def _index_url(page_url):
    """Return the index URL, redacted, of a package's index page URL.

    A package's page is the index URL followed by the package's name.
    """
    return Link(page_url.rstrip('/').rpartition('/')[0] + '/').redacted


@cache
def _requires_python_mismatch(py_ver, requires_python, specifier):
    """Return why a series can't use a file, or None if it can.

    py_ver is the series' release tuple, requires_python the file's
    Requires-Python and specifier that with unearth's legacy fixes applied.
    It's memoised as an index gives the same few Requires-Python values for
    many files.
    """
    try:
        specifier = SpecifierSet(specifier)
    except InvalidSpecifier:
        return f'Invalid requires-python: {requires_python}'
    series = PythonSeries('.'.join(map(str, py_ver)))
    if series.specifier_satisfiable(specifier):
        return None
    return (
        f'No release of the target python version ({series.version}) '
        f'matches the requires-python specifier {requires_python}'
    )


@dataclass
class SeriesEvaluator(Evaluator):
    """unearth Evaluator that checks Requires-Python against a whole series.

    A file is compatible with the target Python when any release in the
    target's series satisfies its Requires-Python. Without a target Python
    unearth's own check against the running interpreter is used.

    supported_tags is the target Python's supported wheel tags as a frozenset.
    SeriesPackageFinder builds it once for all of its evaluators.
    """

    supported_tags: frozenset | None = None

    def check_requires_python(self, link):
        py_ver = self.target_python.py_ver
        if py_ver is None:
            return super().check_requires_python(link)
        if self.ignore_compatibility or not link.requires_python:
            return
        # Fixed here rather than in the memo so that unearth's FutureWarning
        # about a legacy specifier is still reported against evaluate_link
        specifier = fix_legacy_specifier(link.requires_python)
        message = _requires_python_mismatch(
            tuple(py_ver), link.requires_python, specifier
        )
        if message is not None:
            raise LinkMismatchError(message)

    def check_wheel_tags(self, filename):
        # unearth checks the wheel's tags against a list of the supported
        # ones, which hashes all of them, over a thousand, for every wheel.
        # Checking against a frozenset only hashes the wheel's few tags.
        if self.ignore_compatibility:
            return
        supported = self.supported_tags
        if supported is None:
            supported = frozenset(self.target_python.supported_tags())
        if parse_wheel_filename(filename)[-1].isdisjoint(supported):
            raise LinkMismatchError(
                f'The wheel tags in {filename} are not compatible'
            )


class IndexCache:
    """The links on index pages and when each release was first uploaded.

    Neither depends on the target Python, so the SeriesPackageFinders of a
    run, one per target Python version, share one of these.  Each index page
    is then fetched and parsed once, and its files dated once.
    """

    def __init__(self):
        # Normalized page URL -> the links collected from it
        self._pages = {}
        # (canonical name, Version) -> earliest upload time of its files
        self._first_uploads = {}
        # canonical name -> indexes that gave files for it, none with an
        # upload time
        self._undated_indexes = {}

    def links(self, session, page_link, package_name, headers=None):
        """Return the links on a package's index page, fetched once."""
        key = page_link.normalized
        links = self._pages.get(key)
        if links is None:
            links = list(
                collect_links_from_location(session, page_link, headers=headers)
            )
            self._record_first_uploads(links, package_name)
            # Only a page that gives files is kept.  For a page it fails to
            # fetch unearth logs a warning and gives at most the page's own
            # link, so the next target Python tries, and warns, again as it
            # would without the cache.
            if any(link is not page_link for link in links):
                self._pages[key] = links
        return links

    def _record_first_uploads(self, links, package_name):
        # Every file of a release counts, including ones that are yanked or
        # aren't compatible with the target Python
        evaluator = Evaluator(
            package_name, ignore_compatibility=True, allow_yanked=True
        )
        name = canonicalize_name(package_name)
        # The index pages the files came from, and those with an upload time
        pages = set()
        dated_pages = set()
        for link in links:
            package = evaluator.evaluate_link(link)
            if package is None:
                continue
            pages.add(link.comes_from)
            if link.upload_time is None:
                continue
            dated_pages.add(link.comes_from)
            key = (name, Version(package.version))
            first = self._first_uploads.get(key)
            if first is None or link.upload_time < first:
                self._first_uploads[key] = link.upload_time
        # A file found locally rather than on an index page has no page
        undated = {
            _index_url(page) for page in pages - dated_pages if page is not None
        }
        if undated:
            self._undated_indexes.setdefault(name, set()).update(undated)

    def first_upload(self, name, version):
        """Return when a release was first uploaded, or None if unknown.

        That's the earliest upload time of any of its files found so far,
        whether or not they're compatible with the target Python. It's None
        when none of them has an upload time, e.g. on an index that only
        serves the HTML API.
        """
        return self._first_uploads.get((canonicalize_name(name), version))

    def undated_indexes(self, name):
        """Return the indexes that gave files for a package, none dated.

        These are the indexes found so far whose files for the package all
        lack an upload time, e.g. ones that only serve the HTML API, sorted.
        Credentials in their URLs are redacted.
        """
        return sorted(self._undated_indexes.get(canonicalize_name(name), ()))


class SeriesPackageFinder(PackageFinder):
    """unearth PackageFinder that evaluates links with SeriesEvaluator.

    find_all lists a package's files from index pages kept in an IndexCache,
    which also records when each release was first uploaded, see
    first_upload.  Pass the same index_cache to the finders of a run to
    share them.
    """

    def __init__(self, *args, index_cache=None, **kwargs):
        super().__init__(*args, **kwargs)
        # The target's supported wheel tags, see SeriesEvaluator
        self._supported_tags = frozenset(self.target_python.supported_tags())
        self._index_cache = IndexCache() if index_cache is None else index_cache

    def build_evaluator(self, package_name, allow_yanked=False):
        evaluator = super().build_evaluator(package_name, allow_yanked)
        return SeriesEvaluator(
            **{f.name: getattr(evaluator, f.name) for f in fields(evaluator)},
            supported_tags=self._supported_tags,
        )

    # find_all uses two private PackageFinder methods, checked against unearth
    # 0.19.0: _build_index_page_link(index_url, package_name), for the URL of
    # a package's index page, and _sort_key(package), to order the files as
    # unearth does.  It stands in for find_matches(package_name,
    # allow_yanked=True, allow_prereleases=True), whose private
    # _find_packages does the same with pages it fetches itself.
    # TestUnearthPrivateSurface in tests/test_series.py fails if either
    # method changes or find_all stops returning what find_matches does.
    # Then call that find_matches instead, which fetches and parses each page
    # once per target Python again, and keep recording first uploads by
    # passing IndexCache.links a page link built as unearth builds it: the
    # index URL joined with the canonical name and a trailing slash.
    def find_all(self, package_name):
        """Return every file of a package the target Python can use.

        Yanked files and pre-releases are included.  The best file comes
        first, as from unearth's find_matches.
        """
        # Proviso only gives index URLs and never sets respect_source_order,
        # which would sort each index on its own
        assert not self.respect_source_order
        evaluator = self.build_evaluator(package_name, allow_yanked=True)
        packages = []
        for source in self.sources:
            assert source['type'] == 'index'
            page_link = self._build_index_page_link(source['url'], package_name)
            links = self._index_cache.links(
                self.session, page_link, package_name, self.headers
            )
            packages.extend(filter(None, map(evaluator.evaluate_link, links)))
        # Stable, so files that sort equal keep the order of the indexes
        return sorted(packages, key=self._sort_key, reverse=True)

    def first_upload(self, name, version):
        """Return when a release was first uploaded, see IndexCache."""
        return self._index_cache.first_upload(name, version)

    def undated_indexes(self, name):
        """Return the indexes that gave files for a package, see IndexCache."""
        return self._index_cache.undated_indexes(name)
