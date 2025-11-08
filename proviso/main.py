from argparse import ArgumentParser
from os import getcwd
from pprint import pprint

from proviso.builder import Builder
from proviso.resolver import Resolver


def main():
    parser = ArgumentParser(description='Extract project metadata and resolve dependencies')
    parser.add_argument(
        '--directory',
        default=getcwd(),
        help='Project root directory (default: current directory)',
    )
    
    args = parser.parse_args()

    # Build project metadata
    builder = Builder(args.directory)
    metadata = builder.metadata

    print(f'Project: {metadata.name} {metadata.version}')
    print()

    # Get runtime requirements (exclude extras)
    requirements = [
        req for req in (metadata.requires_dist or [])
        if req.marker is None or 'extra' not in str(req.marker)
    ]

    if requirements:
        print(f'Runtime requirements: {[str(r) for r in requirements]}')
        print()
        print('Resolving dependencies...')

        # Resolve dependencies
        resolver = Resolver()
        resolved = resolver.resolve(requirements)

        print()
        print('Resolved dependencies:')
        for name, info in sorted(resolved.items()):
            print(f'  {name}: {info["version"]}')
    else:
        print('No runtime requirements to resolve')


if __name__ == '__main__':
    main()
