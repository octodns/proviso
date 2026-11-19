from argparse import ArgumentParser, ArgumentTypeError
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from itertools import combinations
from logging import DEBUG, ERROR, INFO, WARNING, basicConfig, getLogger
from os import getcwd, getenv
from os.path import dirname, expanduser, join
from sys import argv, exit

import httpx2
from build import BuildBackendException
from resolvelib import ResolutionImpossible, ResolutionTooDeep

from proviso.builder import Builder
from proviso.python import Python
from proviso.resolver import (
    MetadataUnavailableError,
    Resolver,
    UnsupportedRequirementError,
    is_exact_pin,
    make_identifier,
    redact_requirement,
)
from proviso.series import PythonSeries
from proviso.utils import CachingClient

log = getLogger('proviso')


def build_project_metadata(directory):
    """Build project metadata and handle exceptions."""
    builder = Builder(directory)
    try:
        return builder.metadata
    except BuildBackendException as e:
        cpe = e.exception
        log.error(f'''Failed to build project.

captured stdout:
--------------------------------------------------------------------------------
{cpe.output.decode('utf-8')}
--------------------------------------------------------------------------------
captured stderr:
--------------------------------------------------------------------------------
{cpe.stderr.decode('utf-8')}
--------------------------------------------------------------------------------
''')
        exit(1)


def format_and_print_metadata(metadata, extras, python_versions):
    """Format and print project metadata."""
    log.info(
        f'Project: {metadata.name} {metadata.version}, '
        f'extras: {", ".join(extras)}, '
        f'python_versions: {", ".join(python_versions)}'
    )


def non_negative_int(value):
    """argparse type for an option that takes zero or more days."""
    try:
        number = int(value)
    except ValueError:
        raise ArgumentTypeError(f'invalid int value: {value!r}') from None
    if number < 0:
        raise ArgumentTypeError(f'must be 0 or greater, got {number}')
    return number


def _http_error_detail(error):
    """Describe an httpx2 transport or status error without the URL."""
    if isinstance(error, httpx2.HTTPStatusError):
        response = error.response
        return f'HTTP {response.status_code} {response.reason_phrase}'
    return str(error) or type(error).__name__


def _http_error_message(error):
    """Describe an httpx2 transport or status error, naming the URL if known."""
    detail = _http_error_detail(error)
    try:
        return f'{error.request.url}: {detail}'
    except RuntimeError:
        # the request was never attached to the error
        return detail


def _resolution_error_message(error, python_version, cooldown=False):
    """Return the one error message that describes a resolution failure."""
    prefix = f'Unable to resolve dependencies for Python {python_version}'
    if isinstance(error, ResolutionImpossible):
        # resolvelib records one cause per rejected release of the parent, so
        # the number of causes says nothing about whether anything conflicts
        causes = [
            (
                str(cause.requirement),
                (
                    f'{make_identifier(cause.parent.name, cause.parent.extras)}'
                    f'=={cause.parent.version}'
                    if cause.parent is not None
                    else 'the project'
                ),
            )
            for cause in error.causes
        ]
        if len({requirement for requirement, _ in causes}) == 1:
            # one requirement nothing satisfies, not a conflict
            parents = list(dict.fromkeys(parent for _, parent in causes))
            shown = ', '.join(parents[:3])
            if len(parents) > 3:
                shown = f'{shown} and {len(parents) - 3} more'
            # the cooldown exempts a version a project requirement pins
            # exactly, so it can't be why no version of that matched
            exempt = any(
                cause.parent is None
                and any(is_exact_pin(s) for s in cause.requirement.specifier)
                for cause in error.causes
            )
            hint = (
                '; releases inside the --cooldown-days window are excluded'
                if cooldown and not exempt
                else ''
            )
            return (
                f'{prefix}: no version of {error.causes[0].requirement.name} '
                f'satisfies {causes[0][0]} (from {shown}){hint}'
            )
        conflicts = '\n'.join(
            f'  {requirement} (from {parent})'
            for requirement, parent in dict.fromkeys(causes)
        )
        return f'{prefix}, these requirements conflict:\n{conflicts}'
    if isinstance(error, ResolutionTooDeep):
        return (
            f'{prefix}: gave up after the limit of {error.round_count} '
            'resolution rounds'
        )
    if isinstance(error, httpx2.HTTPError):
        return f'{prefix}: request failed: {_http_error_message(error)}'
    return f'{prefix}: {error}'


def find_requirements(
    requirements,
    python_versions,
    session=None,
    exclude_newer_than=None,
    allow_prereleases=False,
    extras=(),
    build_sdists=False,
    project_name=None,
):

    if not requirements:
        return {}

    # Direct references aren't supported. Check every requirement, whatever
    # its marker, so the result doesn't depend on the target Pythons
    direct = [redact_requirement(r) for r in requirements if r.url is not None]
    if direct:
        log.error(
            'The project requires direct references, which proviso does not '
            f'support: {", ".join(direct)}'
        )
        exit(1)

    log.info(f'Requirements: {", ".join([str(r) for r in requirements])}')

    # Create one resolver instance (shared session across all Python versions)
    resolver = Resolver(session=session)

    # Collect versions: versions[package][version] = [python_versions]
    versions = defaultdict(lambda: defaultdict(list))

    # Resolve for each Python version
    for python_version in python_versions:
        log.info(f'Resolving dependencies for Python {python_version}')

        try:
            resolved = resolver.resolve(
                requirements,
                python_version=python_version,
                exclude_newer_than=exclude_newer_than,
                allow_prereleases=allow_prereleases,
                extras=extras,
                build_sdists=build_sdists,
                project_name=project_name,
            )
        except (
            MetadataUnavailableError,
            ResolutionImpossible,
            ResolutionTooDeep,
            UnsupportedRequirementError,
            httpx2.HTTPError,
        ) as e:
            log.error(
                _resolution_error_message(
                    e, python_version, cooldown=exclude_newer_than is not None
                )
            )
            exit(1)

        log.info(
            f'Resolved {len(resolved)} dependencies for Python {python_version}'
        )

        # Accumulate into versions dict
        for name, info in resolved.items():
            versions[name][info['version']].append(python_version)

    return versions


def write_requirements_to_file(
    versions, python_versions, filename, header=None
):
    """Write resolved requirements to file."""
    series = {v: PythonSeries(v) for v in python_versions}
    with open(filename, 'w') as fh:  # Changed to 'w' mode for writing
        if header:
            fh.write(header)
            if not header.endswith('\n'):
                fh.write('\n')

        fh.write('x-python-version-not-supported; ')
        fh.write(
            ' and '.join(series[v].guard_marker() for v in python_versions)
        )
        fh.write('\n')

        for pkg, vers in sorted(versions.items()):
            for ver, pythons in vers.items():
                # same pkg ver for all pythons
                fh.write(pkg)
                fh.write('==')
                fh.write(ver)
                if pythons != python_versions:
                    fh.write('; ')
                    fh.write(
                        ' or '.join(series[p].match_marker() for p in pythons)
                    )
                fh.write('\n')


def parse_and_validate_args(metadata, args, session=None):
    """Parse and validate command line arguments.

    Args:
        metadata: Project metadata
        args: Parsed command line arguments
        session: Optional cached HTTP client for API requests

    Returns a dict with parsed values:
    - directory: expanded directory path
    - extras: set of extras to include
    - python_versions: list of Python versions
    - output_path: full path for output file
    - header: header string to write to file
    """
    # Expand user path (e.g., ~ -> /home/username)
    directory = expanduser(args.directory)

    # Parse extras from command line
    if args.extras is None:
        extras = metadata.provides_extra or []
    else:
        extras = set(e.strip() for e in args.extras.split(',') if e.strip())
        # Validate that requested extras exist
        available_extras = set(metadata.provides_extra or [])
        invalid_extras = extras - available_extras
        if invalid_extras:
            log.error(
                f"Invalid extras requested: {', '.join(sorted(invalid_extras))}. "
                f"Available extras: {', '.join(sorted(available_extras)) if available_extras else 'none'}"
            )
            exit(1)

    if args.python_versions is None:
        # If no python versions specified, use active versions from endoflife.date
        python = Python(session=session)
        try:
            python_versions = [release['cycle'] for release in python.active]
        except httpx2.HTTPError as e:
            log.error(
                'Unable to look up the active Python versions from '
                f'{Python.API_URL}: {_http_error_detail(e)}'
            )
            exit(1)
            python_versions = []
    else:
        python_versions = [
            v.strip() for v in args.python_versions.split(',') if v.strip()
        ]

    series = []
    for v in python_versions:
        try:
            series.append((v, PythonSeries(v)))
        except ValueError:
            log.error(f'Invalid Python version {v!r}, expected X, X.Y or X.Y.Z')
            exit(1)
            break

    # Each release may be covered by only one target, otherwise the output
    # would pin a package twice for the same interpreter
    for (a, sa), (b, sb) in combinations(series, 2):
        if sa.overlaps(sb):
            log.error(
                f'Python versions {a} and {b} overlap; each release may be covered by only one target'
            )
            exit(1)
            break

    # Determine output path: if filename has a directory component, use as-is;
    # otherwise place it in the project directory
    filename = args.filename
    if dirname(filename):
        output_path = expanduser(filename)
    else:
        output_path = join(directory, filename)

    # Build the header
    if args.header is None:
        # Default header
        command_line = ' '.join(argv)
        header = f'# DO NOT EDIT THIS FILE DIRECTLY - use {command_line}\n'
    else:
        # User-provided header, replace [command line] placeholder
        command_line = ' '.join(argv)
        header = args.header.replace('[command line]', command_line)
        if header and not header.endswith('\n'):
            header += '\n'

    return {
        'directory': directory,
        'extras': extras,
        'python_versions': python_versions,
        'output_path': output_path,
        'header': header,
    }


def main():  # pragma: no cover
    parser = ArgumentParser(
        description='Extract project requirements, resolve dependencies, and manage requirements.txt'
    )
    parser.add_argument(
        '--directory',
        default=getcwd(),
        help='Project root directory (default: current directory)',
    )
    parser.add_argument(
        '--extras',
        default=None,
        help='Comma-separated list of extras to include (e.g., "dev,test"). Defaults to all defined extras.',
    )
    parser.add_argument(
        '--python-versions',
        default=None,
        help='Comma-separated list of Python versions (e.g., "3.9,3.10,3.11"). Defaults to currently active versions per endoflife.date.',
    )
    parser.add_argument(
        '--filename',
        default='requirements.txt',
        help='Output filename or path for requirements (default: requirements.txt). If just a filename, will be placed in --directory; if a path, will be used as-is.',
    )
    level_map = {
        'DEBUG': DEBUG,
        'INFO': INFO,
        'WARNING': WARNING,
        'ERROR': ERROR,
        'debug': DEBUG,
        'info': INFO,
        'warning': WARNING,
        'error': ERROR,
    }
    parser.add_argument(
        '--level',
        default='WARNING',
        choices=sorted(level_map.keys()),
        help='Logging level (default: WARNING)',
    )
    parser.add_argument(
        '--header',
        default=None,
        help='Header comment to place at the top of the requirements file. Use the string "[command line]" to insert the actual command that was run. (default: "# DO NOT EDIT THIS FILE DIRECTLY - use [command line]")',
    )
    parser.add_argument(
        '--cache-db',
        default=None,
        help='Path to SQLite database for persistent HTTP caching. If not specified, checks PROVISO_CACHE_DB environment variable. If neither is set, uses in-memory caching.',
    )
    parser.add_argument(
        '--cooldown-days',
        type=non_negative_int,
        default=7,
        help='Exclude releases first uploaded within this many days (dependency cooldown). Files added later to an older release are still used, and a version the project pins exactly with == or === is exempt. Default: 7. Use 0 to disable.',
    )
    parser.add_argument(
        '--pre',
        action='store_true',
        default=False,
        help='Include pre-release versions for all packages. By default, pre-releases are only used for a package whose specifier names one (e.g. "foo>=2.0b1").',
    )
    parser.add_argument(
        '--build-sdists',
        action='store_true',
        default=False,
        help="Allow proviso to run a package's build backend to learn its dependencies when neither the index nor the sdist provides usable metadata. The build runs under the Python running proviso, not the target Python, and executes the package's build code.",
    )

    args = parser.parse_args()

    # Configure logging
    basicConfig(
        level=level_map.get(args.level, WARNING),
        format='%(asctime)s [%(levelname)-8s] %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S',
    )

    # Suppress verbose httpx2 logging unless we're in DEBUG mode
    if args.level != 'DEBUG':
        getLogger('httpx2').setLevel(WARNING)

    # Determine cache database path
    # Priority: command line arg > environment variable > None (in-memory)
    cache_db_path = args.cache_db
    if cache_db_path is None:
        cache_db_path = getenv('PROVISO_CACHE_DB')
    if cache_db_path:
        cache_db_path = expanduser(cache_db_path)

    # Create shared caching client for all HTTP requests
    with CachingClient(cache_db_path=cache_db_path) as session:
        # Build project metadata
        metadata = build_project_metadata(args.directory)

        # Parse and validate arguments
        parsed = parse_and_validate_args(metadata, args, session=session)

        format_and_print_metadata(
            metadata, parsed['extras'], parsed['python_versions']
        )

        requirements = metadata.requires_dist or []

        # Calculate exclude_newer_than if cooldown is enabled
        exclude_newer_than = None
        if args.cooldown_days > 0:
            exclude_newer_than = datetime.now(timezone.utc) - timedelta(
                days=args.cooldown_days
            )
            log.info(
                f'Dependency cooldown: excluding packages uploaded after {exclude_newer_than.isoformat()}'
            )

        versions = find_requirements(
            requirements,
            python_versions=parsed['python_versions'],
            session=session,
            exclude_newer_than=exclude_newer_than,
            allow_prereleases=args.pre,
            extras=parsed['extras'],
            build_sdists=args.build_sdists,
            project_name=metadata.name,
        )

        write_requirements_to_file(
            versions,
            parsed['python_versions'],
            parsed['output_path'],
            header=parsed['header'],
        )


if __name__ == '__main__':  # pragma: no cover
    main()
