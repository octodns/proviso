# Developer Agent Guide for proviso

This repository contains the `proviso` utility, a requirements.txt dependency resolution and pinning tool. It extracts dependencies from a project's `setup.py` or `pyproject.toml`, recursively resolves their transitive dependencies using PyPI, and compiles a single, version-pinned `requirements.txt` file containing environment markers that works across multiple Python versions.

> [!IMPORTANT]
> **Core Workflow and Guidelines**
>
> All agents working on this repository must read and follow the general instructions and workflow guidelines defined in the core octoDNS `AGENTS.md` file.
> - **Local check**: Look for the file at `../octodns/AGENTS.md`.
> - **Remote check**: If the local file is not available, fetch it from GitHub: [octoDNS Core AGENTS.md](https://github.com/octodns/octodns/raw/refs/heads/main/AGENTS.md).
>
> You must align your code structure, style, pull request guidelines, and overall development workflows with the instructions specified there.

## Repository & Module Information

### Key Components

- **CLI Entry Point**: [main.py](file:///home/ross/octodns/proviso/proviso/main.py) (function [main](file:///home/ross/octodns/proviso/proviso/main.py#L208-L326)) handles argument parsing, configures logging levels, instantiates the HTTP caching client, fetches Python lifecycle information, calls the resolver, and writes the output requirements file.
- **Metadata Builder**: [Builder](file:///home/ross/octodns/proviso/proviso/builder.py#L10-L33) (defined in [builder.py](file:///home/ross/octodns/proviso/proviso/builder.py)) invokes standard build frontends/backends to compile the project metadata and extract raw dependency definitions.
- **Python Version Manager**: [Python](file:///home/ross/octodns/proviso/proviso/python.py#L13-L52) (defined in [python.py](file:///home/ross/octodns/proviso/proviso/python.py)) queries the `endoflife.date` API to dynamically retrieve currently active Python version cycles.
- **PyPI Dependency Resolver**: [Resolver](file:///home/ross/octodns/proviso/proviso/resolver.py#L25-L232) (defined in [resolver.py](file:///home/ross/octodns/proviso/proviso/resolver.py)) manages PyPI package lookups. It performs recursive dependency resolution, filters packages by release date (implementing dependency cooldown logic), and checks versions using `resolvelib`.
- **Caching HTTP Client**: [CachingClient](file:///home/ross/octodns/proviso/proviso/utils.py#L52-L68) (defined in [utils.py](file:///home/ross/octodns/proviso/proviso/utils.py)) is a persistent HTTP client wrapping `httpx` and `hishel` to cache PyPI API queries using SQLite.

### Key Workflows & Features

1. **Extraction**: `proviso` extracts direct package dependencies and optional extras from `setup.py` or `pyproject.toml` using build APIs.
2. **Dynamic Active Python Lifecycle Resolution**: If no Python versions are specified, `proviso` contacts `https://endoflife.date/api/python.json` to look up currently supported Python cycles.
3. **Multi-Version Resolution**: For each active Python version, the dependencies are resolved recursively against the PyPI JSON API.
4. **Environment Markers Generation**: Pinned package versions are combined and written with appropriate PEP 508 environment markers (e.g. `package==1.0.0; python_version=='3.10'`) when different versions are resolved across Python versions.
5. **Dependency Cooldown**: Supports a `--cooldown-days` constraint that excludes packages published newer than the cooldown window.

## Development & Testing

- **Setup Script**: Run `./script/bootstrap` to create a virtual environment, install runtime and development dependencies (including `black`, `isort`, `pyflakes`, and `pytest`), and configure git pre-commit hooks.
- **Test Suite**: Run unit tests using `pytest` via `./script/test` (or `pytest tests/`). Test files are located in [tests/](file:///home/ross/octodns/proviso/tests).
- **Code Coverage**: Verify code coverage using `./script/coverage`.

## Key Constraints & Behaviors

- **Python Version**: Targets Python `>=3.9`.
- **Formatting**: Code formatting is enforced via `black` (version `>=26.0.0,<27.0.0`) and `isort`.
- **Non-Provider nature**: `proviso` is a package requirements resolution tool, not a DNS provider. It does not implement DNS synchronization or DNS record classes.
