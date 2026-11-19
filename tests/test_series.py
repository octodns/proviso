from dataclasses import fields
from datetime import datetime, timezone
from inspect import signature
from unittest import TestCase
from unittest.mock import patch

import httpx2
from packaging.markers import Marker, default_environment
from packaging.specifiers import SpecifierSet
from packaging.version import Version
from unearth import PackageFinder, TargetPython
from unearth.collector import collect_links_from_location
from unearth.evaluator import Evaluator, LinkMismatchError
from unearth.fetchers import DEFAULT_SECURE_ORIGINS
from unearth.link import Link

from proviso.series import (
    IndexCache,
    PythonSeries,
    SeriesEvaluator,
    SeriesPackageFinder,
)

HOST = {
    'implementation_name': 'cpython',
    'implementation_version': '3.99.7',
    'os_name': 'posix',
    'platform_machine': 'x86_64',
    'platform_python_implementation': 'CPython',
    'platform_release': '',
    'platform_system': 'Linux',
    'platform_version': '',
    'python_full_version': '3.99.7',
    'python_version': '3.99',
    'sys_platform': 'linux',
}


class TestPythonSeries(TestCase):
    def test_forms(self):
        self.assertEqual((3,), PythonSeries('3').release)
        self.assertEqual((3, 12), PythonSeries('3.12').release)
        self.assertEqual((3, 12, 5), PythonSeries('3.12.5').release)
        self.assertEqual('3.12', PythonSeries('3.12').version)
        self.assertEqual("PythonSeries('3.12')", repr(PythonSeries('3.12')))

    def test_invalid(self):
        for version in ('3.12.5.1', '3.13rc1', '3.12.post1', '1!3.12'):
            with self.assertRaises(ValueError, msg=version):
                PythonSeries(version)

    def test_contains(self):
        series = PythonSeries('3.12')
        self.assertTrue(series.contains((3, 12, 0)))
        self.assertTrue(series.contains((3, 12, 99)))
        self.assertFalse(series.contains((3, 13, 0)))
        self.assertTrue(PythonSeries('3').contains((3, 1, 2)))
        self.assertFalse(PythonSeries('3').contains((4, 0, 0)))
        self.assertTrue(PythonSeries('3.12.5').contains((3, 12, 5)))
        self.assertFalse(PythonSeries('3.12.5').contains((3, 12, 6)))

    def test_overlaps(self):
        for a, b in (
            ('3', '3.12'),
            ('3.12', '3.12.5'),
            ('3', '3.12.5'),
            ('3.12', '3.12'),
            ('3.12', '3.12.0'),
            ('3.12.5', '3.12.5'),
        ):
            self.assertTrue(PythonSeries(a).overlaps(PythonSeries(b)), (a, b))
            self.assertTrue(PythonSeries(b).overlaps(PythonSeries(a)), (b, a))
        for a, b in (
            ('3.12', '3.13'),
            ('3.12.4', '3.12.5'),
            ('2', '3'),
            ('2', '3.12'),
            ('3.11', '3.12.5'),
        ):
            self.assertFalse(PythonSeries(a).overlaps(PythonSeries(b)), (a, b))
            self.assertFalse(PythonSeries(b).overlaps(PythonSeries(a)), (b, a))

    def test_releases_minor(self):
        series = PythonSeries('3.12')
        self.assertEqual([(3, 12, 0)], series.releases())
        self.assertEqual(
            [(3, 12, 0), (3, 12, 3), (3, 12, 4), (3, 12, 5)],
            series.releases(['3.12.4']),
        )
        # Literals outside the series, or that aren't versions, add nothing
        self.assertEqual(
            [(3, 12, 0)], series.releases(['3.13', '3.11.4', 'linux', ''])
        )
        # A trailing .* is ignored and there is no patch -1
        self.assertEqual([(3, 12, 0), (3, 12, 1)], series.releases(['3.12.*']))

    def test_releases_major(self):
        series = PythonSeries('3')
        self.assertEqual([(3, 0, 0)], series.releases())
        self.assertEqual(
            [
                (3, 0, 0),
                (3, 10, 0),
                (3, 10, 1),
                (3, 11, 0),
                (3, 11, 1),
                (3, 12, 0),
                (3, 12, 1),
            ],
            series.releases(['3.11']),
        )
        self.assertEqual(
            [(3, 0, 0), (3, 0, 1), (3, 1, 0), (3, 1, 1)], series.releases(['3'])
        )
        self.assertEqual([(3, 0, 0)], series.releases(['4', '2.7']))

    def test_releases_patch(self):
        series = PythonSeries('3.12.5')
        self.assertEqual([(3, 12, 5)], series.releases())
        self.assertEqual([(3, 12, 5)], series.releases(['3.12.5', '3.12.4']))

    def _applies(self, version, marker, extra=''):
        return PythonSeries(version).marker_applies(Marker(marker), HOST, extra)

    def test_marker_minor_series(self):
        for marker, expected in (
            ("python_full_version < '3.12.4'", True),
            ("python_full_version >= '3.12.4'", True),
            ("python_version >= '3.13'", False),
            ("python_full_version >= '3.13'", False),
            ("python_version < '3.11'", False),
            ("python_full_version == '3.12.7'", True),
            ("python_full_version != '3.12.0'", True),
            ("python_full_version == '3.11.7'", False),
            ("python_version == '3.12'", True),
            ("python_version in '3.11 3.12'", True),
            ("python_version not in '3.11 3.12'", False),
        ):
            self.assertEqual(expected, self._applies('3.12', marker), marker)

    def test_marker_major_series(self):
        for marker, expected in (
            ("python_version < '3.11'", True),
            ("python_version >= '4'", False),
            ("python_version < '3'", False),
            ("python_version in '3.11 3.12'", True),
            ("python_full_version == '3.10.7'", True),
        ):
            self.assertEqual(expected, self._applies('3', marker), marker)

    def test_marker_patch_release(self):
        for marker, expected in (
            ("python_full_version < '3.12.4'", False),
            ("python_full_version >= '3.12.4'", True),
            ("python_version == '3.12'", True),
        ):
            self.assertEqual(expected, self._applies('3.12.5', marker), marker)

    def test_marker_compound(self):
        # Needs a release that is both >= 3.12.2 and < 3.12.4
        marker = (
            "python_full_version >= '3.12.2' and "
            "python_full_version < '3.12.4' and sys_platform == 'linux'"
        )
        self.assertTrue(self._applies('3.12', marker))
        self.assertFalse(self._applies('3.13', marker))
        # Host values other than the Python version are still used
        self.assertFalse(
            self._applies('3.12', marker.replace('linux', 'win32'))
        )
        marker = (
            "(python_full_version < '3.12.1' and python_full_version > "
            "'3.12.1') or extra == 'dev'"
        )
        self.assertFalse(self._applies('3.12', marker))
        self.assertTrue(self._applies('3.12', marker, extra='dev'))

    def test_marker_without_version_literals(self):
        self.assertTrue(self._applies('3.12', "sys_platform == 'linux'"))
        self.assertFalse(self._applies('3.12', "os_name == 'nt'"))
        self.assertTrue(self._applies('3.12', "extra == 'dev'", 'dev'))
        self.assertFalse(self._applies('3.12', "extra == 'dev'"))

    def test_marker_implementation_version_is_target(self):
        self.assertTrue(
            self._applies('3.12', "implementation_version >= '3.12.3'")
        )
        self.assertFalse(
            self._applies('3.12', "implementation_version >= '3.99'")
        )

    def test_marker_with_real_default_environment(self):
        # The host's values are overridden whatever Python runs the tests
        env = default_environment()
        series = PythonSeries('2.7')
        self.assertTrue(
            series.marker_applies(Marker("implementation_version < '3'"), env)
        )
        self.assertFalse(
            series.marker_applies(Marker("python_version >= '3'"), env)
        )

    def test_release_environment(self):
        self.assertEqual(
            {
                'python_version': '3.12',
                'python_full_version': '3.12.4',
                'implementation_version': '3.12.4',
            },
            PythonSeries.release_environment((3, 12, 4)),
        )

    def test_specifier_satisfiable(self):
        series = PythonSeries('3.12')
        for specifier, expected in (
            ('>=3.12.1', True),
            ('>=3.13', False),
            ('!=3.12.0,!=3.12.1,>=3.8', True),
            ('<3.12.1', True),
            ('<3.12', False),
            ('==3.12.*', True),
            ('==3.11.*', False),
            ('~=3.11', True),
            ('', True),
        ):
            self.assertEqual(
                expected,
                series.specifier_satisfiable(SpecifierSet(specifier)),
                specifier,
            )
        self.assertFalse(
            PythonSeries('3.12.0').specifier_satisfiable(
                SpecifierSet('>=3.12.1')
            )
        )
        self.assertTrue(
            PythonSeries('3').specifier_satisfiable(SpecifierSet('>=3.13'))
        )
        self.assertFalse(
            PythonSeries('3').specifier_satisfiable(SpecifierSet('>=4'))
        )


class TestOutputMarkers(TestCase):
    def _env(self, full_version):
        python_version = '.'.join(full_version.split('.')[:2])
        return dict(
            HOST,
            python_version=python_version,
            python_full_version=full_version,
        )

    def test_minor(self):
        series = PythonSeries('3.12')
        self.assertEqual("python_version=='3.12'", series.match_marker())
        self.assertEqual("python_version!='3.12'", series.guard_marker())

    def test_patch(self):
        series = PythonSeries('3.12.5')
        self.assertEqual("python_full_version=='3.12.5'", series.match_marker())
        self.assertEqual("python_full_version!='3.12.5'", series.guard_marker())

    def test_major(self):
        series = PythonSeries('3')
        self.assertEqual(
            "python_version>='3' and python_version<'4'", series.match_marker()
        )
        self.assertEqual(
            "(python_version<'3' or python_version>='4')", series.guard_marker()
        )

    def test_markers_evaluate(self):
        for version, matching, other in (
            ('3', ('3.0.0', '3.12.4', '3.99.1'), ('2.7.18', '4.0.0')),
            ('3.12', ('3.12.0', '3.12.9'), ('3.11.9', '3.13.0')),
            ('3.12.5', ('3.12.5',), ('3.12.4', '3.12.6')),
        ):
            series = PythonSeries(version)
            match = Marker(series.match_marker())
            guard = Marker(series.guard_marker())
            for full_version in matching:
                env = self._env(full_version)
                self.assertTrue(match.evaluate(env), (version, full_version))
                self.assertFalse(guard.evaluate(env), (version, full_version))
            for full_version in other:
                env = self._env(full_version)
                self.assertFalse(match.evaluate(env), (version, full_version))
                self.assertTrue(guard.evaluate(env), (version, full_version))


class TestSeriesEvaluator(TestCase):
    def _check(self, requires_python, py_ver=(3, 12), **kwargs):
        evaluator = SeriesEvaluator(
            package_name='foo',
            target_python=TargetPython(py_ver=py_ver),
            **kwargs,
        )
        link = Link(
            'https://example.com/foo-1.0.tar.gz',
            requires_python=requires_python,
        )
        try:
            evaluator.check_requires_python(link)
        except LinkMismatchError:
            return False
        return True

    def test_series(self):
        for requires_python, expected in (
            ('>=3.12.1', True),
            ('>=3.13', False),
            ('!=3.12.0,!=3.12.1,>=3.8', True),
            ('<3.12.1', True),
            ('==3.12.*', True),
            ('>=3.6, <4', True),
            (None, True),
            ('', True),
        ):
            self.assertEqual(
                expected, self._check(requires_python), requires_python
            )

    def test_legacy_specifier(self):
        # unearth rewrites >=3.12.* to >=3.12.0
        with self.assertWarns(FutureWarning):
            self.assertTrue(self._check('>=3.12.*'))
        with self.assertWarns(FutureWarning):
            self.assertFalse(self._check('>=3.13.*'))

    def test_legacy_specifier_warning_location(self):
        # The warning names the caller of check_requires_python, as it does
        # for unearth's own check
        with self.assertWarns(FutureWarning) as ctx:
            self.assertTrue(self._check('>=3.11.*'))
        self.assertEqual(__file__, ctx.filename)

    def test_patch_target(self):
        self.assertFalse(self._check('>=3.12.1', py_ver=(3, 12, 0)))
        self.assertTrue(self._check('>=3.12.1', py_ver=(3, 12, 1)))

    def test_major_target(self):
        self.assertTrue(self._check('>=3.13', py_ver=(3,)))
        self.assertFalse(self._check('>=4', py_ver=(3,)))

    def test_invalid_specifier(self):
        with self.assertRaises(LinkMismatchError) as ctx:
            SeriesEvaluator(
                package_name='foo', target_python=TargetPython(py_ver=(3, 12))
            ).check_requires_python(
                Link('https://example.com/foo.tar.gz', requires_python='>=x')
            )
        self.assertEqual('Invalid requires-python: >=x', str(ctx.exception))
        # The message gives the file's value, not the one unearth fixed up
        with self.assertWarns(FutureWarning):
            self.assertEqual(
                'Invalid requires-python: >=x.*', self._message('>=x.*')
            )
        self.assertEqual(
            'Invalid requires-python: >=x.*', self._message('>=x.*')
        )

    def test_mismatch_message(self):
        with self.assertRaises(LinkMismatchError) as ctx:
            SeriesEvaluator(
                package_name='foo', target_python=TargetPython(py_ver=(3, 12))
            ).check_requires_python(
                Link('https://example.com/foo.tar.gz', requires_python='>=3.13')
            )
        self.assertIn('(3.12)', str(ctx.exception))
        self.assertIn('>=3.13', str(ctx.exception))

    def test_ignore_compatibility(self):
        self.assertTrue(self._check('>=3.13', ignore_compatibility=True))
        self.assertTrue(self._check('>=x', ignore_compatibility=True))

    def test_without_target_uses_running_python(self):
        with patch('unearth.evaluator.sys') as mock_sys:
            mock_sys.version_info = (3, 11, 4)
            self.assertTrue(self._check('>=3.11', py_ver=None))
            self.assertFalse(self._check('>=3.12', py_ver=None))

    def _message(self, requires_python, py_ver=(3, 12)):
        evaluator = SeriesEvaluator(
            package_name='foo', target_python=TargetPython(py_ver=py_ver)
        )
        with self.assertRaises(LinkMismatchError) as ctx:
            evaluator.check_requires_python(
                Link(
                    'https://example.com/foo.tar.gz',
                    requires_python=requires_python,
                )
            )
        return str(ctx.exception)

    def test_repeated_requires_python(self):
        # The result for a series and Requires-Python is memoised, a repeat
        # gives the same message and a different series its own result
        for _ in range(2):
            self.assertEqual(
                'No release of the target python version (3.12) matches the '
                'requires-python specifier >=3.13',
                self._message('>=3.13'),
            )
            self.assertEqual(
                'Invalid requires-python: >=y', self._message('>=y')
            )
            self.assertEqual(
                'Invalid requires-python: >=y',
                self._message('>=y', py_ver=(3, 13)),
            )
            self.assertTrue(self._check('>=3.13', py_ver=(3, 13)))
            self.assertEqual(
                'No release of the target python version (3.11) matches the '
                'requires-python specifier >=3.13',
                self._message('>=3.13', py_ver=(3, 11)),
            )


class TestSeriesEvaluatorWheelTags(TestCase):
    # A wheel for every combination of these, most of them incompatible
    PYTHONS = ('py3', 'py2.py3', 'py2', 'cp311', 'cp315', 'cp39', 'pp310')
    ABIS = ('none', 'abi3', 'cp311', 'cp315', 'cp315t', 'cp39')
    PLATFORMS = (
        'any',
        'manylinux_2_17_x86_64.manylinux2014_x86_64',
        'manylinux2014_x86_64',
        'musllinux_1_2_x86_64',
        'macosx_11_0_arm64',
        'win_amd64',
    )

    def _filenames(self):
        return [
            f'foo-1.0-{python}-{abi}-{platform}.whl'
            for python in self.PYTHONS
            for abi in self.ABIS
            for platform in self.PLATFORMS
        ]

    def _compatible(self, evaluator, filename):
        try:
            evaluator.check_wheel_tags(filename)
        except LinkMismatchError as e:
            self.assertEqual(
                f'The wheel tags in {filename} are not compatible', str(e)
            )
            return False
        return True

    def test_same_as_unearth(self):
        for py_ver in ((3, 11), (3, 15)):
            target_python = TargetPython(py_ver=py_ver)
            finder = SeriesPackageFinder(
                session=None, index_urls=[], target_python=target_python
            )
            evaluators = (
                finder.build_evaluator('foo'),
                # Without the finder's tags the evaluator builds its own
                SeriesEvaluator(
                    package_name='foo', target_python=target_python
                ),
            )
            base = Evaluator(package_name='foo', target_python=target_python)
            results = []
            for filename in self._filenames():
                expected = self._compatible(base, filename)
                results.append(expected)
                for evaluator in evaluators:
                    self.assertEqual(
                        expected,
                        self._compatible(evaluator, filename),
                        (py_ver, filename),
                    )
            # Both outcomes are covered
            self.assertIn(True, results)
            self.assertIn(False, results)

    def test_compatible_and_incompatible(self):
        evaluator = SeriesEvaluator(
            package_name='foo', target_python=TargetPython(py_ver=(3, 12))
        )
        self.assertTrue(self._compatible(evaluator, 'foo-1.0-py3-none-any.whl'))
        self.assertFalse(
            self._compatible(evaluator, 'foo-1.0-cp27-cp27m-win32.whl')
        )

    def test_ignore_compatibility(self):
        evaluator = SeriesEvaluator(
            package_name='foo',
            target_python=TargetPython(py_ver=(3, 12)),
            ignore_compatibility=True,
            supported_tags=frozenset(),
        )
        self.assertTrue(
            self._compatible(evaluator, 'foo-1.0-cp27-cp27m-win32.whl')
        )


class TestSeriesPackageFinder(TestCase):
    def _finder(self, py_ver):
        return SeriesPackageFinder(
            session=None,
            index_urls=[],
            target_python=TargetPython(py_ver=py_ver),
        )

    def test_build_evaluator(self):
        evaluator = self._finder((3, 12)).build_evaluator(
            'Foo', allow_yanked=True
        )
        self.assertIsInstance(evaluator, SeriesEvaluator)
        self.assertEqual('Foo', evaluator.package_name)
        self.assertEqual((3, 12), evaluator.target_python.py_ver)
        self.assertTrue(evaluator.allow_yanked)
        self.assertEqual(
            frozenset(evaluator.target_python.supported_tags()),
            evaluator.supported_tags,
        )

    def test_evaluate_link(self):
        evaluator = self._finder((3, 12)).build_evaluator('foo')
        link = Link(
            'https://example.com/foo-1.0.tar.gz', requires_python='>=3.12.1'
        )
        package = evaluator.evaluate_link(link)
        self.assertEqual('1.0', package.version)

        link = Link(
            'https://example.com/foo-1.0.tar.gz', requires_python='>=3.13'
        )
        self.assertIsNone(evaluator.evaluate_link(link))


def _json_index(pages):
    """Return a session serving PEP 691 index pages.

    pages maps a page URL to its file entries.  Every URL requested is added
    to the session's requested list, and one in its failing set gets a 500.
    """

    def handler(request):
        session.requested.append(str(request.url))
        if str(request.url) in session.failing:
            return httpx2.Response(500)
        return httpx2.Response(
            200,
            headers={'Content-Type': 'application/vnd.pypi.simple.v1+json'},
            json={
                'meta': {'api-version': '1.1'},
                'files': pages.get(str(request.url), []),
            },
        )

    session = httpx2.Client(transport=httpx2.MockTransport(handler))
    session.iter_secure_origins = lambda: iter(DEFAULT_SECURE_ORIGINS)
    session.requested = []
    session.failing = set()
    return session


def _file(filename, host='files.example.com', **kwargs):
    entry = {
        'filename': filename,
        'url': f'https://{host}/{filename}',
        'hashes': {},
    }
    entry.update(kwargs)
    return entry


class TestUnearthPrivateSurface(TestCase):
    """Fail if unearth changes the private methods SeriesPackageFinder uses.

    SeriesPackageFinder.find_all lists a package's files as unearth's
    find_matches does, but from index pages kept in an IndexCache.  It uses
    unearth's private PackageFinder._build_index_page_link and _sort_key to
    do so.  These tests use the real unearth machinery so an unearth upgrade
    that changes them, or how find_matches lists files, is caught here rather
    than as different candidates.
    """

    MESSAGE = (
        "unearth's private PackageFinder methods or find_matches have "
        'changed, see the comment on SeriesPackageFinder.find_all in '
        'proviso/series.py'
    )

    INDEXES = (
        'https://pypi.example.com/simple/',
        'https://mirror.example.com/simple/',
    )

    # Not in the order find_all returns them
    PAGES = {
        'https://pypi.example.com/simple/foo/': [
            _file('foo-1.0-py3-none-any.whl'),
            _file('foo-2.0-cp27-cp27m-win32.whl'),
            _file('foo-2.0.tar.gz', **{'requires-python': '>=3.8'}),
            _file('foo-2.0-py3-none-any.whl'),
            _file('foo-2.0-cp312-none-any.whl'),
            _file('foo-2.0-py312-none-any.whl'),
            _file('foo-3.0b1-py3-none-any.whl'),
            _file('foo-1.5-py3-none-any.whl', yanked='broken'),
            _file('foo-1.6-py3-none-any.whl', yanked=True),
            _file('foo-1.1-py3-none-any.whl', **{'requires-python': '>=x'}),
            _file('foo-1.2-py3-none-any.whl', **{'requires-python': '>=3.13'}),
            _file('bar-1.0-py3-none-any.whl'),
            _file('foo-bad.whl'),
            _file('foo-1.3.exe'),
        ],
        'https://mirror.example.com/simple/foo/': [
            _file('foo-1.9.tar.gz', host='mirror.example.com'),
            _file('foo-2.0-py3-none-any.whl', host='mirror.example.com'),
            _file('foo-1.0-py3-none-any.whl', host='mirror.example.com'),
        ],
    }

    def test_signatures(self):
        for name, parameters in (
            ('_build_index_page_link', ['self', 'index_url', 'package_name']),
            ('_sort_key', ['self', 'package']),
        ):
            method = getattr(PackageFinder, name, None)
            self.assertIsNotNone(method, self.MESSAGE)
            self.assertEqual(
                parameters, list(signature(method).parameters), self.MESSAGE
            )
        self.assertEqual(
            ['session', 'location', 'expand', 'headers'],
            list(signature(collect_links_from_location).parameters),
            self.MESSAGE,
        )
        self.assertIn(
            'upload_time', [f.name for f in fields(Link)], self.MESSAGE
        )
        self.assertIn(
            'package_name', [f.name for f in fields(Evaluator)], self.MESSAGE
        )

    def test_same_as_find_matches(self):
        for py_ver in ((3, 12), (3, 13)):
            finder = SeriesPackageFinder(
                session=_json_index(self.PAGES),
                index_urls=self.INDEXES,
                target_python=TargetPython(py_ver=py_ver),
            )
            expected = [
                (p.name, p.version, p.link.url)
                for p in finder.find_matches(
                    'Foo', allow_yanked=True, allow_prereleases=True
                )
            ]
            self.assertEqual(
                expected,
                [
                    (p.name, p.version, p.link.url)
                    for p in finder.find_all('Foo')
                ],
                self.MESSAGE,
            )
            # The files cover what the comparison is meant to
            urls = [url for _, _, url in expected]
            for filename in (
                'files.example.com/foo-3.0b1-py3-none-any.whl',
                'files.example.com/foo-2.0.tar.gz',
                'mirror.example.com/foo-2.0-py3-none-any.whl',
                'files.example.com/foo-1.5-py3-none-any.whl',
            ):
                self.assertIn(f'https://{filename}', urls, py_ver)
            # Each target has a file the other can't use
            self.assertEqual(
                py_ver == (3, 12),
                'https://files.example.com/foo-2.0-cp312-none-any.whl' in urls,
            )
            self.assertEqual(
                py_ver == (3, 13),
                'https://files.example.com/foo-1.2-py3-none-any.whl' in urls,
            )


class TestIndexCache(TestCase):
    INDEX = 'https://pypi.example.com/simple/'
    PAGE = 'https://pypi.example.com/simple/foo/'

    SDIST_UPLOAD = datetime(2024, 6, 1, tzinfo=timezone.utc)
    WHEEL_UPLOAD = datetime(2024, 1, 1, tzinfo=timezone.utc)

    PAGES = {
        PAGE: [
            _file(
                'foo-1.0.tar.gz', **{'upload-time': SDIST_UPLOAD.isoformat()}
            ),
            # Uploaded first, but not for any target Python or platform
            _file(
                'foo-1.0-cp27-cp27m-win32.whl',
                **{'upload-time': WHEEL_UPLOAD.isoformat()},
            ),
        ]
    }

    def _finders(self, session, index_cache):
        return [
            SeriesPackageFinder(
                session=session,
                index_urls=[self.INDEX],
                target_python=TargetPython(py_ver=py_ver),
                index_cache=index_cache,
            )
            for py_ver in ((3, 12), (3, 13))
        ]

    def test_shared(self):
        session = _json_index(self.PAGES)
        for finder in self._finders(session, IndexCache()):
            self.assertEqual(
                ['foo-1.0.tar.gz'],
                [p.link.filename for p in finder.find_all('Foo')],
            )
            # The earliest file isn't a match, but still dates the release
            self.assertEqual(
                self.WHEEL_UPLOAD, finder.first_upload('foo', Version('1.0'))
            )
            self.assertEqual([], finder.undated_indexes('foo'))
        self.assertEqual([self.PAGE], session.requested)

    def test_not_shared(self):
        session = _json_index(self.PAGES)
        first, second = self._finders(session, None)
        first.find_all('foo')
        self.assertIsNone(second.first_upload('foo', Version('1.0')))
        second.find_all('foo')
        self.assertEqual([self.PAGE, self.PAGE], session.requested)

    def test_undated(self):
        pages = {self.PAGE: [_file('foo-1.0.tar.gz')]}
        session = _json_index(pages)
        for finder in self._finders(session, IndexCache()):
            self.assertEqual(1, len(finder.find_all('foo')))
            self.assertIsNone(finder.first_upload('foo', Version('1.0')))
            self.assertEqual([self.INDEX], finder.undated_indexes('foo'))
        self.assertEqual([self.PAGE], session.requested)

    def test_failed_page_not_kept(self):
        session = _json_index(self.PAGES)
        session.failing.add(self.PAGE)
        first, second = self._finders(session, IndexCache())
        with self.assertLogs('unearth.collector', level='WARNING') as logs:
            self.assertEqual([], first.find_all('foo'))
        self.assertIn(
            f'Failed to collect links from {self.PAGE}', logs.output[0]
        )
        session.failing.clear()
        self.assertEqual(1, len(second.find_all('foo')))
        self.assertEqual(
            self.WHEEL_UPLOAD, second.first_upload('foo', Version('1.0'))
        )
        self.assertEqual([self.PAGE, self.PAGE], session.requested)

    def test_empty_page_not_kept(self):
        session = _json_index({})
        index_cache = IndexCache()
        for finder in self._finders(session, index_cache):
            self.assertEqual([], finder.find_all('foo'))
        self.assertEqual([self.PAGE, self.PAGE], session.requested)

    def test_request_error_raised(self):
        def handler(request):
            raise httpx2.ConnectError('refused', request=request)

        session = httpx2.Client(transport=httpx2.MockTransport(handler))
        session.iter_secure_origins = lambda: iter(DEFAULT_SECURE_ORIGINS)
        for finder in self._finders(session, IndexCache()):
            with self.assertRaises(httpx2.ConnectError):
                finder.find_all('foo')
