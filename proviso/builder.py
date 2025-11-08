from functools import cached_property
from os.path import join
from tempfile import TemporaryDirectory

from packaging.metadata import Metadata

from build import ProjectBuilder


class Builder:
    def __init__(self, directory):
        self.directory = directory

    @cached_property
    def metadata(self):
        builder = ProjectBuilder(self.directory)
        with TemporaryDirectory() as tmpdir:
            metadata_path = builder.metadata_path(tmpdir)
            with open(join(metadata_path, 'METADATA')) as f:
                return Metadata.from_email(f.read())
