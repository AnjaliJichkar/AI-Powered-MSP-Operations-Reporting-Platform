"""Declarative base and the type conventions every model inherits.

The naming convention matters: without it Alembic autogenerate produces
migrations with unnamed constraints that cannot be dropped later.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, ClassVar

from sqlalchemy import TIMESTAMP, BigInteger, MetaData, Text, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # Postgres defaults applied to every annotated column:
    #   str      -> text (no artificial length limits)
    #   int      -> bigint (ids and counters do not overflow)
    #   datetime -> timestamptz (never a naive timestamp)
    #   dict     -> jsonb
    #   UUID     -> uuid
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        str: Text,
        int: BigInteger,
        dt.datetime: TIMESTAMP(timezone=True),
        dict[str, Any]: JSONB,
        uuid.UUID: Uuid(as_uuid=True),
    }

    def __repr__(self) -> str:
        return f"<{type(self).__name__} id={getattr(self, 'id', None)!r}>"
