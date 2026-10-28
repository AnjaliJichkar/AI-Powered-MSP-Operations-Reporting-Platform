"""What the coverage board remembers: exclusions, and one snapshot a day.

`coverage_exclusions` is a person's decision that a managed device is not
expected to have a tool — a kiosk with no vulnerability agent by design, a
server the scanner may not touch — with the reason, who and when. It is what
turns a gap into `excluded` on the board, and it never hides the device: the
row stays readable, and withdrawing it is a second write (`removed_at`), not a
delete. A reason is required by the database as well as by the API, so no
path can record one without.

`coverage_snapshots` is the board once a day per client, written by the daily
collector: the per-site, per-tool counts and the list of gaps. One row per
(client, day), replaced if the day is run again, so a rerun changes nothing.
"Trend" and "new gaps since yesterday" are read from these rows, and so is
the board itself: the page shows the latest row and its time, and a refresh
(a person's, or the daily collector's) computes a new one in the background.

Both are scoped to a client (hard rule 1).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    Identity,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CoverageExclusion(Base):
    __tablename__ = "coverage_exclusions"
    __table_args__ = (
        CheckConstraint("length(btrim(reason)) > 0", name="ck_coverage_exclusions_reason"),
        Index("ix_coverage_exclusions_client_live", "client_id", "removed_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    site_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[str] = mapped_column(Text, nullable=False)
    """NinjaOne's device id, as text: stable across renames, unlike the hostname."""
    device_name: Mapped[str] = mapped_column(Text, nullable=False)
    """The hostname at the time, for the page and the audit row."""
    tool: Mapped[str] = mapped_column(Text, nullable=False)
    """A key from `app/coverage/config.COVERAGE_TOOLS`."""
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    """The user's email at the time: the page prints it without a join, and it
    is what the audit row says."""
    created_at: Mapped[dt.datetime] = mapped_column(nullable=False, server_default=text("now()"))
    removed_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    removed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    removed_by: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def live(self) -> bool:
        return self.removed_at is None


class CoverageSnapshot(Base):
    __tablename__ = "coverage_snapshots"
    __table_args__ = (
        UniqueConstraint("client_id", "taken_on", name="uq_coverage_snapshots_client_day"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    taken_on: Mapped[dt.date] = mapped_column(Date, nullable=False)
    """The UTC day. One snapshot a day; a rerun replaces it."""
    taken_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    taken_by: Mapped[str] = mapped_column(Text, nullable=False)
    duration_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    """Per tool and per site: managed, covered, stale, absent, unmatched, excluded,
    not_assessed, covered_percent. The shape `app/coverage/snapshots.py` writes."""
    gaps: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    """Every (site, device, tool, state) that was a gap that day, so the next day
    can say which are new. Device names, so a demo render must not print this."""
    board: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    """The whole board the snapshot was made from (`app/coverage/serialize.py`),
    computed without exclusions so the page can lay the live ones over it. The
    page reads the latest row and never computes on load; NULL on rows from
    before this was stored, which the page reports as needing a refresh."""
