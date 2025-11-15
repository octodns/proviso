from unittest import TestCase
from unittest.mock import MagicMock, mock_open, patch

from packaging.metadata import Metadata

from proviso.builder import Builder, _runner


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
