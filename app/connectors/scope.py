"""What a connector is allowed to see, and what this call should cover.

A connector no longer holds one vendor id. It holds a `ClientScope`: every
organisation the franchisee owns (the allowlist) and the subset this particular
call addresses (the targets).

A `SiteRef` is one vendor organisation, not one site: a site with two
GravityZone companies is two refs with the same `site_id`. Every per-target
read fans out over the refs and every row is tagged with the ref's site, so
the two companies' endpoints arrive as one site's population and the guard
accepts either company id. "One site" therefore means one distinct `site_id`,
never one ref.

The two are separate on purpose.

- **targets** drives what we ask the vendor for. One site when the caller named
  one, every active site otherwise.
- **allowed** drives `_scoped()`, the guard that checks what came back. It is
  the client's *whole* allowlist rather than the targets, because a vendor
  returning a sibling site's record is a filter bug, while a vendor returning
  another franchisee's record is a containment breach. Checking against the
  full allowlist keeps those two distinguishable: the first is dropped and
  logged as a scoping warning, the second can never be silently merged in.

Nothing here reads the database. `app/connectors/factory.py` builds it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.connectors.errors import UpstreamError
from app.models.enums import ConnectorName


@dataclass(frozen=True, slots=True)
class VendorOrganisation:
    """One organisation as a vendor reports it, before it becomes a `Site`."""

    external_id: str
    name: str


@dataclass(frozen=True, slots=True)
class SiteRef:
    """One end customer, as a connector needs to know it."""

    site_id: uuid.UUID
    slug: str
    name: str
    external_id: str
    """The vendor's own organisation or company id, as a string."""

    def as_int(self, connector: str) -> int:
        """The vendor id as an integer, for the vendors that use numbers."""
        try:
            return int(self.external_id)
        except (TypeError, ValueError):
            raise UpstreamError(
                f"the {connector} id mapped for site {self.slug!r} is not a number: "
                f"{self.external_id!r}",
                connector=connector,
            ) from None


@dataclass(frozen=True, slots=True)
class ClientScope:
    """The franchisee, its sites, and the ones this call covers."""

    client_id: uuid.UUID
    client_slug: str
    connector: ConnectorName
    allowed: tuple[SiteRef, ...]
    """Every active site of this client for this connector. The guard list."""

    targets: tuple[SiteRef, ...]
    """The sites this call reads. A single site when one was named."""

    excluded: tuple[SiteRef, ...] = ()
    """This client's *inactive* sites: known to be ours, never read.

    A tenant-wide listing returns their records too. The guard drops them
    quietly, because a deactivated customer or an internal organisation is not
    a containment breach — unlike an organisation we know nothing about.
    """

    @property
    def site_ids(self) -> list[uuid.UUID]:
        """The distinct sites this call reads, in target order."""
        return list(dict.fromkeys(site.site_id for site in self.targets))

    @property
    def target_slugs(self) -> list[str]:
        """The distinct sites this call reads, by slug, in target order: what a tool
        reports as `sites_covered`. A site with two vendor organisations is one site."""
        return list(dict.fromkeys(site.slug for site in self.targets))

    @property
    def is_single_site(self) -> bool:
        """One distinct site, however many vendor organisations it holds."""
        return len(self.site_ids) == 1

    def refs_for_site(self, site_id: uuid.UUID) -> tuple[SiteRef, ...]:
        """Every vendor organisation the allowlist holds for one site."""
        return tuple(site for site in self.allowed if site.site_id == site_id)

    @property
    def covers_every_site(self) -> bool:
        """True when no site was named: the call reads the whole allowlist."""
        return bool(self.allowed) and set(self.targets) == set(self.allowed)

    @property
    def target_external_ids(self) -> list[str]:
        return [site.external_id for site in self.targets]

    @property
    def allowed_external_ids(self) -> set[str]:
        return {site.external_id for site in self.allowed}

    def allowed_ints(self, connector: str) -> set[int]:
        """The allowlist as integers, for vendors whose ids are numeric."""
        return {site.as_int(connector) for site in self.allowed}

    def site_for_external_id(self, external_id: object) -> SiteRef | None:
        """Which of our sites a returned record belongs to, if any."""
        if external_id is None:
            return None
        wanted = str(external_id)
        for site in self.allowed:
            if site.external_id == wanted:
                return site
        return None

    def target_for_external_id(self, external_id: object) -> SiteRef | None:
        wanted = str(external_id)
        for site in self.targets:
            if site.external_id == wanted:
                return site
        return None

    def describe_targets(self) -> str:
        """A short human description, for error and log messages."""
        if not self.targets:
            return "no sites"
        if self.is_single_site:
            return self.targets[0].slug
        return f"{len(self.site_ids)} sites"

    def narrowed_to(self, site: SiteRef) -> ClientScope:
        """The same scope narrowed to one site: every ref of that site. The allowlist
        is unchanged."""
        return ClientScope(
            client_id=self.client_id,
            client_slug=self.client_slug,
            connector=self.connector,
            allowed=self.allowed,
            targets=self.refs_for_site(site.site_id) or (site,),
            excluded=self.excluded,
        )
