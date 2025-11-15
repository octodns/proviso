from argparse import ArgumentParser
from collections import defaultdict
from os import getcwd
from os.path import expanduser
from sys import exit, stderr

from build import BuildBackendException
from proviso.builder import Builder
from proviso.python import Python
from proviso.resolver import Resolver


def build_project_metadata(directory):
    """Build project metadata and handle exceptions."""
    builder = Builder(directory)
    try:
        return builder.metadata
    except BuildBackendException as e:
        cpe = e.exception
        print(
            f'''Failed to build project.

captured stdout:
--------------------------------------------------------------------------------
{cpe.output.decode('utf-8')}
--------------------------------------------------------------------------------
captured stderr:
--------------------------------------------------------------------------------
{cpe.stderr.decode('utf-8')}
--------------------------------------------------------------------------------
''',
            file=stderr,
        )
        exit(1)


def format_and_print_metadata(metadata, extras, python_versions):
    """Format and print project metadata."""
    print(
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


def write_requirements_to_file(versions, python_versions):
    """Write resolved requirements to file."""
    filename = '/tmp/requirements.txt'
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
                        fh.write("python_version='")
                        fh.write(python)
                        fh.write("'")
                fh.write('\n')


def find_requirements(requirements, python_versions):

    if not requirements:
        return {}

    print(
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
        print(f'  Python {python_version}:')
        print('    Resolving dependencies...')

        resolved = resolver.resolve(requirements, python_version=python_version)

        print(f'    Resolved {len(resolved)} dependencies')

        # Accumulate into versions dict
        for name, info in resolved.items():
            versions[name][info['version']].append(python_version)

    print()

    python_versions = set(python_versions)

    return versions


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

    args = parser.parse_args()

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

    write_requirements_to_file(versions, python_versions)


if __name__ == '__main__':
    main()
