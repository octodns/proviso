## Proviso requrements.txt management

A tool for managing requirements.txt version pinning and updating.

### Installation

#### Command line

```
pip install proviso
```

#### requirements.txt/setup.py

Pinning specific versions or SHAs is recommended to avoid unplanned upgrades.

##### Versions

```
# Start with the latest versions and don't just copy what's here
proviso==0.0.1
```

##### SHAs

```
# Start with the latest/specific versions and don't just copy what's here
-e git+https://git@github.com/octodns/proviso.git@ec9661f8b335241ae4746eea467a8509205e6a30#egg=proviso
```

### Usage

```console
usage: proviso [-h] [--directory DIRECTORY] [--extras EXTRAS] [--python-versions PYTHON_VERSIONS] [--filename FILENAME]
               [--level {DEBUG,INFO,WARNING,ERROR}]

Extract project requirements, resolve dependencies, and manage requirements.txt

options:
  -h, --help            show this help message and exit
  --directory DIRECTORY
                        Project root directory (default: current directory)
  --extras EXTRAS       Comma-separated list of extras to include (e.g., "dev,test"). Defaults to all defined extras.
  --python-versions PYTHON_VERSIONS
                        Comma-separated list of Python versions (e.g., "3.9,3.10,3.11"). Defaults to currently active versions
                        per endoflife.date.
  --filename FILENAME   Output filename or path for requirements (default: requirements.txt). If just a filename, will be placed
                        in --directory; if a path, will be used as-is.
  --level {DEBUG,INFO,WARNING,ERROR}
                        Logging level (default: INFO)
```

### Development

See the [/script/](/script/) directory for some tools to help with the development process. They generally follow the [Script to rule them all](https://github.com/github/scripts-to-rule-them-all) pattern. Most useful is `./script/bootstrap` which will create a venv and install both the runtime and development related requirements. It will also hook up a pre-commit hook that covers most of what's run by CI.
