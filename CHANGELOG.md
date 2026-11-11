## 0.4.0 - 2026-11-11

Minor:
* Support unearth 0.19, which switched from httpx to httpx2; proviso now uses httpx2 and hishel's httpx2 integration. - [#32](https://github.com/octodns/proviso/pull/32)
* Support hishel 2.x, which is now required. Existing hishel 1.x cache databases are cleared automatically, and caching without --cache-db is now in-memory as documented. - [#30](https://github.com/octodns/proviso/pull/30)
* Default --cooldown-days to 7 so dependency cooldown is on by default; pass 0 to disable - [#27](https://github.com/octodns/proviso/pull/27)
* Modernize packaging: move build metadata from setup.py into pyproject.toml, remove setup.py; require Python >=3.10 - [#24](https://github.com/octodns/proviso/pull/24)
* Mirror pip's resolution provider for performance and correctness - [#23](https://github.com/octodns/proviso/pull/23)
* Cache find_matches results per package to avoid combinatorial blowup when many packages share a common dependency - [#22](https://github.com/octodns/proviso/pull/22)

Patch:
* Resolve extras on dependency requirements, e.g. `hishel[httpx]` now pulls in the `httpx` extra's dependencies - [#31](https://github.com/octodns/proviso/pull/31)
* resolver: increase max_rounds to 1000 to support large package graphs - [#20](https://github.com/octodns/proviso/pull/20)

## 0.3.0 - 2026-04-03

Minor:
* Use CachingClient as a context manager to ensure proper resource cleanup. - [#13](https://github.com/octodns/proviso/pull/13)

Patch:
* Correctly resolve optional dependencies when extras are requested. - [#11](https://github.com/octodns/proviso/pull/11)
* Raise exception on HTTP errors when fetching package metadata. - [#12](https://github.com/octodns/proviso/pull/12)

## 0.2.0 - 2025-11-28

Minor:
* Minor bump to see if there's further pypi collisions

## 0.1.0 - 2025-11-26

Minor:
* Add --cooldown-days option for dependency cooldowns - [#7](https://github.com/octodns/proviso/pull/7)
* Add x-python-version-not-supported marker to requirements files - [#5](https://github.com/octodns/proviso/pull/5)
* Move to hishel http caching, more configurable - [#4](https://github.com/octodns/proviso/pull/4)

Patch:
* Support building packages with no extras - [#3](https://github.com/octodns/proviso/pull/3)

## 0.0.1 - 2025-11-16

- Initial release
