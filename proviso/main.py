from argparse import ArgumentParser
from os import getcwd
from pprint import pprint

from proviso.builder import Builder
from proviso.python import Python
from proviso.resolver import Resolver


def main():
    parser = ArgumentParser(description='Extract project metadata and resolve dependencies')
    parser.add_argument(
        '--directory',
        default=getcwd(),
        help='Project root directory (default: current directory)',
    )
    parser.add_argument(
        '--python-version',
        default=None,
        help='Target Python version for resolution (e.g., "3.9", "3.10.5"). Defaults to current Python version.',
    )
    parser.add_argument(
        '--extras',
        default='',
        help='Comma-separated list of extras to include (e.g., "dev,test"). Defaults to no extras.',
    )
    parser.add_argument(
        '--python-versions',
        default='',
        help='Comma-separated list of Python versions (e.g., "3.9,3.10,3.11"). Defaults to currently active versions per endoflife.date.',
    )

    args = parser.parse_args()

    # Build project metadata
    builder = Builder(args.directory)
    metadata = builder.metadata

    print(f'Project: {metadata.name} {metadata.version}')
    print()

    # Parse extras from command line
    extras = set(e.strip() for e in args.extras.split(',') if e.strip())

    # Parse python versions from command line
    python_versions = [v.strip() for v in args.python_versions.split(',') if v.strip()]

    # If no python versions specified, use active versions from endoflife.date
    if not python_versions:
        python = Python()
        python_versions = [release['cycle'] for release in python.active]

    # Filter requirements based on extras
    requirements = []
    for req in (metadata.requires_dist or []):
        if req.marker is None:
            # No marker - always include
            requirements.append(req)
        elif 'extra' in str(req.marker):
            # Has extra marker - include if any requested extra matches
            for extra in extras:
                if req.marker.evaluate({'extra': extra}):
                    requirements.append(req)
                    break
        else:
            # Has other marker (python_version, platform, etc.) - include
            requirements.append(req)

    if requirements:
        print(f'Runtime requirements: {[str(r) for r in requirements]}')
        print()
        print('Resolving dependencies...')

        # Resolve dependencies
        resolver = Resolver(python_version=args.python_version)
        resolved = resolver.resolve(requirements)

        print()
        print('Resolved dependencies:')
        for name, info in sorted(resolved.items()):
            print(f'  {name}: {info["version"]}')
    else:
        print('No runtime requirements to resolve')


if __name__ == '__main__':
    main()
