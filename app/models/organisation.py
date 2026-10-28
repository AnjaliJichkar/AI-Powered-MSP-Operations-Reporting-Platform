"""Who signs in, and what they are allowed to see.

The engine already had one tenancy axis: a `Client` is a franchisee and a `Site`
is one of that franchisee's end customers. Authentication adds a second, and the
two are deliberately separate objects:

- a **`Client`** is a customer record. It owns credentials and vendor mappings,
  and it exists whether or not anybody from that MSP ever signs in.
- an **`Organisation`** is a set of people who sign in. Exactly one of them is
  Inputiv itself; every other is bound to one client.

Keeping them apart means a franchisee can be onboarded, synced and reported on
before a single account exists for it, and that deactivating a client's logins
is not the same act as deactivating the client.

Everything here is append-mostly. Users and organisations are suspended rather
than deleted, because `audit_log` rows point at them and hard rule 4 says the
record of what happened is not rewritten by anything done afterwards.

**An external identity provider is a later change, and the shape is here for
it.** `Organisation.identity_provider` is the default for users created in it,
`User.identity_provider` is the one that governs that user, and
`User.external_subject` holds the provider's stable subject claim. A user on an
external provider has no `password_hash` and no TOTP secret of ours: the
provider owns both. Nothing in this module reads a password except when the
governing provider is `local`, so adding Entra for one organisation does not
touch another.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import IdentityProvider, OrganisationKind, Role, UserStatus
from app.models.mixins import TimestampMixin
from app.models.types import (
    identity_provider_type,
    organisation_kind_type,
    role_type,
    user_status_type,
)

if TYPE_CHECKING:
    from app.models.client import Client

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{1,62}$"

#: Roles an organisation of each kind may hold. The database enforces the same
#: pairing with a check constraint, so a role cannot be set that the resolver
#: would then have to second-guess.
ROLES_BY_KIND: dict[OrganisationKind, frozenset[Role]] = {
    OrganisationKind.INPUTIV: frozenset({Role.INPUTIV_ADMIN, Role.INPUTIV_ENGINEER}),
    OrganisationKind.FRANCHISEE: frozenset(
        {Role.FRANCHISEE_ADMIN, Role.FRANCHISEE_TECHNICIAN, Role.FRANCHISEE_VIEWER}
    ),
}

_INPUTIV_ROLES = "'inputiv_admin', 'inputiv_engineer'"
_FRANCHISEE_ROLES = "'franchisee_admin', 'franchisee_technician', 'franchisee_viewer'"


class Organisation(Base, TimestampMixin):
    """A set of people who sign in: Inputiv itself, or one franchisee's staff."""

    __tablename__ = "organisations"
    __table_args__ = (
        CheckConstraint(f"slug ~ '{SLUG_PATTERN}'", name="slug_format"),
        # A franchisee organisation is meaningless without its client, and the
        # Inputiv organisation must not name one: its people reach every client
        # through an explicit switch, never through a column.
        CheckConstraint(
            "(kind = 'franchisee' AND client_id IS NOT NULL) OR "
            "(kind = 'inputiv' AND client_id IS NULL)",
            name="client_matches_kind",
        ),
        # One organisation of people per client, and exactly one Inputiv.
        Index(
            "uq_organisations_client",
            "client_id",
            unique=True,
            postgresql_where=text("client_id IS NOT NULL"),
        ),
        Index(
            "uq_organisations_inputiv",
            "kind",
            unique=True,
            postgresql_where=text("kind = 'inputiv'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    kind: Mapped[OrganisationKind] = mapped_column(organisation_kind_type, nullable=False)
    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)

    client_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("clients.id", ondelete="RESTRICT"), nullable=True
    )
    """The franchisee these people work for. Null for Inputiv, and only for
    Inputiv. RESTRICT: a client with accounts is not deleted out from under them."""

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    """False refuses every sign-in for the whole organisation at once."""

    identity_provider: Mapped[IdentityProvider] = mapped_column(
        identity_provider_type, nullable=False, server_default=text("'local'")
    )
    """The default provider for users created here. Only `local` is implemented."""

    identity_provider_config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    """Per-organisation provider settings (tenant id, issuer, allowed domains)
    for whoever wires up the provider. Never a client secret: those belong in
    the secrets manager, like every other credential (hard rule 3)."""

    client: Mapped[Client | None] = relationship(lazy="selectin")
    users: Mapped[list[User]] = relationship(
        back_populates="organisation", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def allowed_roles(self) -> frozenset[Role]:
        return ROLES_BY_KIND[self.kind]

    def __repr__(self) -> str:
        return f"<Organisation slug={self.slug!r} kind={self.kind!r}>"


class User(Base, TimestampMixin):
    """One person who signs in. One organisation, one role."""

    __tablename__ = "users"
    __table_args__ = (
        # Email is the login, so it is unique across every organisation: one
        # address is one person, and a sign-in never has to ask which tenant.
        UniqueConstraint("email", name="uq_users_email"),
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        CheckConstraint("position('@' in email) > 1", name="email_shape"),
        CheckConstraint(
            f"(role IN ({_INPUTIV_ROLES}) AND organisation_kind = 'inputiv') OR "
            f"(role IN ({_FRANCHISEE_ROLES}) AND organisation_kind = 'franchisee')",
            name="role_matches_organisation_kind",
        ),
        # Site scope is carried but unused: no user is pinned to a site today,
        # and only a franchisee user ever could be.
        CheckConstraint(
            "site_id IS NULL OR organisation_kind = 'franchisee'",
            name="site_scope_is_franchisee_only",
        ),
        # A local user signs in with a password; an external one never has ours.
        CheckConstraint(
            "identity_provider <> 'local' OR external_subject IS NULL",
            name="external_subject_is_not_local",
        ),
        Index("ix_users_organisation_id", "organisation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False
    )
    organisation_kind: Mapped[OrganisationKind] = mapped_column(
        organisation_kind_type, nullable=False
    )
    """Copied from the organisation so the role/kind pairing is one check
    constraint rather than a trigger. Kept in step by `create_user`."""

    email: Mapped[str] = mapped_column(Text, nullable=False)
    """Lower-cased at every entry point; the database refuses anything else."""

    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    role: Mapped[Role] = mapped_column(role_type, nullable=False)
    status: Mapped[UserStatus] = mapped_column(
        user_status_type, nullable=False, server_default=text("'invited'")
    )

    site_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("sites.id", ondelete="SET NULL"), nullable=True
    )
    """The one site this user is restricted to, if ever. Always null today:
    scope is (organisation, optional site) and the site half is not yet used."""

    identity_provider: Mapped[IdentityProvider] = mapped_column(
        identity_provider_type, nullable=False, server_default=text("'local'")
    )
    external_subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The external provider's stable subject claim, when one governs this user."""

    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    """argon2id, encoded with its own parameters. Null for an invited user who
    has not set one, and for a user an external provider governs."""

    password_changed_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)

    totp_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The shared secret, encrypted at rest with the credential cipher. Never
    returned by any endpoint after enrolment."""

    totp_confirmed_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    """Set when the user proved a code from the secret. A second factor is
    required at login only from this moment: an abandoned enrolment never
    locks anybody out."""

    failed_login_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    locked_until: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)

    organisation: Mapped[Organisation] = relationship(back_populates="users", lazy="selectin")

    @property
    def totp_enabled(self) -> bool:
        return self.totp_secret is not None and self.totp_confirmed_at is not None

    def __repr__(self) -> str:
        return f"<User id={self.id!r} role={self.role!r} status={self.status!r}>"


class Invite(Base, TimestampMixin):
    """A one-time link that lets one address create one account."""

    __tablename__ = "invites"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_invites_token_hash"),
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        Index("ix_invites_email", "email"),
        Index("ix_invites_organisation_id", "organisation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    organisation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organisations.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[Role] = mapped_column(role_type, nullable=False)

    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    """SHA-256 of the token. The token itself is shown once, when the invite is
    created, and is not recoverable afterwards: a stolen database yields no
    usable invite link."""

    expires_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    accepted_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    accepted_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    revoked_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    """Null for the first invite of all, which the bootstrap CLI writes before
    any user exists."""

    organisation: Mapped[Organisation] = relationship(lazy="selectin")

    def is_open(self, now: dt.datetime) -> bool:
        return self.accepted_at is None and self.revoked_at is None and self.expires_at > now


class Session(Base):
    """One signed-in browser. The cookie carries a token; this row is the truth.

    Two expiries, both enforced on every request: `expires_at` is absolute and
    never extended, `last_seen_at` plus the idle window closes a session left
    alone. A session is revoked, never deleted, so a sign-out is still visible
    in the record alongside the audit row it wrote.
    """

    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_sessions_token_hash"),
        Index("ix_sessions_user_id", "user_id"),
        Index("ix_sessions_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, server_default=text("gen_random_uuid()")
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    """SHA-256 of the cookie value. The cookie is the only copy of the token."""

    csrf_token: Mapped[str] = mapped_column(Text, nullable=False)
    """Held in the clear on purpose: it is handed back to the page on every
    `GET /auth/me` so a reload can send it again, and it is worthless without
    the session cookie, which script cannot read."""

    created_at: Mapped[dt.datetime] = mapped_column(server_default=text("now()"), nullable=False)
    last_seen_at: Mapped[dt.datetime] = mapped_column(server_default=text("now()"), nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(nullable=False)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)

    acting_client_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("clients.id", ondelete="RESTRICT"), nullable=True
    )
    """Which franchisee Inputiv staff are currently working on. Set only by the
    switch endpoint, which writes an audit row for every change. A franchisee
    user's client comes from their organisation and this stays null."""

    ip_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    user: Mapped[User] = relationship(lazy="selectin")

    def is_live(self, now: dt.datetime, *, idle_seconds: float) -> bool:
        if self.revoked_at is not None:
            return False
        if self.expires_at <= now:
            return False
        return (now - self.last_seen_at).total_seconds() < idle_seconds
