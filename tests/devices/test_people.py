"""The people join: one tenant's accounts, their contact, their machines.

Pure, so this is rows in and ids out: no database, no vendor and nothing
stored. What is being pinned is that each join is by one thing — an address, a
login name — and that what nothing matches is counted rather than guessed at.
"""

from __future__ import annotations

import datetime as dt
import uuid

from app.connectors.microsoft365.models import GraphUser
from app.devices.people import BY_DISPLAY_NAME, BY_LOGIN, join_people
from app.models.device import Device

NOW = dt.datetime(2026, 10, 13, 9, tzinfo=dt.UTC)
CLIENT = uuid.uuid4()
SITE = uuid.uuid4()


def device(name: str) -> Device:
    return Device(
        id=uuid.uuid4(),
        client_id=CLIENT,
        site_id=SITE,
        canonical_name=name,
        netbios_key=name.lower()[:15],
        serials=[],
        macs=[],
        node_class="WINDOWS_WORKSTATION",
        status="active",
        first_seen_at=NOW,
        last_seen_at=NOW,
    )


def account(upn: str, name: str, *, enabled: bool = True, mail: str | None = None) -> GraphUser:
    # Graph's own ids are opaque GUIDs, and that is the point of printing them
    # rather than an address: the id says which account without saying who.
    return GraphUser.model_validate(
        {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, upn)),
            "displayName": name,
            "userPrincipalName": upn,
            "mail": mail,
            "accountEnabled": enabled,
        }
    )


def test_an_account_is_joined_to_a_contact_by_address_and_to_a_machine_by_login() -> None:
    ws = device("CON-LT-044")
    join = join_people(
        accounts=[account("sarah.chen@contoso.example", "Sarah Chen")],
        contacts=[(900, "Sarah.Chen@contoso.example"), (901, "nobody@contoso.example")],
        logged_on=[("CON-LT-044", "CONTOSO\\sarah.chen")],
        devices_by_name={"con-lt-044": ws},
        site_id=SITE,
        site_slug="contoso",
        tenant_id="t-1",
    )
    row = join.people[0]
    assert row.contact_id == 900  # matched whatever the case
    assert [(d.device_id, d.matched_by) for d in row.devices] == [(ws.id, BY_LOGIN)]
    assert (join.matched_contacts, join.matched_devices) == (1, 1)
    assert join.contacts_unmatched == 1  # the address nobody in the tenant has
    assert join.logged_on_unmatched == 0


def test_an_address_that_is_nobodys_is_counted_and_never_guessed() -> None:
    """A contact whose address is not in the directory and a machine whose last
    user is not either: two counts, no pairing. A near miss has to show as two
    unmatched things, never as a wrong pair."""
    join = join_people(
        accounts=[account("chris.allen@contoso.example", "Chris Allen")],
        contacts=[(900, "c.allen@contoso.example")],
        logged_on=[("CON-LT-044", "CONTOSO\\svc_backup")],
        devices_by_name={"con-lt-044": device("CON-LT-044")},
    )
    row = join.people[0]
    assert row.contact_id is None and row.devices == []
    assert (join.contacts_unmatched, join.logged_on_unmatched) == (1, 1)


def test_the_mail_address_counts_as_well_as_the_sign_in_name() -> None:
    """An account whose sign-in name is not the address people write to still
    joins: Graph carries both and so does the join."""
    join = join_people(
        accounts=[
            account("sc@contoso.onmicrosoft.example", "Sarah Chen", mail="sarah@contoso.example")
        ],
        contacts=[(900, "sarah@contoso.example")],
        logged_on=[],
        devices_by_name={},
    )
    assert join.people[0].contact_id == 900


def test_a_display_name_login_is_a_weaker_claim_and_says_so() -> None:
    """The machine's last user is `schen`, which is not the address's local
    part. The display name's usual shapes reach it, and the pairing is marked
    as having been made that way."""
    ws = device("CON-WS-012")
    join = join_people(
        accounts=[account("sarah.chen@contoso.example", "Sarah Chen")],
        contacts=[],
        logged_on=[("CON-WS-012", "CONTOSO\\schen")],
        devices_by_name={"con-ws-012": ws},
    )
    assert [(d.device_id, d.matched_by) for d in join.people[0].devices] == [
        (ws.id, BY_DISPLAY_NAME)
    ]


def test_a_person_on_several_machines_gets_all_of_them_once_each() -> None:
    desk, laptop = device("CON-WS-012"), device("CON-LT-044")
    join = join_people(
        accounts=[account("sarah.chen@contoso.example", "Sarah Chen")],
        contacts=[],
        logged_on=[
            ("CON-WS-012", "contoso\\sarah.chen"),
            ("CON-LT-044", "sarah.chen@contoso.example"),
            # The same machine twice is one machine.
            ("CON-LT-044", "CONTOSO\\sarah.chen"),
        ],
        devices_by_name={"con-ws-012": desk, "con-lt-044": laptop},
    )
    assert {d.device_id for d in join.people[0].devices} == {desk.id, laptop.id}


def test_a_machine_the_identity_layer_does_not_hold_is_not_a_pairing() -> None:
    """The join reaches the devices the ticket page reaches and no others: a
    machine NinjaOne reports that nothing has resolved yet is not ours to pair,
    and its login is not counted as unmatched either — somebody was found."""
    join = join_people(
        accounts=[account("sarah.chen@contoso.example", "Sarah Chen")],
        contacts=[],
        logged_on=[("CON-LT-999", "CONTOSO\\sarah.chen")],
        devices_by_name={},
    )
    assert join.people[0].devices == []
    assert join.logged_on_unmatched == 0


def test_nothing_is_joined_across_two_customers() -> None:
    """The caller passes one site's contacts and machines, and the join is of
    one tenant: an account here can only ever reach what was handed in. A
    second customer's identical name reaches nothing."""
    join = join_people(
        accounts=[account("sarah.chen@contoso.example", "Sarah Chen")],
        contacts=[(900, "sarah.chen@fabrikam.example")],
        logged_on=[],
        devices_by_name={},
        site_id=SITE,
    )
    assert join.people[0].contact_id is None and join.contacts_unmatched == 1


def test_the_stored_shape_is_counts_and_ids() -> None:
    """What a caller may keep and what the audit row carries: no address, no
    display name, no machine name."""
    join = join_people(
        accounts=[account("sarah.chen@contoso.example", "Sarah Chen")],
        contacts=[(900, "sarah.chen@contoso.example")],
        logged_on=[("CON-LT-044", "CONTOSO\\sarah.chen")],
        devices_by_name={"con-lt-044": device("CON-LT-044")},
        tenant_id="t-1",
    )
    text = repr(join.as_dict())
    for secret in ("sarah", "Chen", "CON-LT-044", "contoso.example"):
        assert secret.lower() not in text.lower(), secret
    assert join.as_dict()["accounts_with_a_contact"] == 1
