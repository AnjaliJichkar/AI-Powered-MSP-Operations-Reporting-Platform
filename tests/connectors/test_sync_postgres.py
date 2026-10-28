"""sync_sites against a real database: matching, adoption, conflicts, merging.

No vendor is involved: `sync_sites` takes the organisations a connector would
have returned. Skipped unless `TEST_DATABASE_URL` is set.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.errors import ConnectorNotConfigured, SyncConflict
from app.connectors.factory import build_client_scope, list_client_sites
from app.connectors.scope import VendorOrganisation
from app.connectors.sync import deactivate_site, merge_sites, sync_sites
from app.models import Client, ConnectorName, Site, ToolMapping
from tests.connectors.conftest import LIVE_URL

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(LIVE_URL is None, reason="TEST_DATABASE_URL is not set"),
]

NINJAONE = ConnectorName.NINJAONE
AUTOTASK = ConnectorName.AUTOTASK
BITDEFENDER = ConnectorName.BITDEFENDER
CHECK = "ck_tool_mappings_tenant_level_has_no_external_id"


async def make_client(
    session: AsyncSession, slug: str = "teamlogic-austin", name: str = "TeamLogic IT of Austin"
) -> Client:
    client = Client(slug=slug, name=name)
    session.add(client)
    await session.flush()
    return client


def org(external_id: int | str, name: str) -> VendorOrganisation:
    return VendorOrganisation(external_id=str(external_id), name=name)


async def mappings_of(session: AsyncSession, site: Site) -> dict[str, str | None]:
    rows = (
        (await session.execute(select(ToolMapping).where(ToolMapping.site_id == site.id)))
        .scalars()
        .all()
    )
    return {row.connector_name.value: row.external_id for row in rows}


async def all_mappings_of(session: AsyncSession, site: Site) -> list[tuple[str, str | None]]:
    rows = (
        (await session.execute(select(ToolMapping).where(ToolMapping.site_id == site.id)))
        .scalars()
        .all()
    )
    return [(row.connector_name.value, row.external_id) for row in rows]


async def site_by_slug(session: AsyncSession, client: Client, slug: str) -> Site:
    return (
        await session.execute(select(Site).where(Site.client_id == client.id, Site.slug == slug))
    ).scalar_one()


async def legacy_tenant_row(
    session: AsyncSession, client: Client, connector: ConnectorName, external_id: str
) -> ToolMapping:
    """A mapping from before sites existed: tenant-level, still carrying a vendor id.

    The check constraint forbids creating one now, which is the point; it was
    added NOT VALID so exactly these rows survive migration 0004. Dropping and
    re-adding it inside the test transaction reproduces that state.
    """
    await session.execute(text(f"ALTER TABLE tool_mappings DROP CONSTRAINT {CHECK}"))
    row = ToolMapping(
        client_id=client.id, site_id=None, connector_name=connector, external_id=external_id
    )
    session.add(row)
    await session.flush()
    await session.execute(
        text(
            f"ALTER TABLE tool_mappings ADD CONSTRAINT {CHECK} "
            "CHECK (site_id IS NOT NULL OR external_id IS NULL) NOT VALID"
        )
    )
    return row


# --- matching across connectors ----------------------------------------------


async def test_a_second_connector_matches_the_site_by_normalised_name(
    session: AsyncSession,
) -> None:
    client = await make_client(session)

    first = await sync_sites(session, client, NINJAONE, [org(42, "Acme Dental (Main St)")])
    assert first.created == ["acme-dental"]

    second = await sync_sites(session, client, AUTOTASK, [org(7, "Acme Dental, LLC")])
    assert second.matched == ["acme-dental"]
    assert second.created == []

    sites = await list_client_sites(session, client.id)
    assert [site.slug for site in sites] == ["acme-dental"]
    # The first connector's spelling stays; the second only adds its mapping.
    assert sites[0].name == "Acme Dental (Main St)"
    assert await mappings_of(session, sites[0]) == {"ninjaone": "42", "autotask": "7"}

    # And both vendors now scope to the same site.
    for connector in (NINJAONE, AUTOTASK):
        scope = await build_client_scope(session, client, connector)
        assert [ref.slug for ref in scope.allowed] == ["acme-dental"]


async def test_matching_never_crosses_clients(session: AsyncSession) -> None:
    """Two franchisees can each have an "Acme Dental"; neither sees the other's."""
    austin = await make_client(session, "teamlogic-austin", "TeamLogic IT of Austin")
    dallas = await make_client(session, "teamlogic-dallas", "TeamLogic IT of Dallas")

    await sync_sites(session, austin, NINJAONE, [org(42, "Acme Dental")])
    report = await sync_sites(session, dallas, AUTOTASK, [org(7, "Acme Dental")])

    assert report.created == ["acme-dental"]
    assert report.matched == []
    assert await mappings_of(session, await site_by_slug(session, austin, "acme-dental")) == {
        "ninjaone": "42"
    }
    assert await mappings_of(session, await site_by_slug(session, dallas, "acme-dental")) == {
        "autotask": "7"
    }


async def test_a_site_already_mapped_for_a_connector_does_not_absorb_a_second_org(
    session: AsyncSession,
) -> None:
    """Two NinjaOne organisations are two sites, however alike their names."""
    client = await make_client(session)
    report = await sync_sites(
        session, client, NINJAONE, [org(42, "Acme Dental"), org(43, "Acme Dental LLC")]
    )
    assert report.created == ["acme-dental", "acme-dental-2"]
    assert report.matched == []


async def test_an_inactive_site_is_not_a_match_target(session: AsyncSession) -> None:
    """A deactivated site stays out of the way; the new vendor gets a fresh one."""
    client = await make_client(session)
    await sync_sites(session, client, NINJAONE, [org(42, "Acme Dental")])
    await deactivate_site(session, client, "acme-dental")

    report = await sync_sites(session, client, AUTOTASK, [org(7, "Acme Dental")])
    assert report.created == ["acme-dental-2"]
    assert report.matched == []


async def test_a_renamed_organisation_keeps_its_site_and_its_matching_key_follows(
    session: AsyncSession,
) -> None:
    client = await make_client(session)
    await sync_sites(session, client, NINJAONE, [org(42, "Acme Dental")])

    report = await sync_sites(session, client, NINJAONE, [org(42, "Acme Family Dental, LLC")])
    assert report.renamed == ["acme-dental"]

    site = await site_by_slug(session, client, "acme-dental")
    assert site.name == "Acme Family Dental, LLC"
    assert site.normalized_name == "acme family dental"


async def test_a_site_the_vendor_stops_listing_is_deactivated_then_reactivated(
    session: AsyncSession,
) -> None:
    client = await make_client(session)
    await sync_sites(session, client, NINJAONE, [org(42, "Acme Dental"), org(43, "Bright Legal")])

    gone = await sync_sites(session, client, NINJAONE, [org(42, "Acme Dental")])
    assert gone.deactivated == ["bright-legal"]
    assert [s.slug for s in await list_client_sites(session, client.id)] == ["acme-dental"]

    back = await sync_sites(
        session, client, NINJAONE, [org(42, "Acme Dental"), org(43, "Bright Legal")]
    )
    assert back.reactivated == ["bright-legal"]
    assert back.created == []


# --- internal organisations ---------------------------------------------------


async def test_the_franchisees_own_organisations_are_recorded_inactive(
    session: AsyncSession,
) -> None:
    client = await make_client(session, name="TeamLogic IT of Austin")
    report = await sync_sites(
        session,
        client,
        NINJAONE,
        [
            org(1, "TeamLogic IT of Austin"),
            org(2, "zz Old Templates"),
            org(3, "Acme Dental"),
        ],
    )

    assert report.created == ["acme-dental"]
    assert report.internal == ["teamlogic-it-of-austin", "zz-old-templates"]

    active = await list_client_sites(session, client.id)
    everything = await list_client_sites(session, client.id, active_only=False)
    assert [s.slug for s in active] == ["acme-dental"]
    assert len(everything) == 3
    # Recorded with their mapping, so an operator can see and correct them...
    lab = await site_by_slug(session, client, "zz-old-templates")
    assert await mappings_of(session, lab) == {"ninjaone": "2"}
    # ...but never in scope.
    scope = await build_client_scope(session, client, NINJAONE)
    assert scope.allowed_external_ids == {"3"}


async def test_autotask_teamlogic_locations_are_internal_by_default(
    session: AsyncSession,
) -> None:
    client = await make_client(session)
    report = await sync_sites(
        session, client, AUTOTASK, [org(5, "TeamLogic IT of Dallas"), org(6, "Bright Legal")]
    )
    assert report.internal == ["teamlogic-it-of-dallas"]
    assert report.created == ["bright-legal"]


async def test_the_rules_come_from_the_clients_tenant_level_extra(session: AsyncSession) -> None:
    client = await make_client(session, name="TeamLogic IT of Austin")
    session.add(
        ToolMapping(
            client_id=client.id,
            site_id=None,
            connector_name=NINJAONE,
            extra={"sync": {"internal_prefixes": [], "match_tenant_name": False}},
        )
    )
    await session.flush()

    report = await sync_sites(
        session, client, NINJAONE, [org(1, "TeamLogic IT of Austin"), org(2, "zz Old Templates")]
    )
    assert report.internal == []
    assert report.created == ["teamlogic-it-of-austin", "zz-old-templates"]

    # The override is per connector: Autotask keeps its own defaults.
    autotask = await sync_sites(session, client, AUTOTASK, [org(9, "TeamLogic IT of Dallas")])
    assert autotask.internal == ["teamlogic-it-of-dallas"]


async def test_an_organisation_that_becomes_internal_is_deactivated(
    session: AsyncSession,
) -> None:
    client = await make_client(session)
    await sync_sites(session, client, NINJAONE, [org(2, "Old Templates")])

    report = await sync_sites(session, client, NINJAONE, [org(2, "zz Old Templates")])
    assert report.renamed == ["old-templates"]
    assert report.internal == ["old-templates"]
    assert not (await site_by_slug(session, client, "old-templates")).is_active


# --- adoption and conflicts ---------------------------------------------------


async def test_a_tenant_level_mapping_with_the_vendor_id_is_adopted_onto_the_site(
    session: AsyncSession,
) -> None:
    client = await make_client(session)
    legacy = await legacy_tenant_row(session, client, NINJAONE, "42")

    report = await sync_sites(session, client, NINJAONE, [org(42, "Acme Dental")])

    assert report.created == ["acme-dental"]
    assert report.adopted == ["acme-dental"]
    site = await site_by_slug(session, client, "acme-dental")
    # The same row, now site-level: no duplicate was inserted.
    rows = (
        (
            await session.execute(
                select(ToolMapping).where(
                    ToolMapping.client_id == client.id, ToolMapping.connector_name == NINJAONE
                )
            )
        )
        .scalars()
        .all()
    )
    assert [row.id for row in rows] == [legacy.id]
    assert legacy.site_id == site.id
    assert legacy.external_name == "Acme Dental"


async def test_a_vendor_id_held_by_another_client_aborts_the_sync(session: AsyncSession) -> None:
    """Hard rule 1: re-pointing a mapping would move a customer between franchisees."""
    austin = await make_client(session, "teamlogic-austin")
    dallas = await make_client(session, "teamlogic-dallas", "TeamLogic IT of Dallas")
    await sync_sites(session, austin, NINJAONE, [org(42, "Acme Dental")])

    with pytest.raises(SyncConflict, match="already mapped for a different client"):
        await sync_sites(
            session, dallas, NINJAONE, [org(77, "Bright Legal"), org(42, "Acme Dental")]
        )

    # Nothing was written for Dallas — not even the site before the conflict.
    assert await list_client_sites(session, dallas.id, active_only=False) == []
    assert await mappings_of(session, await site_by_slug(session, austin, "acme-dental")) == {
        "ninjaone": "42"
    }


async def test_another_clients_legacy_tenant_row_is_a_conflict_too(session: AsyncSession) -> None:
    austin = await make_client(session, "teamlogic-austin")
    dallas = await make_client(session, "teamlogic-dallas", "TeamLogic IT of Dallas")
    await legacy_tenant_row(session, dallas, NINJAONE, "42")

    with pytest.raises(SyncConflict):
        await sync_sites(session, austin, NINJAONE, [org(42, "Acme Dental")])


async def test_a_new_tenant_level_row_cannot_carry_a_vendor_id(session: AsyncSession) -> None:
    """The check constraint: only legacy rows get to look like that, and only until adopted."""
    client = await make_client(session)
    session.add(
        ToolMapping(client_id=client.id, site_id=None, connector_name=NINJAONE, external_id="42")
    )
    with pytest.raises(IntegrityError, match=CHECK):
        await session.flush()
    await session.rollback()


# --- operator corrections -----------------------------------------------------


async def two_unmatched_sites(session: AsyncSession) -> Client:
    """What sync produces when the names are too different to match."""
    client = await make_client(session)
    await sync_sites(session, client, NINJAONE, [org(42, "Acme Dental")])
    await sync_sites(session, client, AUTOTASK, [org(7, "Acme Family Dentistry")])
    return client


async def test_merging_moves_the_mappings_and_keeps_the_kept_sites_name(
    session: AsyncSession,
) -> None:
    client = await two_unmatched_sites(session)

    report = await merge_sites(session, client, keep="acme-dental", merge="acme-family-dentistry")
    assert report.moved == ["autotask"]

    kept = await site_by_slug(session, client, "acme-dental")
    merged = await site_by_slug(session, client, "acme-family-dentistry")
    assert kept.name == "Acme Dental"
    assert kept.is_active
    assert await mappings_of(session, kept) == {"ninjaone": "42", "autotask": "7"}
    assert not merged.is_active
    assert await mappings_of(session, merged) == {}

    # The next sync sees every organisation mapped and changes nothing.
    again = await sync_sites(session, client, AUTOTASK, [org(7, "Acme Family Dentistry")])
    assert not again.changed
    assert again.unchanged == 1


async def test_merging_two_sites_on_one_connector_combines_the_mappings(
    session: AsyncSession,
) -> None:
    """A customer with two GravityZone companies: the kept site holds both, remembers
    the merged site's names, and the next sync maps the second company back here."""
    client = await make_client(session)
    await sync_sites(session, client, NINJAONE, [org(42, "Acme Dental")])
    await sync_sites(
        session, client, BITDEFENDER, [org("b1", "Acme Dental"), org("b2", "Acme Dental Inc")]
    )
    # Two Bitdefender companies became two sites: the second normalises to the
    # same name, but a name alone never puts two organisations of one connector
    # on one site, so it got the `-2` slug the pilot's duplicates carry.
    assert (await site_by_slug(session, client, "acme-dental-2")).is_active

    report = await merge_sites(session, client, keep="acme-dental", merge="acme-dental-2")
    assert report.combined == [("bitdefender", "b2")]
    assert "Acme Dental Inc" in report.absorbed

    kept = await site_by_slug(session, client, "acme-dental")
    rows = await all_mappings_of(session, kept)
    assert sorted(rows) == [("bitdefender", "b1"), ("bitdefender", "b2"), ("ninjaone", "42")]
    assert {e["name"] for e in kept.absorbed_names} == {"Acme Dental Inc"}
    assert not (await site_by_slug(session, client, "acme-dental-2")).is_active

    # The next sync sees both companies mapped and changes nothing.
    again = await sync_sites(
        session, client, BITDEFENDER, [org("b1", "Acme Dental"), org("b2", "Acme Dental Inc")]
    )
    assert not again.changed


async def test_merging_across_vendor_tenants_is_refused(session: AsyncSession) -> None:
    client = await make_client(session)
    await sync_sites(session, client, NINJAONE, [org(42, "Acme Dental"), org(43, "Bright Legal")])
    for slug, tenant in (("acme-dental", "pod-a"), ("bright-legal", "pod-b")):
        site = await site_by_slug(session, client, slug)
        mapping = (
            await session.execute(select(ToolMapping).where(ToolMapping.site_id == site.id))
        ).scalar_one()
        mapping.extra = {"tenant_id": tenant}
    await session.flush()

    with pytest.raises(ConnectorNotConfigured, match="different vendor tenants"):
        await merge_sites(session, client, keep="acme-dental", merge="bright-legal")


async def test_merging_across_clients_is_refused(session: AsyncSession) -> None:
    austin = await make_client(session, "teamlogic-austin")
    dallas = await make_client(session, "teamlogic-dallas", "TeamLogic IT of Dallas")
    await sync_sites(session, austin, NINJAONE, [org(42, "Acme Dental")])
    await sync_sites(session, dallas, NINJAONE, [org(77, "Bright Legal")])

    with pytest.raises(ConnectorNotConfigured, match="not a site of 'teamlogic-austin'"):
        await merge_sites(session, austin, keep="acme-dental", merge="bright-legal")


async def test_a_site_cannot_be_merged_into_itself(session: AsyncSession) -> None:
    client = await two_unmatched_sites(session)
    with pytest.raises(ConnectorNotConfigured, match="same site"):
        await merge_sites(session, client, keep="acme-dental", merge="acme-dental")


async def test_deactivating_a_site_removes_it_from_scope_but_keeps_its_mapping(
    session: AsyncSession,
) -> None:
    client = await two_unmatched_sites(session)

    site = await deactivate_site(session, client, "acme-family-dentistry")
    assert not site.is_active
    assert await mappings_of(session, site) == {"autotask": "7"}
    scope = await build_client_scope(session, client, AUTOTASK)
    assert scope.allowed == ()


# --- memory, overlap, and what the vendor stops returning --------------------------


async def test_a_second_organisation_of_one_connector_is_a_possible_duplicate_by_name_alone(
    session: AsyncSession,
) -> None:
    """The GravityZone integration suffix is stripped for matching, but a name alone
    never puts two organisations of one connector on one site."""
    client = await make_client(session)
    await sync_sites(session, client, NINJAONE, [org(42, "Insurance Council of Texas")])
    report = await sync_sites(
        session,
        client,
        BITDEFENDER,
        [org("b1", "Insurance Council of Texas"), org("b2", "Insurance Council of Texas (vnghix)")],
    )
    assert report.matched == ["insurance-council-of-texas"]  # the first joins the NinjaOne site
    assert report.created == ["insurance-council-of-texas-2"]
    assert report.possible_duplicates == [
        "insurance-council-of-texas-2 ~ insurance-council-of-texas"
    ]
    assert report.duplicated == []


async def test_a_merge_is_remembered_so_the_next_sync_does_not_recreate_the_site(
    session: AsyncSession,
) -> None:
    client = await make_client(session)
    await sync_sites(session, client, NINJAONE, [org(42, "Acme Dental")])
    await sync_sites(
        session, client, BITDEFENDER, [org("b1", "Acme Dental"), org("b2", "Acme Dental (k3j9xz)")]
    )
    await merge_sites(session, client, keep="acme-dental", merge="acme-dental-2")
    # A third company appears with the absorbed name: it goes onto the kept site.
    report = await sync_sites(
        session,
        client,
        BITDEFENDER,
        [
            org("b1", "Acme Dental"),
            org("b2", "Acme Dental (k3j9xz)"),
            org("b3", "Acme Dental (k3j9xz)"),
        ],
    )
    assert report.remembered == ["acme-dental"]
    assert report.created == []
    kept = await site_by_slug(session, client, "acme-dental")
    assert sorted(e for c, e in await all_mappings_of(session, kept) if c == "bitdefender") == [
        "b1",
        "b2",
        "b3",
    ]
    assert report.duplicated == ["acme-dental: 3 organisations"]


async def test_device_overlap_links_a_new_organisation_to_the_site_that_has_its_devices(
    session: AsyncSession,
) -> None:
    client = await make_client(session)
    await sync_sites(session, client, NINJAONE, [org(42, "Acme Dental"), org(43, "Bright Legal")])
    acme = await site_by_slug(session, client, "acme-dental")
    bright = await site_by_slug(session, client, "bright-legal")
    site_hosts = {acme.id: {"ws-01", "ws-02", "ws-03", "dc-01"}, bright.id: {"bl-01", "bl-02"}}

    report = await sync_sites(
        session,
        client,
        BITDEFENDER,
        [org("b1", "ACME-DENTAL-GZ"), org("b2", "Unrelated Co")],
        hostnames_by_organisation={"b1": {"ws-01", "ws-02", "ws-03"}, "b2": {"bl-01", "zz-99"}},
        hostnames_by_site=site_hosts,
        overlap_share_threshold=0.5,
    )
    # b1's devices are all Acme's: linked there, whatever it is called. b2 shares
    # half of its devices with Bright, exactly at the threshold: linked too.
    assert report.linked_by_overlap == ["acme-dental (100%)", "bright-legal (50%)"]
    assert report.created == []
    assert await mappings_of(session, acme) == {"ninjaone": "42", "bitdefender": "b1"}
    assert await mappings_of(session, bright) == {"ninjaone": "43", "bitdefender": "b2"}

    below = await sync_sites(
        session,
        client,
        BITDEFENDER,
        [org("b1", "ACME-DENTAL-GZ"), org("b2", "Unrelated Co"), org("b3", "Newcomer")],
        hostnames_by_organisation={"b3": {"ws-01", "n-01", "n-02", "n-03", "n-04"}},
        hostnames_by_site=site_hosts,
        overlap_share_threshold=0.5,
    )
    assert below.created == ["newcomer"]
    assert below.linked_by_overlap == []
    assert below.possible_duplicates == ["newcomer ~ acme-dental"]  # some overlap, not enough


async def test_a_mapping_the_vendor_stops_returning_is_reported_and_left_in_place(
    session: AsyncSession,
) -> None:
    client = await make_client(session)
    await sync_sites(session, client, NINJAONE, [org(42, "Acme Dental")])
    await sync_sites(session, client, BITDEFENDER, [org("b1", "Acme Dental")])

    # Bitdefender no longer lists the company. The mapping stays, the site stays
    # active (NinjaOne still has it), and the report says so.
    report = await sync_sites(session, client, BITDEFENDER, [])
    assert report.missing == ["acme-dental: bitdefender org b1"]
    assert report.deactivated == []
    acme = await site_by_slug(session, client, "acme-dental")
    assert acme.is_active
    assert await mappings_of(session, acme) == {"ninjaone": "42", "bitdefender": "b1"}

    # Only when nothing stands for the site at all is it deactivated.
    gone = await sync_sites(session, client, NINJAONE, [])
    assert gone.missing == ["acme-dental: ninjaone org 42"]
    assert gone.deactivated == []  # Bitdefender's mapping still stands, unreturned or not
