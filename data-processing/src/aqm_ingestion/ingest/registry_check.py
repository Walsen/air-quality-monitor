"""Checking a reading's site against the registry (Requirement 6.7).

An unknown ``SiteCode`` is deliberately NOT a validation failure. A registry refresh
can lag behind a newly deployed sensor, and dropping that sensor's readings until the
refresh lands would lose data permanently — whereas keeping a reading whose
coordinates are not yet known costs only its absence from geographic queries, which
resolves itself the moment the metadata arrives.

So this is a separate check from :mod:`aqm_ingestion.ingest.quarantine`, not a
quarantine reason. Keeping them apart is the point: a reading with an unknown site is
STORED, and a test asserts no quarantine reason exists that could express otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from aqm_ingestion.observability.logging import get_logger
from aqm_ingestion.ports.protocols import RegistryEntry

_logger = get_logger("ingest.registry_check")


class _RegistryReader(Protocol):
    """The one registry method this check needs (§1 Interface Segregation)."""

    def get(self, site_code: str) -> RegistryEntry | None:
        """Return the registry entry for a site, or None if it is unknown."""
        ...


@dataclass(frozen=True, slots=True)
class SiteRegistration:
    """Whether a reading's site has metadata yet.

    Deliberately two fields. The geographic eligibility below is DERIVED rather than
    stored, so the two can never contradict each other — a site cannot be recorded
    as unknown and simultaneously eligible for a query that needs its coordinates.
    """

    site_code: str
    known: bool

    @property
    def eligible_for_geographic_results(self) -> bool:
        """Whether this site may appear in a geographic result (Requirement 6.7).

        An unknown site has no coordinates, so it cannot be ranked by distance. Said
        explicitly here rather than left for each caller to infer from ``known``,
        because the reason is about coordinates rather than about registration.
        """
        return self.known


def check_site_known(
    site_code: str, registry: _RegistryReader
) -> SiteRegistration:
    """Resolve a site against the registry, warning once when it is unknown.

    The reading is kept either way — this function reports, it does not reject
    (Requirement 6.7).

    Args:
        site_code: the reading's site.
        registry: the registry to look the site up in.

    Returns:
        The registration, including whether the site may appear in geographic
        results.
    """
    if registry.get(site_code) is not None:
        return SiteRegistration(site_code=site_code, known=True)

    # WARNING, not ERROR: the reading is still ingested, so this is a recoverable
    # condition that resolves itself when the registry refreshes (§6).
    _logger.warning(
        "unknown_site",
        SiteCode=site_code,
        reason="absent_from_registry",
        effect="ingested_but_excluded_from_geographic_results",
    )
    return SiteRegistration(site_code=site_code, known=False)
