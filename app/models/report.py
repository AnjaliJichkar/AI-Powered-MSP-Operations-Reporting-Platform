"""Business-review storage: a franchisee's report settings, and the reviews themselves.

`report_configs` holds one row per client (franchisee): how its reports are
branded and what it charges. It is configuration, not a credential — but the
price book is commercially sensitive, so it is only ever read for its own
client, like everything else.

`tbr_reports` holds one review document per (site, period). The document is the
whole review as JSON: facts with their definitions, findings, the drafted and
edited sections, and who approved what. Two things follow from keeping it all:

- the *prior snapshot* that "improved since last review" is measured against is
  simply the previous period's row, so a delta is never computed from memory;
- a rendered deck can always be traced back to the facts it was rendered from.

Reports are superseded, never deleted: regenerating a period writes a new row
and marks the old one `superseded`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin


class ReportConfig(Base, TimestampMixin):
    """One franchisee's report settings. See `app/reports/tbr/config.py` for the shape."""

    __tablename__ = "report_configs"

    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), primary_key=True
    )
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    updated_by: Mapped[str | None] = mapped_column(Text, nullable=True)


class TbrReport(Base, TimestampMixin):
    """One business review: a client's site, for one period."""

    __tablename__ = "tbr_reports"
    __table_args__ = (
        Index("ix_tbr_reports_site_period", "site_id", "period_key", text("created_at DESC")),
        Index("ix_tbr_reports_client", "client_id", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
    )
    """The franchisee. A report is only ever read through this id (hard rule 1)."""
    site_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sites.id", ondelete="CASCADE"), nullable=False
    )
    period_key: Mapped[str] = mapped_column(Text, nullable=False)
    """`2026-08/1`: the last month of the period and its length in months."""
    period_start: Mapped[dt.datetime] = mapped_column(nullable=False)
    period_end: Mapped[dt.datetime] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'draft'"))
    """`draft`, `rendered` or `superseded`."""
    document: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    rendered_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    rendered_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
