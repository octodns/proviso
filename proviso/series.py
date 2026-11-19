#
#
#

import re
from dataclasses import fields

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from unearth import PackageFinder
from unearth.evaluator import Evaluator, LinkMismatchError
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
        for release in self.releases(literals):
            env = dict(environment, extra=extra)
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


class SeriesEvaluator(Evaluator):
    """unearth Evaluator that checks Requires-Python against a whole series.

    A file is compatible with the target Python when any release in the
    target's series satisfies its Requires-Python. Without a target Python
    unearth's own check against the running interpreter is used.
    """

    def check_requires_python(self, link):
        py_ver = self.target_python.py_ver
        if py_ver is None:
            return super().check_requires_python(link)
        if self.ignore_compatibility or not link.requires_python:
            return
        try:
            requires_python = SpecifierSet(
                fix_legacy_specifier(link.requires_python)
            )
        except InvalidSpecifier as e:
            raise LinkMismatchError(
                f'Invalid requires-python: {link.requires_python}'
            ) from e
        series = PythonSeries('.'.join(map(str, py_ver)))
        if not series.specifier_satisfiable(requires_python):
            raise LinkMismatchError(
                f'No release of the target python version ({series.version}) '
                f'matches the requires-python specifier {link.requires_python}'
            )


class SeriesPackageFinder(PackageFinder):
    """unearth PackageFinder that evaluates links with SeriesEvaluator.

    It also records when each release was first uploaded, see first_upload.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # (canonical name, Version) -> earliest upload time of its files
        self._first_uploads = {}

    def build_evaluator(self, package_name, allow_yanked=False):
        evaluator = super().build_evaluator(package_name, allow_yanked)
        return SeriesEvaluator(
            **{f.name: getattr(evaluator, f.name) for f in fields(evaluator)}
        )

    def _evaluate_links(self, links, evaluator):
        # unearth passes every link on an index page here before filtering
        # them, so it's where all of a release's files can be seen
        links = list(links)
        self._record_first_uploads(links, evaluator.package_name)
        return super()._evaluate_links(links, evaluator)

    def _record_first_uploads(self, links, package_name):
        # Every file of a release counts, including ones that are yanked or
        # aren't compatible with the target Python
        evaluator = Evaluator(
            package_name, ignore_compatibility=True, allow_yanked=True
        )
        name = canonicalize_name(package_name)
        for link in links:
            if link.upload_time is None:
                continue
            package = evaluator.evaluate_link(link)
            if package is None:
                continue
            key = (name, Version(package.version))
            first = self._first_uploads.get(key)
            if first is None or link.upload_time < first:
                self._first_uploads[key] = link.upload_time

    def first_upload(self, name, version):
        """Return when a release was first uploaded, or None if unknown.

        That's the earliest upload time of any of its files found so far,
        whether or not they're compatible with the target Python. It's None
        when none of them has an upload time, e.g. on an index that only
        serves the HTML API.
        """
        return self._first_uploads.get((canonicalize_name(name), version))
