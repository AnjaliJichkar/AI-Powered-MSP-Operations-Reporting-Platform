"""A person's corrections to the identity layer: link, unlink, and what to look at.

The daily resolution never guesses; what it could not place sits in the
unresolved queue for a person. `link` ties a vendor record to a device by
hand (`matched_by = manual`), which the job then leaves alone; `unlink`
withdraws a link with a reason, which is required and audited, and the
record is queued again so it is not silently lost. Nothing is deleted. Every
change is a `device_linked` audit row naming the device id, the vendor id,
the action and the reason; never a device name on the terminal.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.recorder import AuditContext, record_event
from app.models.device import Device, DeviceUnresolved, DeviceVendorLink
from app.models.enums import AuditEventType, AuditOutcome, ConnectorName, Interface


class DeviceAdminError(ValueError):
    """A request that cannot be recorded: no reason, an unknown device, a taken record."""


class DeviceNotFound(DeviceAdminError):
    """No such device or link for this client. Another client's is the same answer."""


class RecordTaken(DeviceAdminError):
    """The vendor record is actively linked to another device; unlink it there first."""


@dataclass(frozen=True, slots=True)
class Operator:
    actor_id: str
    client_id: uuid.UUID
    client_slug: str | None = None
    actor_user_id: uuid.UUID | None = None
    """The account, when the operator is a signed-in user rather than a CLI handle."""
    interface: Interface = Interface.SYSTEM

    def audit(self) -> AuditContext:
        return AuditContext.new(
            actor_id=self.actor_id,
            actor_user_id=self.actor_user_id,
            interface=self.interface,
            client_id=self.client_id,
            client_ref=self.client_slug,
        )


def _reason(reason: str) -> str:
    cleaned = " ".join(reason.split())
    if not cleaned:
        raise DeviceAdminError("a reason is required: say why this record is that device's")
    return cleaned


async def _device(session: AsyncSession, operator: Operator, device_id: uuid.UUID) -> Device:
    device = (
        await session.execute(
            select(Device).where(Device.id == device_id, Device.client_id == operator.client_id)
        )
    ).scalar_one_or_none()
    if device is None:
        raise DeviceNotFound("no such device for this client")
    return device


async def link(
    session: AsyncSession,
    operator: Operator,
    *,
    device_id: uuid.UUID,
    connector: ConnectorName,
    external_id: str,
    reason: str,
    vendor_name: str | None = None,
    now: dt.datetime | None = None,
) -> DeviceVendorLink:
    """Tie a vendor record to a device by hand. The record must not be another device's."""
    reason = _reason(reason)
    now = now or dt.datetime.now(dt.UTC)
    device = await _device(session, operator, device_id)
    existing = (
        await session.execute(
            select(DeviceVendorLink).where(
                DeviceVendorLink.client_id == operator.client_id,
                DeviceVendorLink.connector_name == connector,
                DeviceVendorLink.external_id == external_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None and existing.active and existing.device_id != device.id:
        raise RecordTaken(
            f"{connector.value} record {external_id} is linked to device {existing.device_id}; "
            "unlink it there first"
        )
    if existing is not None:
        existing.device_id = device.id
        existing.active = True
        existing.matched_by = "manual"
        existing.note = reason
        existing.last_seen_at = now
        row = existing
    else:
        row = DeviceVendorLink(
            client_id=operator.client_id,
            device_id=device.id,
            connector_name=connector,
            external_id=external_id,
            vendor_name=vendor_name,
            matched_by="manual",
            first_seen_at=now,
            last_seen_at=now,
            active=True,
            note=reason,
        )
        session.add(row)
    queued = (
        await session.execute(
            select(DeviceUnresolved).where(
                DeviceUnresolved.client_id == operator.client_id,
                DeviceUnresolved.connector_name == connector,
                DeviceUnresolved.external_id == external_id,
                DeviceUnresolved.resolved_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if queued is not None:
        queued.resolved_at = now
    device.last_seen_at = max(device.last_seen_at, now)
    await session.flush()
    await record_event(
        session,
        operator.audit(),
        event_type=AuditEventType.DEVICE_LINKED,
        outcome=AuditOutcome.SUCCESS,
        connector_name=connector,
        payload={
            "action": "linked",
            "device_id": str(device.id),
            "external_id": external_id,
            "reason": reason,
        },
    )
    return row


async def unlink(
    session: AsyncSession,
    operator: Operator,
    *,
    link_id: int,
    reason: str,
    now: dt.datetime | None = None,
) -> DeviceVendorLink:
    """Withdraw a link. The row stays inactive with the reason; the record is queued again."""
    reason = _reason(reason)
    now = now or dt.datetime.now(dt.UTC)
    row = (
        await session.execute(
            select(DeviceVendorLink).where(
                DeviceVendorLink.id == link_id, DeviceVendorLink.client_id == operator.client_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise DeviceNotFound("no such link for this client")
    if not row.active:
        raise DeviceAdminError("that link is already inactive")
    row.active = False
    row.note = reason
    device = await _device(session, operator, row.device_id)
    candidates = [{"device_id": str(device.id), "name": device.canonical_name}]
    # A record has one queue row, ever (the unique key): one that was queued
    # before and settled is reopened rather than doubled.
    queued = (
        await session.execute(
            select(DeviceUnresolved).where(
                DeviceUnresolved.client_id == operator.client_id,
                DeviceUnresolved.connector_name == row.connector_name,
                DeviceUnresolved.external_id == row.external_id,
            )
        )
    ).scalar_one_or_none()
    if queued is not None:
        queued.site_id = device.site_id
        queued.vendor_name = row.vendor_name or queued.vendor_name
        queued.found_by = "unlinked"
        queued.candidates = candidates
        queued.last_seen_at = now
        queued.resolved_at = None
    else:
        session.add(
            DeviceUnresolved(
                client_id=operator.client_id,
                site_id=device.site_id,
                connector_name=row.connector_name,
                external_id=row.external_id,
                vendor_name=row.vendor_name,
                found_by="unlinked",
                candidates=candidates,
                first_seen_at=now,
                last_seen_at=now,
            )
        )
    await session.flush()
    await record_event(
        session,
        operator.audit(),
        event_type=AuditEventType.DEVICE_LINKED,
        outcome=AuditOutcome.SUCCESS,
        connector_name=row.connector_name,
        payload={
            "action": "unlinked",
            "device_id": str(row.device_id),
            "link_id": row.id,
            "external_id": row.external_id,
            "reason": reason,
        },
    )
    return row
