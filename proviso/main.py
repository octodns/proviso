from argparse import ArgumentParser
from collections import defaultdict
from logging import DEBUG, INFO, WARNING, basicConfig, getLogger
from os import getcwd
from os.path import dirname, expanduser, join
from sys import exit

from build import BuildBackendException
from proviso.builder import Builder
from proviso.python import Python
from proviso.resolver import Resolver

log = getLogger('proviso')


def build_project_metadata(directory):
    """Build project metadata and handle exceptions."""
    builder = Builder(directory)
    try:
        return builder.metadata
    except BuildBackendException as e:
        cpe = e.exception
        log.error(
            f'''Failed to build project.

captured stdout:
--------------------------------------------------------------------------------
{cpe.output.decode('utf-8')}
--------------------------------------------------------------------------------
captured stderr:
--------------------------------------------------------------------------------
{cpe.stderr.decode('utf-8')}
--------------------------------------------------------------------------------
'''
        )
        exit(1)


def format_and_print_metadata(metadata, extras, python_versions):
    """Format and print project metadata."""
    log.info(
        f'''Project: {metadata.name} {metadata.version}
  extras: {', '.join(extras)}
  python_versions: {', '.join(python_versions)}
'''
    )


def get_requirements_with_extras(metadata, extras):
    """Get requirements including extras."""
    requires_dist = metadata.requires_dist or []

    # base runtime direct requirements
    requirements = [r for r in requires_dist if r.marker is None]
    for extra in extras:
        requirements.extend(
            r
            for r in requires_dist
            if r.marker and r.marker.evaluate({'extra': extra})
        )

    return requirements


def find_requirements(requirements, python_versions):

    if not requirements:
        return {}

    log.info(
        f'''Requirements:
  {'\n  '.join([str(r) for r in requirements])}
'''
    )

    # Create one resolver instance (shared cache across all Python versions)
    resolver = Resolver()

    # Collect versions: versions[package][version] = [python_versions]
    versions = defaultdict(lambda: defaultdict(list))

    # Resolve for each Python version
    for python_version in python_versions:
        log.info(f'  Python {python_version}:')
        log.info('    Resolving dependencies...')

        resolved = resolver.resolve(requirements, python_version=python_version)

        log.info(f'    Resolved {len(resolved)} dependencies')

        # Accumulate into versions dict
        for name, info in resolved.items():
            versions[name][info['version']].append(python_version)

    log.info('')

    python_versions = set(python_versions)

    return versions


def write_requirements_to_file(versions, python_versions, filename):
    """Write resolved requirements to file."""
    with open(filename, 'w') as fh:  # Changed to 'w' mode for writing
        for pkg, vers in sorted(versions.items()):
            for ver, pythons in vers.items():
                # same pkg ver for all pythons
                fh.write(pkg)
                fh.write('==')
                fh.write(ver)
                if pythons != python_versions:
                    fh.write('; ')
                    for i, python in enumerate(pythons):
                        if i:
                            fh.write(' or ')
                        fh.write("python_version=='")
                        fh.write(python)
                        fh.write("'")
                fh.write('\n')


def main():
    parser = ArgumentParser(
        description='Extract project metadata and resolve dependencies'
    )
    parser.add_argument(
        '--directory',
        default=getcwd(),
        help='Project root directory (default: current directory)',
    )
    parser.add_argument(
        '--extras',
        default=None,
        help='Comma-separated list of extras to include (e.g., "dev,test"). Defaults to all defined extras extras.',
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
    parser.add_argument(
        '--level',
        default='INFO',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        help='Logging level (default: INFO)',
    )

    args = parser.parse_args()

    # Configure logging
    level_map = {'DEBUG': DEBUG, 'INFO': INFO, 'WARNING': WARNING}
    basicConfig(
        level=level_map.get(args.level, INFO),
        format='%(levelname)-7s: %(message)s',
    )

    # Suppress verbose httpx logging unless we're in DEBUG mode
    if args.level != 'DEBUG':
        getLogger('httpx').setLevel(WARNING)

    # Expand user path (e.g., ~ -> /home/username)
    directory = expanduser(args.directory)

    # Build project metadata
    metadata = build_project_metadata(directory)

    # Parse extras from command line
    if args.extras is None:
        extras = metadata.provides_extra
    else:
        extras = set(e.strip() for e in args.extras.split(',') if e.strip())

    if args.python_versions is None:
        # If no python versions specified, use active versions from endoflife.date
        python = Python()
        python_versions = [release['cycle'] for release in python.active]
    else:
        python_versions = [
            v.strip() for v in args.python_versions.split(',') if v.strip()
        ]

    format_and_print_metadata(metadata, extras, python_versions)

    requirements = get_requirements_with_extras(metadata, extras)

    versions = find_requirements(requirements, python_versions=python_versions)

    # Determine output path: if filename has a directory component, use as-is;
    # otherwise place it in the project directory
    filename = args.filename
    if dirname(filename):
        output_path = expanduser(filename)
    else:
        output_path = join(directory, filename)

    write_requirements_to_file(versions, python_versions, output_path)


if __name__ == '__main__':
    main()
