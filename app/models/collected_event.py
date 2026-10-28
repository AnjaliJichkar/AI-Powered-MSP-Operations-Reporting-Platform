"""Events collected daily from the vendors, and the record of which windows were collected.

`collected_events` is one row per vendor event we have seen (a Bitdefender
detection, later a Cove backup job or a Blackpoint alert), keyed by the
vendor's own id so a collection run can be repeated without duplicating
anything: a second sight of the same event updates the row.

`collection_coverage` is one row per (site, connector, kind, run): the window
the run asked the vendor for and how it ended. It is what says "August is
covered for this site" or "the 14th failed", so a report or an engineer can
tell a quiet day from a day nobody collected.

Both are scoped to a client (hard rule 1) and never deleted by the collector.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Identity, Index, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import ConnectorName
from app.models.types import connector_name_type


class CollectedEvent(Base):
    __tablename__ = "collected_events"
    __table_args__ = (
        UniqueConstraint(
            "connector_name", "kind", "external_id", name="uq_collected_events_vendor_event"
        ),
        Index("ix_collected_events_site_kind_time", "site_id", "kind", text("occurred_at DESC")),
        Index("ix_collected_events_client_time", "client_id", text("occurred_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    site_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False
    )
    connector_name: Mapped[ConnectorName] = mapped_column(connector_name_type, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    """`detection` for a Bitdefender incident; later kinds name their own vendor object."""
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    """The vendor's id for the event. With `connector_name` and `kind`, the idempotency key."""
    occurred_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    """When the vendor says it happened (an incident's `created`)."""
    vendor_updated_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    """The vendor's last-updated time, so a re-collection can tell a changed event."""
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    """The compact record (`app/events/collector.py`), never the raw vendor row."""
    first_collected_at: Mapped[dt.datetime] = mapped_column(
        nullable=False, server_default=text("now()")
    )
    last_collected_at: Mapped[dt.datetime] = mapped_column(
        nullable=False, server_default=text("now()")
    )


class CollectionCoverage(Base):
    __tablename__ = "collection_coverage"
    __table_args__ = (
        Index(
            "ix_collection_coverage_site_kind",
            "site_id",
            "connector_name",
            "kind",
            text("window_end DESC"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    site_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False
    )
    connector_name: Mapped[ConnectorName] = mapped_column(connector_name_type, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    window_start: Mapped[dt.datetime] = mapped_column(nullable=False)
    window_end: Mapped[dt.datetime] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    """`collected`; `failed` with `error` saying why; or `not_applicable` when the site's
    licence cannot produce this kind of event (`error` says which), so the window is
    neither covered nor quiet."""
    events_seen: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    events_new: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    collected_at: Mapped[dt.datetime] = mapped_column(nullable=False, server_default=text("now()"))
    collected_by: Mapped[str] = mapped_column(Text, nullable=False)
