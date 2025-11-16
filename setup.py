#!/usr/bin/env python

from setuptools import find_packages, setup


def descriptions():
    with open('README.md') as fh:
        ret = fh.read()
        first = ret.split('\n', 1)[0].replace('#', '')
        return first, ret


def version():
    with open('proviso/__init__.py') as fh:
        for line in fh:
            if line.startswith('__version__'):
                return line.split("'")[1]
    return 'unknown'


description, long_description = descriptions()

tests_require = ('pytest', 'pytest-cov', 'pytest-network')

setup(
    author='Ross McFarland',
    author_email='rwmcfa1@gmail.com',
    description=description,
    entry_points={'console_scripts': ('proviso = proviso.main:main',)},
    extras_require={
        'dev': tests_require
        + (
            # we need to manually/explicitely bump major versions as they're
            # likely to result in formatting changes that should happen in their
            # own PR. This will basically happen yearly
            # https://black.readthedocs.io/en/stable/the_black_code_style/index.html#stability-policy
            'black>=25.0.0,<26.0.0',
            'changelet',
            'isort>=5.11.5',
            'pyflakes>=2.2.0',
            'readme_renderer[md]>=26.0',
            'twine>=3.4.2',
        ),
        'test': tests_require,
    },
    install_requires=(
        'build>=0.7.0',
        'resolvelib>=1.0.0',
        'setuptools>=40.8.0',
        'unearth>=0.17.0',
    ),
    license='MIT',
    long_description=long_description,
    long_description_content_type='text/markdown',
    name='proviso',
    packages=find_packages(),
    python_requires='>=3.9',
    url='https://github.com/octodns/proviso',
    version=version(),
)
