TODO: Review this README and add or modify as necessary.

## Proviso provider for octoDNS

An [octoDNS](https://github.com/octodns/octodns/) provider that targets [Proviso](https://github.com/octodns/proviso).

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
octodns==0.9.14
proviso==0.0.1
```

##### SHAs

```
# Start with the latest/specific versions and don't just copy what's here
-e git+https://git@github.com/octodns/octodns.git@9da19749e28f68407a1c246dfdf65663cdc1c422#egg=octodns
-e git+https://git@github.com/octodns/proviso.git@ec9661f8b335241ae4746eea467a8509205e6a30#egg=proviso
```

### Configuration

```yaml
providers:
  proviso:
    class: proviso.Proviso
    # TODO
```

### Support Information

#### Records

TODO: All octoDNS record types are supported.

#### Dynamic

TODO: Proviso does not support dynamic records.

### Development

See the [/script/](/script/) directory for some tools to help with the development process. They generally follow the [Script to rule them all](https://github.com/github/scripts-to-rule-them-all) pattern. Most useful is `./script/bootstrap` which will create a venv and install both the runtime and development related requirements. It will also hook up a pre-commit hook that covers most of what's run by CI.

TODO: any provider specific setup, a docker compose to run things locally etc?
