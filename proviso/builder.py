from functools import cached_property
from os import environ
from os.path import join
from subprocess import run
from tempfile import TemporaryDirectory

from build import ProjectBuilder
from packaging.metadata import Metadata


def _runner(cmd, cwd=None, extra_environ=None):
    env = environ.copy()
    if extra_environ is not None:
        env.update(extra_environ)

    run(cmd, cwd=cwd, env=env, capture_output=True, check=True)


class Builder:
    def __init__(self, directory):
        self.directory = directory

    @cached_property
    def metadata(self):
        builder = ProjectBuilder(self.directory, runner=_runner)
        with TemporaryDirectory() as tmpdir:
            metadata_path = builder.metadata_path(tmpdir)
            with open(join(metadata_path, 'METADATA')) as f:
                return Metadata.from_email(f.read())
