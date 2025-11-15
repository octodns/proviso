from os.path import join
from subprocess import CalledProcessError
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from packaging.requirements import Requirement

from build import BuildBackendException
from proviso.main import (
    build_project_metadata,
    find_requirements,
    format_and_print_metadata,
    get_requirements_with_extras,
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


class TestGetRequirementsWithExtras(TestCase):
    def test_no_requirements(self):
        """Test with metadata that has no requirements."""
        mock_metadata = MagicMock()
        mock_metadata.requires_dist = None

        result = get_requirements_with_extras(mock_metadata, [])

        self.assertEqual([], result)

    def test_base_requirements_only(self):
        """Test with only base requirements (no markers)."""
        req1 = Requirement('requests>=2.0.0')
        req2 = Requirement('urllib3>=1.26.0')

        mock_metadata = MagicMock()
        mock_metadata.requires_dist = [req1, req2]

        result = get_requirements_with_extras(mock_metadata, [])

        self.assertEqual([req1, req2], result)

    def test_requirements_with_extras(self):
        """Test with requirements that have extra markers."""
        req1 = Requirement('requests>=2.0.0')
        req2 = Requirement('pytest>=7.0.0; extra == "dev"')
        req3 = Requirement('black>=22.0.0; extra == "dev"')
        req4 = Requirement('sphinx>=4.0.0; extra == "docs"')

        mock_metadata = MagicMock()
        mock_metadata.requires_dist = [req1, req2, req3, req4]

        # Request dev extra
        result = get_requirements_with_extras(mock_metadata, ['dev'])

        # Should include base requirement and dev extras
        self.assertEqual(3, len(result))
        self.assertIn(req1, result)
        self.assertIn(req2, result)
        self.assertIn(req3, result)
        self.assertNotIn(req4, result)

    def test_multiple_extras(self):
        """Test with multiple extras requested."""
        req1 = Requirement('requests>=2.0.0')
        req2 = Requirement('pytest>=7.0.0; extra == "dev"')
        req3 = Requirement('sphinx>=4.0.0; extra == "docs"')

        mock_metadata = MagicMock()
        mock_metadata.requires_dist = [req1, req2, req3]

        result = get_requirements_with_extras(mock_metadata, ['dev', 'docs'])

        # Should include all requirements
        self.assertEqual(3, len(result))
        self.assertIn(req1, result)
        self.assertIn(req2, result)
        self.assertIn(req3, result)


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

            expected = 'package-a==1.0.0\npackage-b==2.0.0\n'
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

            expected = '# This is a test header\npackage-a==1.0.0\n'
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

            expected = '# This is a test header\npackage-a==1.0.0\n'
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

            # Verify sorted order
            self.assertEqual('aardvark==1.0.0\n', lines[0])
            self.assertEqual('monkey==1.0.0\n', lines[1])
            self.assertEqual('zebra==1.0.0\n', lines[2])

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
