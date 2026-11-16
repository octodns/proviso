from functools import cached_property
from os import environ
from os.path import isfile, join
from subprocess import CalledProcessError, run
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from build import ProjectBuilder
from build.util import project_wheel_metadata
from packaging.metadata import Metadata, parse_email
from packaging.version import Version

# PEP 643: from Metadata-Version 2.2 on, an sdist's PKG-INFO fields are
# reliable unless they're listed in Dynamic.
_RELIABLE_PKG_INFO_VERSION = Version('2.2')


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


def wheel_metadata(path):
    """Return the Metadata in a wheel's .dist-info/METADATA, or None."""
    with ZipFile(path) as zf:
        for name in zf.namelist():
            directory, _, filename = name.partition('/')
            if directory.endswith('.dist-info') and filename == 'METADATA':
                return Metadata.from_email(zf.read(name), validate=False)
    return None


def sdist_pkg_info_metadata(source_dir):
    """Return the Metadata in an unpacked sdist's PKG-INFO.

    Returns None when there's no PKG-INFO or when its Requires-Dist can't be
    trusted under PEP 643: Metadata-Version is older than 2.2 or Requires-Dist
    is listed in Dynamic.
    """
    path = join(source_dir, 'PKG-INFO')
    if not isfile(path):
        return None
    with open(path, 'rb') as f:
        data = f.read()

    raw, _ = parse_email(data)
    metadata_version = raw.get('metadata_version', '0')
    dynamic = {d.lower() for d in raw.get('dynamic', [])}
    if (
        Version(metadata_version) < _RELIABLE_PKG_INFO_VERSION
        or 'requires-dist' in dynamic
    ):
        return None

    return Metadata.from_email(data, validate=False)


def build_sdist_metadata(source_dir):
    """Ask an unpacked sdist's build backend for its metadata.

    The backend runs in an isolated build environment, as pip does. Known
    limitation (#53): that environment uses the interpreter running proviso
    rather than the target Python, so a backend that computes dependencies
    from the running interpreter reports them for this one, and the result is
    reused for every target Python.
    """
    message = project_wheel_metadata(source_dir, isolated=True)
    # The message can't be reliably serialized back to text (multi-line
    # fields fail to fold), so only carry over what proviso uses.
    raw = {'requires_dist': message.get_all('Requires-Dist') or []}
    for field in ('Name', 'Version', 'Requires-Python'):
        if message.get(field) is not None:
            raw[field.lower().replace('-', '_')] = message[field]
    return Metadata.from_raw(raw, validate=False)


def build_failure_reason(exception):
    """Describe a build failure, including any captured process output."""
    reason = str(exception) or repr(exception)
    # build wraps subprocess failures in BuildBackendException and
    # FailedProcessError, both of which keep the original in .exception
    cpe = getattr(exception, 'exception', exception)
    if isinstance(cpe, CalledProcessError):
        for label, output in (('stdout', cpe.output), ('stderr', cpe.stderr)):
            if not output:
                continue
            if isinstance(output, bytes):
                output = output.decode('utf-8', errors='replace')
            reason += f'''

captured {label}:
--------------------------------------------------------------------------------
{output}
--------------------------------------------------------------------------------'''
    return reason
