from email.message import Message
from os.path import join
from subprocess import CalledProcessError
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, mock_open, patch

from build import BuildBackendException
from packaging.metadata import Metadata
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

from proviso.builder import (
    Builder,
    _runner,
    build_failure_reason,
    build_sdist_metadata,
    sdist_pkg_info_metadata,
)


class TestRunner(TestCase):
    def test_runner_basic_command(self):
        """Test that _runner executes a basic command."""
        cmd = ['echo', 'test']

        with patch('proviso.builder.run') as mock_run:
            _runner(cmd)

            # Verify run was called with correct arguments
            mock_run.assert_called_once()
            call_args = mock_run.call_args

            self.assertEqual(cmd, call_args[0][0])
            self.assertIsNone(call_args[1]['cwd'])
            self.assertTrue(call_args[1]['capture_output'])
            self.assertTrue(call_args[1]['check'])

    def test_runner_with_cwd(self):
        """Test that _runner passes cwd correctly."""
        cmd = ['echo', 'test']
        cwd = '/some/directory'

        with patch('proviso.builder.run') as mock_run:
            _runner(cmd, cwd=cwd)

            call_args = mock_run.call_args
            self.assertEqual(cwd, call_args[1]['cwd'])

    def test_runner_with_extra_environ(self):
        """Test that _runner merges extra environment variables."""
        cmd = ['echo', 'test']
        extra_environ = {
            'TEST_VAR': 'test_value',
            'ANOTHER_VAR': 'another_value',
        }

        with patch('proviso.builder.run') as mock_run:
            with patch('proviso.builder.environ', {'EXISTING': 'value'}):
                _runner(cmd, extra_environ=extra_environ)

                call_args = mock_run.call_args
                env = call_args[1]['env']

                # Should have both existing and new variables
                self.assertEqual('value', env['EXISTING'])
                self.assertEqual('test_value', env['TEST_VAR'])
                self.assertEqual('another_value', env['ANOTHER_VAR'])

    def test_runner_preserves_existing_environ(self):
        """Test that _runner preserves existing environment variables."""
        cmd = ['echo', 'test']

        with patch('proviso.builder.run') as mock_run:
            with patch(
                'proviso.builder.environ', {'VAR1': 'val1', 'VAR2': 'val2'}
            ):
                _runner(cmd)

                call_args = mock_run.call_args
                env = call_args[1]['env']

                # Should preserve existing environment
                self.assertEqual('val1', env['VAR1'])
                self.assertEqual('val2', env['VAR2'])


class TestBuilder(TestCase):
    def test_init_sets_directory(self):
        """Test that Builder.__init__ sets the directory."""
        directory = '/path/to/project'
        builder = Builder(directory)

        self.assertEqual(directory, builder.directory)

    def test_metadata_builds_and_parses(self):
        """Test that metadata property builds and parses project metadata."""
        directory = '/path/to/project'
        builder = Builder(directory)

        # Sample METADATA content
        metadata_content = """Metadata-Version: 2.1
Name: test-package
Version: 1.0.0
Summary: A test package
Author: Test Author
Author-email: test@example.com
Requires-Dist: requests>=2.0.0
Requires-Dist: pytest>=7.0.0; extra == "dev"
Provides-Extra: dev
"""

        # Mock the temporary directory and file operations
        mock_tmpdir = '/tmp/test_tmpdir'
        metadata_path = '/tmp/metadata_path'

        with patch('proviso.builder.TemporaryDirectory') as mock_tempdir:
            # Setup TemporaryDirectory context manager
            mock_tempdir_cm = MagicMock()
            mock_tempdir_cm.__enter__.return_value = mock_tmpdir
            mock_tempdir.return_value = mock_tempdir_cm

            with patch(
                'proviso.builder.ProjectBuilder'
            ) as mock_project_builder:
                # Setup ProjectBuilder mock
                mock_pb_instance = MagicMock()
                mock_pb_instance.metadata_path.return_value = metadata_path
                mock_project_builder.return_value = mock_pb_instance

                with patch(
                    'builtins.open', mock_open(read_data=metadata_content)
                ):
                    metadata = builder.metadata

                    # Verify ProjectBuilder was instantiated correctly
                    mock_project_builder.assert_called_once_with(
                        directory, runner=_runner
                    )

                    # Verify metadata_path was called with tmpdir
                    mock_pb_instance.metadata_path.assert_called_once_with(
                        mock_tmpdir
                    )

                    # Verify metadata was parsed correctly
                    self.assertIsInstance(metadata, Metadata)
                    self.assertEqual('test-package', metadata.name)
                    self.assertEqual('1.0.0', str(metadata.version))
                    self.assertEqual('A test package', metadata.summary)

    def test_metadata_cached(self):
        """Test that metadata property is cached."""
        directory = '/path/to/project'
        builder = Builder(directory)

        metadata_content = """Metadata-Version: 2.1
Name: test-package
Version: 1.0.0
"""

        mock_tmpdir = '/tmp/test_tmpdir'
        metadata_path = '/tmp/metadata_path'

        with patch('proviso.builder.TemporaryDirectory') as mock_tempdir:
            mock_tempdir_cm = MagicMock()
            mock_tempdir_cm.__enter__.return_value = mock_tmpdir
            mock_tempdir.return_value = mock_tempdir_cm

            with patch(
                'proviso.builder.ProjectBuilder'
            ) as mock_project_builder:
                mock_pb_instance = MagicMock()
                mock_pb_instance.metadata_path.return_value = metadata_path
                mock_project_builder.return_value = mock_pb_instance

                with patch(
                    'builtins.open', mock_open(read_data=metadata_content)
                ):
                    # First access
                    metadata1 = builder.metadata

                    # Second access
                    metadata2 = builder.metadata

                    # ProjectBuilder should only be called once due to caching
                    mock_project_builder.assert_called_once()

                    # Both should return same object
                    self.assertIs(metadata1, metadata2)

    def test_metadata_with_complex_dependencies(self):
        """Test metadata parsing with complex dependency specifications."""
        directory = '/path/to/project'
        builder = Builder(directory)

        metadata_content = """Metadata-Version: 2.1
Name: complex-package
Version: 2.5.3
Requires-Dist: requests>=2.0.0,<3.0.0
Requires-Dist: numpy>=1.20.0; python_version>='3.8'
Requires-Dist: pytest>=7.0.0; extra == "test"
Requires-Dist: black>=22.0.0; extra == "dev"
Requires-Dist: mypy>=0.950; extra == "dev"
Provides-Extra: test
Provides-Extra: dev
"""

        mock_tmpdir = '/tmp/test_tmpdir'
        metadata_path = '/tmp/metadata_path'

        with patch('proviso.builder.TemporaryDirectory') as mock_tempdir:
            mock_tempdir_cm = MagicMock()
            mock_tempdir_cm.__enter__.return_value = mock_tmpdir
            mock_tempdir.return_value = mock_tempdir_cm

            with patch(
                'proviso.builder.ProjectBuilder'
            ) as mock_project_builder:
                mock_pb_instance = MagicMock()
                mock_pb_instance.metadata_path.return_value = metadata_path
                mock_project_builder.return_value = mock_pb_instance

                with patch(
                    'builtins.open', mock_open(read_data=metadata_content)
                ):
                    metadata = builder.metadata

                    # Verify complex metadata was parsed
                    self.assertEqual('complex-package', metadata.name)
                    self.assertEqual('2.5.3', str(metadata.version))

                    # Verify dependencies
                    self.assertIsNotNone(metadata.requires_dist)
                    dep_names = [
                        str(d)
                        .split(';')[0]
                        .split('>=')[0]
                        .split('<')[0]
                        .strip()
                        for d in metadata.requires_dist
                    ]
                    self.assertIn('requests', dep_names)
                    self.assertIn('numpy', dep_names)
                    self.assertIn('pytest', dep_names)

                    # Verify extras
                    self.assertIn('test', metadata.provides_extra)
                    self.assertIn('dev', metadata.provides_extra)

    def test_metadata_opens_correct_file(self):
        """Test that metadata opens the METADATA file in the correct location."""
        directory = '/path/to/project'
        builder = Builder(directory)

        metadata_content = """Metadata-Version: 2.1
Name: test-package
Version: 1.0.0
"""

        mock_tmpdir = '/tmp/test_tmpdir'
        metadata_path = '/tmp/metadata_path'

        with patch('proviso.builder.TemporaryDirectory') as mock_tempdir:
            mock_tempdir_cm = MagicMock()
            mock_tempdir_cm.__enter__.return_value = mock_tmpdir
            mock_tempdir.return_value = mock_tempdir_cm

            with patch(
                'proviso.builder.ProjectBuilder'
            ) as mock_project_builder:
                mock_pb_instance = MagicMock()
                mock_pb_instance.metadata_path.return_value = metadata_path
                mock_project_builder.return_value = mock_pb_instance

                with patch(
                    'builtins.open', mock_open(read_data=metadata_content)
                ) as mock_file:
                    with patch(
                        'proviso.builder.join',
                        return_value='/expected/path/METADATA',
                    ) as mock_join:
                        builder.metadata

                        # Verify join was called with correct arguments
                        mock_join.assert_called_once_with(
                            metadata_path, 'METADATA'
                        )

                        # Verify file was opened at the joined path
                        mock_file.assert_called_once_with(
                            '/expected/path/METADATA'
                        )


class TestBuildSdistMetadata(TestCase):
    def test_requires_dist(self):
        message = Message()
        message['Name'] = 'foo'
        message['Requires-Dist'] = 'six'
        message['Requires-Dist'] = 'pytest; extra == "test"'
        with patch(
            'proviso.builder.project_wheel_metadata', return_value=message
        ) as mock_metadata:
            metadata = build_sdist_metadata('/src')
        mock_metadata.assert_called_once_with('/src', isolated=True)
        self.assertEqual(
            [Requirement('six'), Requirement('pytest; extra == "test"')],
            metadata.requires_dist,
        )

    def test_name_version_and_requires_python(self):
        message = Message()
        message['Name'] = 'Foo_Bar'
        message['Version'] = '1.0rc1'
        message['Requires-Python'] = '>=3.10'
        with patch(
            'proviso.builder.project_wheel_metadata', return_value=message
        ):
            metadata = build_sdist_metadata('/src')
        self.assertEqual('Foo_Bar', metadata.name)
        self.assertEqual(Version('1.0rc1'), metadata.version)
        self.assertEqual(SpecifierSet('>=3.10'), metadata.requires_python)

    def test_no_requires_dist(self):
        with patch(
            'proviso.builder.project_wheel_metadata', return_value=Message()
        ):
            metadata = build_sdist_metadata('/src')
        self.assertEqual([], metadata.requires_dist)


class TestSdistPkgInfoMetadata(TestCase):
    def test_missing_metadata_version_is_unreliable(self):
        with TemporaryDirectory() as tmpdir:
            with open(join(tmpdir, 'PKG-INFO'), 'w') as fh:
                fh.write('Name: foo\nVersion: 1.0\nRequires-Dist: six\n')
            self.assertIsNone(sdist_pkg_info_metadata(tmpdir))


class TestBuildFailureReason(TestCase):
    def test_plain_exception(self):
        self.assertEqual('boom', build_failure_reason(ValueError('boom')))

    def test_exception_without_message(self):
        self.assertEqual('ValueError()', build_failure_reason(ValueError()))

    def test_called_process_error_text_output(self):
        error = CalledProcessError(1, ['pip'], output='pip output')
        reason = build_failure_reason(error)
        self.assertIn('captured stdout:', reason)
        self.assertIn('pip output', reason)
        self.assertNotIn('captured stderr:', reason)

    def test_wrapped_called_process_error_bytes_output(self):
        error = CalledProcessError(1, ['python'], stderr=b'bad \xff backend')
        reason = build_failure_reason(
            BuildBackendException(error, 'Backend failed')
        )
        self.assertTrue(reason.startswith('Backend failed'))
        self.assertNotIn('captured stdout:', reason)
        self.assertIn('captured stderr:', reason)
        self.assertIn('bad � backend', reason)
