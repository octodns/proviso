from datetime import datetime, timezone
from os.path import join
from subprocess import CalledProcessError
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from build import BuildBackendException
from packaging.requirements import Requirement

from proviso.main import (
    DirectUrl,
    build_project_metadata,
    find_requirements,
    format_and_print_metadata,
    parse_and_validate_args,
    write_requirements_to_file,
)


class TestBuildProjectMetadata(TestCase):
    def test_successful_build(self):
        """Test successful metadata build."""
        directory = '/path/to/project'
        mock_metadata = MagicMock()
        mock_metadata.name = 'test-package'

        with patch('proviso.main.Builder') as mock_builder_class:
            mock_builder_instance = MagicMock()
            mock_builder_instance.metadata = mock_metadata
            mock_builder_class.return_value = mock_builder_instance

            result = build_project_metadata(directory)

            # Verify Builder was instantiated correctly
            mock_builder_class.assert_called_once_with(directory)

            # Verify metadata was returned
            self.assertEqual(mock_metadata, result)

    def test_build_backend_exception(self):
        """Test that BuildBackendException causes exit."""
        directory = '/path/to/project'

        # Create a mock CalledProcessError
        mock_cpe = CalledProcessError(1, ['cmd'])
        mock_cpe.output = b'stdout output'
        mock_cpe.stderr = b'stderr output'

        # Create BuildBackendException with the CalledProcessError
        mock_exception = BuildBackendException(mock_cpe)

        with patch('proviso.main.Builder') as mock_builder_class:
            mock_builder_instance = MagicMock()
            mock_builder_instance.metadata
            type(mock_builder_instance).metadata = property(
                lambda self: (_ for _ in ()).throw(mock_exception)
            )
            mock_builder_class.return_value = mock_builder_instance

            with patch('proviso.main.exit') as mock_exit:
                with patch('proviso.main.log') as mock_log:
                    build_project_metadata(directory)

                    # Verify error was logged
                    mock_log.error.assert_called_once()
                    error_msg = mock_log.error.call_args[0][0]
                    self.assertIn('Failed to build project', error_msg)
                    self.assertIn('stdout output', error_msg)
                    self.assertIn('stderr output', error_msg)

                    # Verify exit was called with code 1
                    mock_exit.assert_called_once_with(1)


class TestFormatAndPrintMetadata(TestCase):
    def test_formats_and_logs_metadata(self):
        """Test that metadata is formatted and logged."""
        mock_metadata = MagicMock()
        mock_metadata.name = 'my-package'
        mock_metadata.version = '1.0.0'
        extras = ['dev', 'test']
        python_versions = ['3.9', '3.10', '3.11']

        with patch('proviso.main.log') as mock_log:
            format_and_print_metadata(mock_metadata, extras, python_versions)

            # Verify log.info was called
            mock_log.info.assert_called_once()
            log_msg = mock_log.info.call_args[0][0]

            # Verify message contains expected information
            self.assertIn('my-package', log_msg)
            self.assertIn('1.0.0', log_msg)
            self.assertIn('dev, test', log_msg)
            self.assertIn('3.9, 3.10, 3.11', log_msg)


class TestFindRequirements(TestCase):
    def test_empty_requirements(self):
        """Test with empty requirements list."""
        result = find_requirements([], ['3.9', '3.10'])

        self.assertEqual({}, result)

    def test_resolves_for_multiple_python_versions(self):
        """Test that requirements are resolved for each Python version."""
        requirements = [Requirement('requests>=2.0.0')]
        python_versions = ['3.9', '3.10']

        with patch('proviso.main.Resolver') as mock_resolver_class:
            mock_resolver_instance = MagicMock()
            # Return different versions for different Python versions
            mock_resolver_instance.resolve.side_effect = [
                {'requests': {'version': '2.28.0', 'extras': []}},
                {'requests': {'version': '2.28.0', 'extras': []}},
            ]
            mock_resolver_class.return_value = mock_resolver_instance

            with patch('proviso.main.log'):
                result = find_requirements(requirements, python_versions)

                # Verify resolver was called twice (once per Python version)
                self.assertEqual(2, mock_resolver_instance.resolve.call_count)

                # Verify result structure
                self.assertIn('requests', result)
                self.assertIn('2.28.0', result['requests'])
                self.assertEqual(['3.9', '3.10'], result['requests']['2.28.0'])

    def test_different_versions_per_python(self):
        """Test when different Python versions resolve to different package versions."""
        requirements = [Requirement('package>=1.0.0')]
        python_versions = ['3.9', '3.10']

        with patch('proviso.main.Resolver') as mock_resolver_class:
            mock_resolver_instance = MagicMock()
            mock_resolver_instance.resolve.side_effect = [
                {'package': {'version': '1.0.0', 'extras': []}},
                {'package': {'version': '1.1.0', 'extras': []}},
            ]
            mock_resolver_class.return_value = mock_resolver_instance

            with patch('proviso.main.log'):
                result = find_requirements(requirements, python_versions)

                # Should have both versions in result
                self.assertIn('package', result)
                self.assertIn('1.0.0', result['package'])
                self.assertIn('1.1.0', result['package'])
                self.assertEqual(['3.9'], result['package']['1.0.0'])
                self.assertEqual(['3.10'], result['package']['1.1.0'])

    def test_with_exclude_newer_than(self):
        """Test that exclude_newer_than is passed to resolver."""
        requirements = [Requirement('requests>=2.0.0')]
        python_versions = ['3.9']
        exclude_newer_than = datetime(2024, 1, 1, tzinfo=timezone.utc)

        with patch('proviso.main.Resolver') as mock_resolver_class:
            mock_resolver_instance = MagicMock()
            mock_resolver_instance.resolve.return_value = {
                'requests': {'version': '2.28.0', 'extras': []}
            }
            mock_resolver_class.return_value = mock_resolver_instance

            with patch('proviso.main.log'):
                find_requirements(
                    requirements,
                    python_versions,
                    exclude_newer_than=exclude_newer_than,
                )

                # Verify resolver.resolve was called with exclude_newer_than
                mock_resolver_instance.resolve.assert_called_once()
                call_kwargs = mock_resolver_instance.resolve.call_args[1]
                self.assertEqual(
                    exclude_newer_than, call_kwargs['exclude_newer_than']
                )

    def test_find_requirements_passes_allow_prereleases(self):
        """allow_prereleases defaults to False and is passed to the resolver."""
        requirements = [Requirement('requests>=2.0.0')]
        for kwargs, expected in (
            ({}, False),
            ({'allow_prereleases': True}, True),
        ):
            with patch('proviso.main.Resolver') as mock_resolver_class:
                mock_resolver = mock_resolver_class.return_value
                mock_resolver.resolve.return_value = {}
                with patch('proviso.main.log'):
                    find_requirements(requirements, ['3.9'], **kwargs)
                self.assertEqual(
                    expected,
                    mock_resolver.resolve.call_args.kwargs['allow_prereleases'],
                )

    def test_find_requirements_passes_extras(self):
        """extras defaults to empty and is passed to the resolver."""
        requirements = [Requirement('requests>=2.0.0')]
        for kwargs, expected in (({}, ()), ({'extras': ['dev']}, ['dev'])):
            with patch('proviso.main.Resolver') as mock_resolver_class:
                mock_resolver = mock_resolver_class.return_value
                mock_resolver.resolve.return_value = {}
                with patch('proviso.main.log'):
                    find_requirements(requirements, ['3.9'], **kwargs)
                self.assertEqual(
                    expected, mock_resolver.resolve.call_args.kwargs['extras']
                )


class TestParseAndValidateArgs(TestCase):
    def test_default_extras(self):
        """Test that extras default to all available extras."""
        mock_metadata = MagicMock()
        mock_metadata.provides_extra = ['dev', 'test', 'docs']

        mock_args = MagicMock()
        mock_args.directory = '/path/to/project'
        mock_args.extras = None
        mock_args.python_versions = '3.9,3.10'
        mock_args.filename = 'requirements.txt'
        mock_args.header = None
        mock_args.cache_db = None

        with patch('proviso.main.argv', ['proviso']):
            with patch('proviso.main.getenv', return_value=None):
                result = parse_and_validate_args(mock_metadata, mock_args)

        self.assertEqual(['dev', 'test', 'docs'], result['extras'])

    def test_default_extras_when_none(self):
        """Test that extras default to empty list when provides_extra is None."""
        mock_metadata = MagicMock()
        mock_metadata.provides_extra = None

        mock_args = MagicMock()
        mock_args.directory = '/path/to/project'
        mock_args.extras = None
        mock_args.python_versions = '3.9,3.10'
        mock_args.filename = 'requirements.txt'
        mock_args.header = None
        mock_args.cache_db = None

        with patch('proviso.main.argv', ['proviso']):
            with patch('proviso.main.getenv', return_value=None):
                result = parse_and_validate_args(mock_metadata, mock_args)

        self.assertEqual([], result['extras'])

    def test_custom_extras(self):
        """Test parsing custom extras from command line."""
        mock_metadata = MagicMock()
        mock_metadata.provides_extra = ['dev', 'test', 'docs']

        mock_args = MagicMock()
        mock_args.directory = '/path/to/project'
        mock_args.extras = 'dev,test'
        mock_args.python_versions = '3.9,3.10'
        mock_args.filename = 'requirements.txt'
        mock_args.header = None
        mock_args.cache_db = None

        with patch('proviso.main.argv', ['proviso']):
            with patch('proviso.main.getenv', return_value=None):
                result = parse_and_validate_args(mock_metadata, mock_args)

        self.assertEqual({'dev', 'test'}, result['extras'])

    def test_invalid_extras_exits(self):
        """Test that invalid extras cause exit."""
        mock_metadata = MagicMock()
        mock_metadata.provides_extra = ['dev', 'test']

        mock_args = MagicMock()
        mock_args.directory = '/path/to/project'
        mock_args.extras = 'dev,invalid'
        mock_args.python_versions = '3.9'
        mock_args.filename = 'requirements.txt'
        mock_args.header = None
        mock_args.cache_db = None

        with patch('proviso.main.exit') as mock_exit:
            with patch('proviso.main.log') as mock_log:
                parse_and_validate_args(mock_metadata, mock_args)

                mock_log.error.assert_called_once()
                error_msg = mock_log.error.call_args[0][0]
                self.assertIn('invalid', error_msg)
                mock_exit.assert_called_once_with(1)

    def test_default_python_versions(self):
        """Test that Python versions default to active versions."""
        mock_metadata = MagicMock()
        mock_metadata.provides_extra = []

        mock_args = MagicMock()
        mock_args.directory = '/path/to/project'
        mock_args.extras = None
        mock_args.python_versions = None
        mock_args.filename = 'requirements.txt'
        mock_args.header = None
        mock_args.cache_db = None

        with patch('proviso.main.Python') as mock_python_class:
            mock_python = MagicMock()
            mock_python.active = [
                {'cycle': '3.9'},
                {'cycle': '3.10'},
                {'cycle': '3.11'},
            ]
            mock_python_class.return_value = mock_python

            with patch('proviso.main.argv', ['proviso']):
                result = parse_and_validate_args(mock_metadata, mock_args)

            self.assertEqual(['3.9', '3.10', '3.11'], result['python_versions'])

    def test_custom_python_versions(self):
        """Test parsing custom Python versions."""
        mock_metadata = MagicMock()
        mock_metadata.provides_extra = []

        mock_args = MagicMock()
        mock_args.directory = '/path/to/project'
        mock_args.extras = None
        mock_args.python_versions = '3.8,3.9'
        mock_args.filename = 'requirements.txt'
        mock_args.header = None
        mock_args.cache_db = None

        with patch('proviso.main.argv', ['proviso']):
            with patch('proviso.main.getenv', return_value=None):
                result = parse_and_validate_args(mock_metadata, mock_args)

        self.assertEqual(['3.8', '3.9'], result['python_versions'])

    def test_directory_expansion(self):
        """Test that ~ is expanded in directory path."""
        mock_metadata = MagicMock()
        mock_metadata.provides_extra = []

        mock_args = MagicMock()
        mock_args.directory = '~/project'
        mock_args.extras = None
        mock_args.python_versions = '3.9'
        mock_args.filename = 'requirements.txt'
        mock_args.header = None
        mock_args.cache_db = None

        with patch('proviso.main.expanduser') as mock_expanduser:
            mock_expanduser.return_value = '/home/user/project'

            with patch('proviso.main.argv', ['proviso']):
                result = parse_and_validate_args(mock_metadata, mock_args)

            # Verify expanduser was called for directory
            mock_expanduser.assert_called_with('~/project')
            self.assertEqual('/home/user/project', result['directory'])

    def test_filename_without_directory(self):
        """Test that filename without directory is placed in project directory."""
        mock_metadata = MagicMock()
        mock_metadata.provides_extra = []

        mock_args = MagicMock()
        mock_args.directory = '/path/to/project'
        mock_args.extras = None
        mock_args.python_versions = '3.9'
        mock_args.filename = 'requirements.txt'
        mock_args.header = None
        mock_args.cache_db = None

        with patch('proviso.main.argv', ['proviso']):
            with patch('proviso.main.getenv', return_value=None):
                result = parse_and_validate_args(mock_metadata, mock_args)

        self.assertEqual(
            '/path/to/project/requirements.txt', result['output_path']
        )

    def test_filename_with_directory(self):
        """Test that filename with directory is used as-is."""
        mock_metadata = MagicMock()
        mock_metadata.provides_extra = []

        mock_args = MagicMock()
        mock_args.directory = '/path/to/project'
        mock_args.extras = None
        mock_args.python_versions = '3.9'
        mock_args.filename = '/tmp/requirements.txt'
        mock_args.header = None
        mock_args.cache_db = None

        with patch('proviso.main.argv', ['proviso']):
            with patch('proviso.main.getenv', return_value=None):
                result = parse_and_validate_args(mock_metadata, mock_args)

        self.assertEqual('/tmp/requirements.txt', result['output_path'])

    def test_default_header(self):
        """Test default header generation."""
        mock_metadata = MagicMock()
        mock_metadata.provides_extra = []

        mock_args = MagicMock()
        mock_args.directory = '/path/to/project'
        mock_args.extras = None
        mock_args.python_versions = '3.9'
        mock_args.filename = 'requirements.txt'
        mock_args.header = None
        mock_args.cache_db = None

        with patch('proviso.main.argv', ['proviso', '--directory', '/foo']):
            with patch('proviso.main.getenv', return_value=None):
                result = parse_and_validate_args(mock_metadata, mock_args)

        expected = (
            '# DO NOT EDIT THIS FILE DIRECTLY - use proviso --directory /foo\n'
        )
        self.assertEqual(expected, result['header'])

    def test_custom_header_with_placeholder(self):
        """Test custom header with [command line] placeholder."""
        mock_metadata = MagicMock()
        mock_metadata.provides_extra = []

        mock_args = MagicMock()
        mock_args.directory = '/path/to/project'
        mock_args.extras = None
        mock_args.python_versions = '3.9'
        mock_args.filename = 'requirements.txt'
        mock_args.header = '# Custom header [command line]'
        mock_args.cache_db = None

        with patch('proviso.main.argv', ['proviso', '--header', 'test']):
            with patch('proviso.main.getenv', return_value=None):
                result = parse_and_validate_args(mock_metadata, mock_args)

        expected = '# Custom header proviso --header test\n'
        self.assertEqual(expected, result['header'])

    def test_custom_header_no_trailing_newline(self):
        """Test that custom header gets newline added."""
        mock_metadata = MagicMock()
        mock_metadata.provides_extra = []

        mock_args = MagicMock()
        mock_args.directory = '/path/to/project'
        mock_args.extras = None
        mock_args.python_versions = '3.9'
        mock_args.filename = 'requirements.txt'
        mock_args.header = '# Custom header'
        mock_args.cache_db = None

        with patch('proviso.main.argv', ['proviso']):
            with patch('proviso.main.getenv', return_value=None):
                result = parse_and_validate_args(mock_metadata, mock_args)

        self.assertTrue(result['header'].endswith('\n'))

    def test_empty_custom_header(self):
        """Test that empty custom header is handled."""
        mock_metadata = MagicMock()
        mock_metadata.provides_extra = []

        mock_args = MagicMock()
        mock_args.directory = '/path/to/project'
        mock_args.extras = None
        mock_args.python_versions = '3.9'
        mock_args.filename = 'requirements.txt'
        mock_args.header = ''
        mock_args.cache_db = None

        with patch('proviso.main.argv', ['proviso']):
            with patch('proviso.main.getenv', return_value=None):
                result = parse_and_validate_args(mock_metadata, mock_args)

        # Empty header should remain empty
        self.assertEqual('', result['header'])


class TestWriteRequirementsToFile(TestCase):
    def test_write_simple_requirements(self):
        """Test writing simple requirements without version markers."""
        versions = {
            'package-a': {'1.0.0': ['3.9', '3.10', '3.11']},
            'package-b': {'2.0.0': ['3.9', '3.10', '3.11']},
        }
        python_versions = ['3.9', '3.10', '3.11']

        with TemporaryDirectory() as tmpdir:
            output_file = join(tmpdir, 'requirements.txt')
            write_requirements_to_file(versions, python_versions, output_file)

            with open(output_file) as fh:
                content = fh.read()

            expected = "x-python-version-not-supported; python_version!='3.9' and python_version!='3.10' and python_version!='3.11'\npackage-a==1.0.0\npackage-b==2.0.0\n"
            self.assertEqual(expected, content)

    def test_write_with_version_markers(self):
        """Test writing requirements with Python version markers."""
        versions = {
            'package-a': {'1.0.0': ['3.9', '3.10']},
            'package-b': {'2.0.0': ['3.11']},
        }
        python_versions = ['3.9', '3.10', '3.11']

        with TemporaryDirectory() as tmpdir:
            output_file = join(tmpdir, 'requirements.txt')
            write_requirements_to_file(versions, python_versions, output_file)

            with open(output_file) as fh:
                content = fh.read()

            # package-a should have version markers
            self.assertIn(
                "package-a==1.0.0; python_version=='3.9' or python_version=='3.10'",
                content,
            )
            # package-b should have version marker
            self.assertIn("package-b==2.0.0; python_version=='3.11'", content)

    def test_write_with_header(self):
        """Test writing requirements with a header."""
        versions = {'package-a': {'1.0.0': ['3.9', '3.10', '3.11']}}
        python_versions = ['3.9', '3.10', '3.11']
        header = '# This is a test header\n'

        with TemporaryDirectory() as tmpdir:
            output_file = join(tmpdir, 'requirements.txt')
            write_requirements_to_file(
                versions, python_versions, output_file, header=header
            )

            with open(output_file) as fh:
                content = fh.read()

            expected = "# This is a test header\nx-python-version-not-supported; python_version!='3.9' and python_version!='3.10' and python_version!='3.11'\npackage-a==1.0.0\n"
            self.assertEqual(expected, content)

    def test_write_with_header_no_trailing_newline(self):
        """Test that headers without trailing newlines get one added."""
        versions = {'package-a': {'1.0.0': ['3.9', '3.10', '3.11']}}
        python_versions = ['3.9', '3.10', '3.11']
        header = '# This is a test header'

        with TemporaryDirectory() as tmpdir:
            output_file = join(tmpdir, 'requirements.txt')
            write_requirements_to_file(
                versions, python_versions, output_file, header=header
            )

            with open(output_file) as fh:
                content = fh.read()

            expected = "# This is a test header\nx-python-version-not-supported; python_version!='3.9' and python_version!='3.10' and python_version!='3.11'\npackage-a==1.0.0\n"
            self.assertEqual(expected, content)

    def test_sorted_output(self):
        """Test that packages are written in sorted order."""
        versions = {
            'zebra': {'1.0.0': ['3.9', '3.10', '3.11']},
            'aardvark': {'1.0.0': ['3.9', '3.10', '3.11']},
            'monkey': {'1.0.0': ['3.9', '3.10', '3.11']},
        }
        python_versions = ['3.9', '3.10', '3.11']

        with TemporaryDirectory() as tmpdir:
            output_file = join(tmpdir, 'requirements.txt')
            write_requirements_to_file(versions, python_versions, output_file)

            with open(output_file) as fh:
                lines = fh.readlines()

            # Verify sorted order (skip first line which is x-python-version-not-supported marker)
            self.assertEqual('aardvark==1.0.0\n', lines[1])
            self.assertEqual('monkey==1.0.0\n', lines[2])
            self.assertEqual('zebra==1.0.0\n', lines[3])

    def test_multiple_versions_same_package(self):
        """Test package with different versions for different Python versions."""
        versions = {'package': {'1.0.0': ['3.9'], '1.1.0': ['3.10', '3.11']}}
        python_versions = ['3.9', '3.10', '3.11']

        with TemporaryDirectory() as tmpdir:
            output_file = join(tmpdir, 'requirements.txt')
            write_requirements_to_file(versions, python_versions, output_file)

            with open(output_file) as fh:
                content = fh.read()

            # Should have both versions with appropriate markers
            self.assertIn("package==1.0.0; python_version=='3.9'", content)
            self.assertIn(
                "package==1.1.0; python_version=='3.10' or python_version=='3.11'",
                content,
            )

    def _write(self, versions, python_versions):
        with TemporaryDirectory() as tmpdir:
            output_file = join(tmpdir, 'requirements.txt')
            write_requirements_to_file(versions, python_versions, output_file)
            with open(output_file) as fh:
                return fh.read()

    def _selected(self, content, full_version):
        """Return the lines of content whose marker is true for a Python."""
        env = {
            'python_version': '.'.join(full_version.split('.')[:2]),
            'python_full_version': full_version,
        }
        requirements = [Requirement(line) for line in content.splitlines()]
        return [
            f'{r.name}{r.specifier}'
            for r in requirements
            if r.marker is None or r.marker.evaluate(env)
        ]

    def test_write_patch_versions(self):
        versions = {
            'a': {'1.0': ['3.12.4', '3.12.5']},
            'b': {'1.0': ['3.12.4'], '2.0': ['3.12.5']},
        }
        content = self._write(versions, ['3.12.4', '3.12.5'])
        self.assertEqual(
            "x-python-version-not-supported; python_full_version!='3.12.4' and python_full_version!='3.12.5'\n"
            'a==1.0\n'
            "b==1.0; python_full_version=='3.12.4'\n"
            "b==2.0; python_full_version=='3.12.5'\n",
            content,
        )
        self.assertEqual(
            ['a==1.0', 'b==1.0'], self._selected(content, '3.12.4')
        )
        self.assertEqual(
            ['a==1.0', 'b==2.0'], self._selected(content, '3.12.5')
        )
        self.assertEqual(
            ['x-python-version-not-supported', 'a==1.0'],
            self._selected(content, '3.12.6'),
        )

    def test_write_major_versions(self):
        versions = {'a': {'1.0': ['2'], '2.0': ['3']}}
        content = self._write(versions, ['2', '3'])
        self.assertEqual(
            "x-python-version-not-supported; (python_version<'2' or python_version>='3') and (python_version<'3' or python_version>='4')\n"
            "a==1.0; python_version>='2' and python_version<'3'\n"
            "a==2.0; python_version>='3' and python_version<'4'\n",
            content,
        )
        self.assertEqual(['a==1.0'], self._selected(content, '2.7.18'))
        self.assertEqual(['a==2.0'], self._selected(content, '3.0.0'))
        self.assertEqual(['a==2.0'], self._selected(content, '3.14.1'))
        self.assertEqual(
            ['x-python-version-not-supported'], self._selected(content, '4.0.0')
        )
        self.assertEqual(
            ['x-python-version-not-supported'], self._selected(content, '1.6.1')
        )

    def test_write_mixed_versions(self):
        versions = {
            'a': {'1.0': ['2', '3.12.5'], '2.0': ['3.11']},
            'b': {'1.0': ['2', '3.11', '3.12.5']},
        }
        content = self._write(versions, ['2', '3.11', '3.12.5'])
        self.assertEqual(
            "x-python-version-not-supported; (python_version<'2' or python_version>='3') and python_version!='3.11' and python_full_version!='3.12.5'\n"
            "a==1.0; python_version>='2' and python_version<'3' or python_full_version=='3.12.5'\n"
            "a==2.0; python_version=='3.11'\n"
            'b==1.0\n',
            content,
        )
        self.assertEqual(
            ['a==1.0', 'b==1.0'], self._selected(content, '2.7.18')
        )
        self.assertEqual(
            ['a==2.0', 'b==1.0'], self._selected(content, '3.11.4')
        )
        self.assertEqual(
            ['a==1.0', 'b==1.0'], self._selected(content, '3.12.5')
        )
        for full_version in ('3.12.4', '3.13.0', '1.6.1'):
            self.assertEqual(
                ['x-python-version-not-supported', 'b==1.0'],
                self._selected(content, full_version),
            )


class TestDirectUrl(TestCase):
    URL = 'https://example.com/a-1.0-py3-none-any.whl#sha256=abc'

    def _write(self, versions, python_versions):
        with TemporaryDirectory() as tmpdir:
            output_file = join(tmpdir, 'requirements.txt')
            write_requirements_to_file(versions, python_versions, output_file)
            with open(output_file) as fh:
                return fh.read().splitlines()[1:]

    def test_find_requirements(self):
        with patch('proviso.main.Resolver') as mock_resolver_class:
            mock_resolver_class.return_value.resolve.side_effect = [
                {'a': {'version': '1.0', 'extras': [], 'url': self.URL}},
                {'a': {'version': '2.0', 'extras': []}},
                {'a': {'version': '1.0', 'extras': ['x'], 'url': self.URL}},
            ]
            with patch('proviso.main.log'):
                result = find_requirements(
                    [Requirement('a')], ['3.11', '3.12', '3.13']
                )
        self.assertEqual(
            {self.URL: ['3.11', '3.13'], '2.0': ['3.12']}, result['a']
        )
        keys = list(result['a'])
        self.assertIsInstance(keys[0], DirectUrl)
        self.assertNotIsInstance(keys[1], DirectUrl)

    def test_write_without_marker(self):
        lines = self._write(
            {'a': {DirectUrl(self.URL): ['3.12', '3.13']}}, ['3.12', '3.13']
        )
        self.assertEqual([f'a @ {self.URL}'], lines)
        requirement = Requirement(lines[0])
        self.assertEqual(self.URL, requirement.url)
        self.assertIsNone(requirement.marker)

    def test_write_mixed(self):
        lines = self._write(
            {
                'a': {DirectUrl(self.URL): ['3.11', '3.13'], '2.0': ['3.12']},
                'b': {'1.0': ['3.11', '3.12', '3.13']},
            },
            ['3.11', '3.12', '3.13'],
        )
        self.assertEqual(
            [
                f"a @ {self.URL} ; python_version=='3.11' or python_version=='3.13'",
                "a==2.0; python_version=='3.12'",
                'b==1.0',
            ],
            lines,
        )
        requirements = [Requirement(line) for line in lines]
        self.assertEqual(self.URL, requirements[0].url)
        for python_version, expected in (
            ('3.11', ['a', 'b']),
            ('3.12', ['a', 'b']),
            ('3.13', ['a', 'b']),
        ):
            env = {'python_version': python_version}
            selected = [
                r
                for r in requirements
                if r.marker is None or r.marker.evaluate(env)
            ]
            self.assertEqual(expected, [r.name for r in selected])
            self.assertEqual(
                python_version != '3.12', selected[0].url == self.URL
            )
