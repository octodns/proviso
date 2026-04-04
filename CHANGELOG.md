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
