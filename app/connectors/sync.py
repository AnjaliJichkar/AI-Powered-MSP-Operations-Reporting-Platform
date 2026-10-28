"""Discovering a client's sites from the vendor.

A franchisee's end customers are not typed in. `sync_sites` asks the vendor,
through the client's own credentials, which organisations those credentials can
see, and reconciles the answer into `Site` rows plus one site-level
`ToolMapping` each.

Four things make it more than an upsert:

- **Cross-connector matching.** Autotask and NinjaOne spell the same customer
  differently. Before creating a site, the normalised vendor name is looked up
  among the client's active sites; a match gets the new connector's mapping
  attached rather than a duplicate site.
- **Internal organisations.** A vendor tenant lists the franchisee's own
  bookkeeping alongside its customers. Those are recorded, so they can be seen
  and corrected, but created inactive so no question is ever scoped to them.
  The rules are per client (`app/connectors/naming.py`).
- **Adoption.** A tenant-level mapping that predates sites, or one an operator
  recorded by hand, carries the same vendor id a site now needs. It is moved
  onto the site rather than duplicated — and if the vendor id turns out to
  belong to another client, the sync aborts before writing anything.
- **Deactivation, never deletion.** `audit_log` and `tool_mappings` reference a
  site, and a customer that disappears for a day should not lose its history.
  A mapped organisation the vendor stops returning is reported as "mapping no
  longer found" and left in place for a person to remove; the site is
  deactivated only when every organisation it has, on any connector, is on
  this connector and none was returned.
- **Memory.** A merge leaves the kept site remembering the names it absorbed
  (`Site.absorbed_names`), so a later sync maps an organisation with one of
  those names onto the kept site rather than recreating the merged one.
- **Device overlap.** A new organisation whose device hostnames overlap an
  existing site's by at least `SYNC_OVERLAP_SHARE` is mapped onto that site
  ("linked by device overlap"), whatever its name; below the share, or with
  no hostnames to compare, an organisation whose name matches a site already
  mapped on this connector is created as its own site and listed under
  "possible duplicates" — a name alone never puts two organisations of one
  connector on one site.

`merge_sites` and `deactivate_site` are the operator's corrections for what the
matching gets wrong, driven by `scripts/link_sites.py`.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.errors import ConnectorNotConfigured, SyncConflict
from app.connectors.naming import SyncRules, normalise_name, rules_for, vendor_match_key
from app.connectors.scope import VendorOrganisation
from app.models.client import Client
from app.models.enums import ConnectorName
from app.models.site import Site
from app.models.tool_mapping import ToolMapping
from app.settings import get_settings

logger = logging.getLogger(__name__)

MAX_SLUG_LENGTH = 63


def slugify(name: str, *, fallback: str) -> str:
    """A slug an engineer can type and the model can read back.

    Must satisfy the `sites.slug` check constraint: lowercase alphanumerics and
    hyphens, starting with an alphanumeric, 2 to 63 characters.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)[:MAX_SLUG_LENGTH].rstrip("-")
    if len(slug) < 2 or not slug[0].isalnum():
        slug = f"site-{fallback}"[:MAX_SLUG_LENGTH]
    return slug


def _unique_slug(base: str, taken: set[str]) -> str:
    """Two customers can share a name; their slugs cannot."""
    if base not in taken:
        taken.add(base)
        return base
    for suffix in range(2, 1000):
        candidate = f"{base[: MAX_SLUG_LENGTH - len(str(suffix)) - 1]}-{suffix}"
        if candidate not in taken:
            taken.add(candidate)
            return candidate
    raise ConnectorNotConfigured(  # pragma: no cover - a thousand same-named sites
        f"cannot derive a unique slug from {base!r}", connector="sync"
    )


@dataclass
class SyncReport:
    """What one sync did, for the CLI and for the audit trail."""

    client_slug: str
    connector: ConnectorName
    seen: int = 0
    created: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    """Existing sites that gained this connector's mapping by name match."""
    adopted: list[str] = field(default_factory=list)
    """Sites whose mapping was a pre-existing tenant-level row, moved across."""
    internal: list[str] = field(default_factory=list)
    """Sites left inactive because the rules say they are the franchisee's own."""
    reactivated: list[str] = field(default_factory=list)
    renamed: list[str] = field(default_factory=list)
    deactivated: list[str] = field(default_factory=list)
    remembered: list[str] = field(default_factory=list)
    """Sites that took an organisation because a merge left them its name."""
    linked_by_overlap: list[str] = field(default_factory=list)
    """`site (share)`: sites that took an organisation because its devices overlap theirs."""
    possible_duplicates: list[str] = field(default_factory=list)
    """`new-site ~ existing-site`: created, but named like a site already on this connector."""
    missing: list[str] = field(default_factory=list)
    """`site: org <id>`: mapped organisations the vendor no longer returned. Left in place."""
    duplicated: list[str] = field(default_factory=list)
    """`site: n organisations`: sites holding more than one organisation on this connector."""
    unchanged: int = 0

    @property
    def changed(self) -> bool:
        return bool(
            self.created
            or self.matched
            or self.adopted
            or self.internal
            or self.reactivated
            or self.renamed
            or self.deactivated
            or self.remembered
            or self.linked_by_overlap
        )

    def summary(self) -> str:
        return (
            f"{self.seen} organisation(s) reported; "
            f"{len(self.created)} new, {len(self.matched)} matched to existing sites, "
            f"{len(self.remembered)} by remembered name, "
            f"{len(self.linked_by_overlap)} linked by device overlap, "
            f"{len(self.adopted)} adopted, {len(self.internal)} internal (inactive), "
            f"{len(self.reactivated)} reactivated, {len(self.renamed)} renamed, "
            f"{len(self.deactivated)} deactivated, {len(self.missing)} mapping(s) no longer "
            f"found, {len(self.possible_duplicates)} possible duplicate(s), {self.unchanged} "
            "unchanged"
        )


async def load_sync_rules(
    session: AsyncSession, client: Client, connector_name: ConnectorName
) -> SyncRules:
    """The client's rules for this connector: defaults plus its tenant-level extra."""
    tenant_row = (
        await session.execute(
            select(ToolMapping).where(
                ToolMapping.client_id == client.id,
                ToolMapping.connector_name == connector_name,
                ToolMapping.site_id.is_(None),
            )
        )
    ).scalar_one_or_none()
    return rules_for(connector_name, tenant_row.extra if tenant_row else None)


async def _holders_of(
    session: AsyncSession,
    client: Client,
    connector_name: ConnectorName,
    external_ids: set[str],
) -> dict[str, ToolMapping]:
    """Who already holds each of these vendor ids, keyed by vendor id.

    The `(connector_name, external_id)` pair is unique across every client, so
    each has at most one holder. A holder belonging to another client is a
    conflict the sync must not paper over: re-pointing the mapping would hand
    one franchisee's customer to another. It is raised here, before the sync
    writes anything at all.
    """
    if not external_ids:
        return {}
    rows = (
        (
            await session.execute(
                select(ToolMapping).where(
                    ToolMapping.connector_name == connector_name,
                    ToolMapping.external_id.in_(external_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    holders: dict[str, ToolMapping] = {}
    for holder in rows:
        if holder.external_id is None:  # pragma: no cover - excluded by the IN
            continue
        if holder.client_id != client.id:
            raise SyncConflict(
                f"{connector_name.value} organisation {holder.external_id!r} is already mapped "
                f"for a different client; refusing to claim it for {client.slug!r}. Resolve "
                "which franchisee owns it before syncing again.",
                connector=connector_name.value,
                details={"client_ref": client.slug, "external_id": holder.external_id},
            )
        holders[holder.external_id] = holder
    return holders


def _attach(
    holder: ToolMapping | None,
    client: Client,
    connector_name: ConnectorName,
    organisation: VendorOrganisation,
    site: Site,
    *,
    session: AsyncSession,
    report: SyncReport,
) -> ToolMapping:
    """Give a site its mapping: a new row, or the client's own tenant-level row adopted."""
    if holder is None:
        mapping = ToolMapping(
            client_id=client.id,
            site_id=site.id,
            connector_name=connector_name,
            external_id=organisation.external_id,
            external_name=organisation.name,
        )
        session.add(mapping)
        return mapping

    # `_holder_of` has ruled out another client's row, and this client's
    # site-level rows were handled by id before we got here, so what is left is
    # a tenant-level row from before sites existed. Move it onto the site.
    holder.site_id = site.id
    holder.external_name = organisation.name
    report.adopted.append(site.slug)
    return holder


def overlap_share(mine: set[str], theirs: set[str]) -> float:
    """The share of `mine` that is also in `theirs`. Zero when either is empty."""
    if not mine or not theirs:
        return 0.0
    return len(mine & theirs) / len(mine)


async def sync_sites(
    session: AsyncSession,
    client: Client,
    connector_name: ConnectorName,
    organisations: list[VendorOrganisation],
    *,
    rules: SyncRules | None = None,
    hostnames_by_organisation: Mapping[str, set[str]] | None = None,
    hostnames_by_site: Mapping[uuid.UUID, set[str]] | None = None,
    overlap_share_threshold: float | None = None,
) -> SyncReport:
    """Reconcile `Site` and `ToolMapping` rows against what the vendor reports.

    Pure database work: the caller does the vendor call, so this is testable
    without HTTP and reusable by every connector. Raises `SyncConflict` before
    writing anything if a vendor id already belongs to another client.

    `hostnames_by_organisation` (vendor id -> normalised hostnames) and
    `hostnames_by_site` (site id -> hostnames from the client's other tools)
    let a new organisation be linked to the site whose devices it holds; the
    script gathers them and the sync works without them.
    """
    rules = rules or await load_sync_rules(session, client, connector_name)
    threshold = (
        overlap_share_threshold
        if overlap_share_threshold is not None
        else get_settings().sync_overlap_share
    )
    org_hosts = hostnames_by_organisation or {}
    site_hosts = hostnames_by_site or {}
    report = SyncReport(client_slug=client.slug, connector=connector_name, seen=len(organisations))

    existing_sites = list(
        (await session.execute(select(Site).where(Site.client_id == client.id))).scalars().all()
    )
    existing_mappings = list(
        (
            await session.execute(
                select(ToolMapping).where(
                    ToolMapping.client_id == client.id,
                    ToolMapping.connector_name == connector_name,
                    ToolMapping.site_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )

    by_external_id = {
        mapping.external_id: mapping
        for mapping in existing_mappings
        if mapping.external_id is not None
    }
    sites_by_id = {site.id: site for site in existing_sites}
    taken_slugs = {site.slug for site in existing_sites}
    # A site this connector already maps cannot take a second organisation.
    mapped_site_ids = {mapping.site_id for mapping in existing_mappings}
    by_normalised: dict[str, list[Site]] = {}
    for site in existing_sites:
        by_normalised.setdefault(site.normalized_name, []).append(site)
    # What merges left behind: a normalised absorbed name -> the site that kept it,
    # for this connector or for any.
    by_absorbed: dict[str, Site] = {}
    for site in existing_sites:
        for entry in site.absorbed_names:
            connector = entry.get("connector")
            if connector in (None, connector_name.value) and entry.get("name"):
                by_absorbed.setdefault(normalise_name(str(entry["name"])), site)

    # Vendor ids new to this connector: refuse another client's before any write.
    holders = await _holders_of(
        session,
        client,
        connector_name,
        {org.external_id for org in organisations if org.external_id not in by_external_id},
    )

    seen_external_ids: set[str] = set()

    for organisation in organisations:
        seen_external_ids.add(organisation.external_id)
        internal = rules.is_internal(organisation.name, client_name=client.name)
        normalised = vendor_match_key(connector_name, organisation.name)

        # 1. Already mapped for this connector: update in place.
        mapping = by_external_id.get(organisation.external_id)
        if mapping is not None and mapping.site_id in sites_by_id:
            known = sites_by_id[mapping.site_id]
            touched = False
            if mapping.external_name != organisation.name:
                # The vendor renamed it. The site follows only if this connector
                # is where its name came from; otherwise two vendors spelling one
                # customer differently would rename it on alternate syncs.
                if mapping.external_name in (None, known.name):
                    known.name = organisation.name  # normalized_name follows via the validator
                    report.renamed.append(known.slug)
                    touched = True
                mapping.external_name = organisation.name
            if internal:
                if known.is_active:
                    known.is_active = False
                    report.internal.append(known.slug)
                    touched = True
            elif not known.is_active:
                known.is_active = True
                report.reactivated.append(known.slug)
                touched = True
            if not touched:
                report.unchanged += 1
            continue

        holder = holders.get(organisation.external_id)

        # 2. A merge left a site remembering this name: it goes there, whether or
        # not the site already holds an organisation on this connector.
        remembered = by_absorbed.get(normalised)
        if remembered is not None and remembered.is_active:
            _attach(
                holder,
                client,
                connector_name,
                organisation,
                remembered,
                session=session,
                report=report,
            )
            mapped_site_ids.add(remembered.id)
            report.remembered.append(remembered.slug)
            continue

        # 3. Another connector already created this customer: attach to it.
        same_name = [
            existing for existing in by_normalised.get(normalised, []) if existing.is_active
        ]
        candidates = [existing for existing in same_name if existing.id not in mapped_site_ids]
        if candidates:
            matched = candidates[0]
            _attach(
                holder,
                client,
                connector_name,
                organisation,
                matched,
                session=session,
                report=report,
            )
            mapped_site_ids.add(matched.id)
            report.matched.append(matched.slug)
            continue

        # 4. Its devices are an existing site's devices: link it there, whatever
        # it is called and whether or not that site is already on this connector.
        mine = org_hosts.get(organisation.external_id, set())
        best_site: Site | None = None
        best_share = 0.0
        if mine:
            for existing in existing_sites:
                if not existing.is_active:
                    continue
                share = overlap_share(mine, site_hosts.get(existing.id, set()))
                if share > best_share:
                    best_site, best_share = existing, share
        if best_site is not None and best_share >= threshold:
            _attach(
                holder,
                client,
                connector_name,
                organisation,
                best_site,
                session=session,
                report=report,
            )
            mapped_site_ids.add(best_site.id)
            report.linked_by_overlap.append(f"{best_site.slug} ({best_share:.0%})")
            continue

        # 5. New to us: create the site, inactive if the rules say it is internal.
        # From the normalised name, so "Acme Dental, LLC" is `acme-dental`, not
        # `acme-dental-llc`: the slug is what people type and the model reads.
        slug = _unique_slug(slugify(normalised, fallback=organisation.external_id), taken_slugs)
        created = Site(
            client_id=client.id,
            slug=slug,
            name=organisation.name,
            is_active=not internal,
        )
        session.add(created)
        await session.flush()
        _attach(
            holder, client, connector_name, organisation, created, session=session, report=report
        )
        sites_by_id[created.id] = created
        mapped_site_ids.add(created.id)
        by_normalised.setdefault(normalised, []).append(created)
        (report.internal if internal else report.created).append(slug)
        # Named like a site this connector already maps, or sharing some of its
        # devices: created, and flagged for a person to look at.
        lookalikes = [s for s in same_name if s.id != created.id]
        if best_site is not None and best_share > 0 and best_site not in lookalikes:
            lookalikes.append(best_site)
        for lookalike in lookalikes:
            report.possible_duplicates.append(f"{slug} ~ {lookalike.slug}")

    # A mapped organisation the vendor no longer returns: reported and left in
    # place for a person to remove. The site is deactivated only when every
    # organisation it has, on any connector, is on this connector and none of
    # them was returned.
    gone_by_site: dict[uuid.UUID, list[ToolMapping]] = {}
    for external_id, mapping in by_external_id.items():
        if external_id in seen_external_ids or mapping.site_id not in sites_by_id:
            continue
        site = sites_by_id[mapping.site_id]
        report.missing.append(f"{site.slug}: {connector_name.value} org {external_id}")
        gone_by_site.setdefault(site.id, []).append(mapping)
    if gone_by_site:
        every_mapping = (
            await session.execute(
                select(
                    ToolMapping.site_id, ToolMapping.connector_name, ToolMapping.external_id
                ).where(ToolMapping.site_id.in_(list(gone_by_site)))
            )
        ).all()
        for site_id in gone_by_site:
            # What still stands for this site: any other connector's mapping, or
            # one on this connector the vendor did return.
            still = [
                row
                for row in every_mapping
                if row.site_id == site_id
                and (row.connector_name != connector_name or row.external_id in seen_external_ids)
            ]
            site = sites_by_id[site_id]
            if not still and site.is_active:
                site.is_active = False
                report.deactivated.append(site.slug)

    # Sites holding more than one organisation on this connector, for the output.
    per_site: dict[uuid.UUID, int] = {}
    for mapping in existing_mappings:
        if mapping.site_id is not None:
            per_site[mapping.site_id] = per_site.get(mapping.site_id, 0) + 1
    for new_mapping in session.new:
        if isinstance(new_mapping, ToolMapping) and new_mapping.site_id is not None:
            per_site[new_mapping.site_id] = per_site.get(new_mapping.site_id, 0) + 1
    for site_id, count in per_site.items():
        if count > 1 and site_id in sites_by_id:
            report.duplicated.append(f"{sites_by_id[site_id].slug}: {count} organisations")

    await session.flush()
    if report.changed:
        logger.info(
            "sync_sites client=%s connector=%s: %s",
            client.slug,
            connector_name.value,
            report.summary(),
        )
    return report


async def site_id_for_slug(
    session: AsyncSession, client_id: uuid.UUID, slug: str
) -> uuid.UUID | None:
    """Resolve a slug within one client. None when it is not that client's."""
    return (
        await session.execute(
            select(Site.id).where(
                Site.client_id == client_id,
                Site.slug == slug,
                Site.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()


# --- operator corrections: scripts/link_sites.py ----------------------------


@dataclass
class MergeReport:
    kept: str
    merged: str
    moved: list[str] = field(default_factory=list)
    """Connector names whose mapping moved from the merged site to the kept one."""
    combined: list[tuple[str, str]] = field(default_factory=list)
    """`(connector, external_id)` of mappings that now sit beside one the kept site
    already had on the same connector: the site holds both organisations."""
    absorbed: list[str] = field(default_factory=list)
    """Names the kept site now remembers, so a later sync maps them here."""


#: Keys a mapping's `extra` may carry to say which vendor tenant it lives in. Two
#: organisations on one connector may share a site only within one tenant.
TENANT_KEYS = ("tenant_id", "tenant", "host", "pod")


def _tenant_of(mapping: ToolMapping) -> str | None:
    for key in TENANT_KEYS:
        value = mapping.extra.get(key) if mapping.extra else None
        if value:
            return str(value)
    return None


def remember_names(site: Site, entries: list[dict[str, str | None]]) -> list[str]:
    """Add absorbed names to a site, once each. Returns the names newly remembered."""
    known = {
        (e.get("connector"), normalise_name(str(e.get("name") or ""))) for e in site.absorbed_names
    }
    added: list[str] = []
    fresh = list(site.absorbed_names)
    for entry in entries:
        name = entry.get("name")
        if not name:
            continue
        key = (entry.get("connector"), normalise_name(name))
        if key in known:
            continue
        known.add(key)
        fresh.append({k: v for k, v in entry.items() if v is not None})
        added.append(name)
    site.absorbed_names = fresh  # a new list, so SQLAlchemy sees the change
    return added


async def merge_sites(
    session: AsyncSession, client: Client, *, keep: str, merge: str
) -> MergeReport:
    """Fold one site into another, moving its mappings and keeping the kept name.

    For when matching did not recognise two vendors' names as one customer, or
    one vendor's two organisations as one customer. The merged site is
    deactivated, not deleted; its vendor ids now resolve to the kept site, so
    the next sync sees every organisation mapped and leaves the result alone.

    Two mappings on one connector are combined onto the kept site: a customer
    with a hand-made GravityZone company and an integration-created one holds
    both, and a site-scoped read covers their union. The one refusal left is
    two organisations on one connector that live in different vendor tenants
    (`TENANT_KEYS` in `extra`): one site cannot be read with two credentials.

    The kept site remembers the merged site's name and the vendor names of
    the mappings it brought (`absorbed_names`), so a later sync maps a company
    with one of those names here rather than recreating the merged site.
    """
    if keep == merge:
        raise ConnectorNotConfigured("--keep and --merge name the same site", connector="sync")
    kept = await _site_of(session, client, keep)
    merged = await _site_of(session, client, merge)

    kept_mappings = list(
        (await session.execute(select(ToolMapping).where(ToolMapping.site_id == kept.id)))
        .scalars()
        .all()
    )
    moving = list(
        (await session.execute(select(ToolMapping).where(ToolMapping.site_id == merged.id)))
        .scalars()
        .all()
    )
    kept_by_connector: dict[ConnectorName, list[ToolMapping]] = {}
    for mapping in kept_mappings:
        kept_by_connector.setdefault(mapping.connector_name, []).append(mapping)
    for mapping in moving:
        for existing in kept_by_connector.get(mapping.connector_name, []):
            mine, theirs = _tenant_of(mapping), _tenant_of(existing)
            if mine and theirs and mine != theirs:
                raise ConnectorNotConfigured(
                    f"cannot merge {merge!r} into {keep!r}: their {mapping.connector_name.value} "
                    f"organisations live in different vendor tenants ({theirs!r} and {mine!r}), "
                    "and one site cannot be read with two credentials.",
                    connector="sync",
                )

    report = MergeReport(kept=kept.slug, merged=merged.slug)
    absorbed: list[dict[str, str | None]] = [{"name": merged.name, "connector": None}]
    for mapping in moving:
        mapping.site_id = kept.id
        report.moved.append(mapping.connector_name.value)
        if kept_by_connector.get(mapping.connector_name):
            report.combined.append((mapping.connector_name.value, mapping.external_id or ""))
        absorbed.append(
            {
                "name": mapping.external_name,
                "connector": mapping.connector_name.value,
                "external_id": mapping.external_id,
            }
        )
    for entry in merged.absorbed_names:
        absorbed.append({str(k): (str(v) if v is not None else None) for k, v in entry.items()})
    report.absorbed = remember_names(kept, absorbed)
    merged.is_active = False
    await session.flush()
    return report


async def deactivate_site(session: AsyncSession, client: Client, slug: str) -> Site:
    """Take a site out of every unscoped read. Its mappings and history remain.

    The next sync will not reactivate it unless the vendor still lists it and
    the rules do not call it internal — so for a vendor-side organisation that
    is really the franchisee's own, set a rule rather than deactivating by hand.
    """
    site = await _site_of(session, client, slug)
    site.is_active = False
    await session.flush()
    return site


async def _site_of(session: AsyncSession, client: Client, slug: str) -> Site:
    """One of this client's sites by slug. Another client's is refused as unknown."""
    site = (
        await session.execute(select(Site).where(Site.client_id == client.id, Site.slug == slug))
    ).scalar_one_or_none()
    if site is None:
        raise ConnectorNotConfigured(f"{slug!r} is not a site of {client.slug!r}", connector="sync")
    return site
