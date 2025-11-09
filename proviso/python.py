from datetime import date
from functools import cached_property

import httpx


class Python:
    """Fetches and filters active Python releases from endoflife.date."""

    API_URL = 'https://endoflife.date/api/python.json'

    @cached_property
    def releases(self):
        """Fetch all Python release data from endoflife.date API.

        Returns:
            List of dicts containing release information with fields:
            - cycle: version string (e.g., "3.12")
            - releaseDate: ISO date string
            - eol: end-of-life ISO date string
            - latest: latest patch version
            - and other metadata
        """
        response = httpx.get(self.API_URL)
        response.raise_for_status()
        return response.json()

    @property
    def active(self):
        """Return list of active Python releases.

        A release is active if: releaseDate <= today <= eol

        Returns:
            List of release dicts sorted by version (oldest to newest)
        """
        today = date.today()
        active_releases = []

        for release in self.releases:
            release_date = date.fromisoformat(release['releaseDate'])
            eol_date = date.fromisoformat(release['eol'])

            if release_date <= today <= eol_date:
                active_releases.append(release)

        # Sort by version number (oldest to newest)
        active_releases.sort(
            key=lambda r: tuple(map(int, r['cycle'].split('.')))
        )

        return active_releases
