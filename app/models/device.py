"""The device identity layer: one row per machine, and its record in every vendor.

`devices` is one machine as the engine knows it, whatever the vendors call
it: a canonical name, its NetBIOS key, the serials and MACs the vendors
reported, and a status — `active`, `rmm_stale` (NinjaOne has not heard from
it in the coverage threshold and no other vendor has either), `rmm_agent_down`
(NinjaOne has not heard from it but a vendor it is linked to still has: the
machine is alive and the RMM agent is down) or `retired` (nothing has seen it
in `DEVICE_RETIRE_DAYS`). Never deleted: a device that comes back is reactivated.

`device_vendor_links` ties one vendor's record — a NinjaOne device id, a
GravityZone endpoint id, a ConnectSecure asset id — to a device, with how the
pairing was made (`matched_by`: the join pass, `vendor_only` for a record
nothing paired, or `manual` with an audit row) and when the vendor last
returned the record. Several links per connector are allowed: a reinstalled
agent leaves the old row behind, and both are the machine's. A vendor record
belongs to exactly one device.

`device_unresolved` is the queue: a vendor record a pass could not pair
without guessing, with the candidate devices named, for a person to link.

`device_agreements` is the daily answer to "do the join and the links still
say the same thing": the board computed both ways after each resolution, the
per-site counts compared, and every difference kept. Read by the Devices page
for Inputiv staff; a difference is shown, never reconciled.

Scoped to a client (hard rule 1). Written only by `app/devices/resolve.py`
and the `scripts/devices.py` link and unlink actions; read by the coverage
board, the review's coverage findings and the device lookup tools when
`DEVICE_IDENTITY_ENABLED` is on.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import ConnectorName
from app.models.mixins import TimestampMixin
from app.models.types import connector_name_type

DEVICE_STATUSES = ("active", "rmm_stale", "rmm_agent_down", "retired")


class Device(Base, TimestampMixin):
    __tablename__ = "devices"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'rmm_stale', 'rmm_agent_down', 'retired')",
            name="ck_devices_status",
        ),
        Index("ix_devices_client_site", "client_id", "site_id"),
        Index("ix_devices_client_status", "client_id", "status"),
        Index("ix_devices_client_netbios", "client_id", "netbios_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    site_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False
    )
    canonical_name: Mapped[str] = mapped_column(Text, nullable=False)
    """The name the engine shows: NinjaOne's when it has one, else the vendor's."""
    netbios_key: Mapped[str] = mapped_column(Text, nullable=False)
    """`normalise_hostname(canonical_name)[:15]`: the second pass's key."""
    serials: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    macs: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    node_class: Mapped[str | None] = mapped_column(Text, nullable=True)
    os: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    first_seen_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    last_seen_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    """The latest time any vendor returned one of its records."""

    def __repr__(self) -> str:
        return f"<Device id={self.id!r} site={self.site_id!r} status={self.status!r}>"


class DeviceVendorLink(Base):
    __tablename__ = "device_vendor_links"
    __table_args__ = (
        # A vendor record is one device's, within a client.
        UniqueConstraint(
            "client_id", "connector_name", "external_id", name="uq_device_vendor_links_record"
        ),
        Index("ix_device_vendor_links_device", "device_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    connector_name: Mapped[ConnectorName] = mapped_column(connector_name_type, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    """The vendor's id for the record: NinjaOne device id, endpoint id, asset id."""
    vendor_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The hostname as that vendor spells it, for the show command and the audit row."""
    matched_by: Mapped[str] = mapped_column(Text, nullable=False)
    """`ninjaone` (the population's own record), `exact`, `netbios15`, `mac`,
    `vendor_only` (a record nothing paired, given a device of its own) or `manual`."""
    first_seen_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    last_seen_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    """False once the vendor stops returning the record, or a person unlinks it."""
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The reason a person gave for a manual link or an unlink."""
    vendor_last_seen_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    """When the vendor last heard from the record, where the vendor says so only
    on request (GravityZone `lastSeen`, one detail call per row). Read for the
    rows paired with devices NinjaOne has lost, and kept here so the next reader
    asks again only when `vendor_checked_at` is older than
    `BITDEFENDER_LAST_SEEN_REFRESH_HOURS`."""
    vendor_checked_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)

    def __repr__(self) -> str:
        return (
            f"<DeviceVendorLink device={self.device_id!r} {self.connector_name!r} "
            f"{self.external_id!r} by={self.matched_by!r} active={self.active!r}>"
        )


class DeviceUnresolved(Base):
    __tablename__ = "device_unresolved"
    __table_args__ = (
        UniqueConstraint(
            "client_id", "connector_name", "external_id", name="uq_device_unresolved_record"
        ),
        Index("ix_device_unresolved_client_open", "client_id", "resolved_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    site_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False
    )
    connector_name: Mapped[ConnectorName] = mapped_column(connector_name_type, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    vendor_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    found_by: Mapped[str] = mapped_column(Text, nullable=False)
    """The pass that found the candidates: exact, netbios15 or mac."""
    candidates: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    """`{"device_id", "name"}` per candidate device."""
    first_seen_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    last_seen_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    """Set when a person linked the record, or a later run paired it on its own."""


class DeviceAgreement(Base):
    __tablename__ = "device_agreements"
    __table_args__ = (Index("ix_device_agreements_client_at", "client_id", "computed_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    computed_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    agree: Mapped[bool] = mapped_column(Boolean, nullable=False)
    sites_compared: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    """Fleet totals both ways: managed, unmanaged, discovered, covered share per tool."""
    differences: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    """`{"site_slug", "site", "field", "off", "on"}` per differing count. Empty when they agree."""
    actor: Mapped[str] = mapped_column(Text, nullable=False)

    def __repr__(self) -> str:
        return f"<DeviceAgreement client={self.client_id!r} agree={self.agree!r}>"
