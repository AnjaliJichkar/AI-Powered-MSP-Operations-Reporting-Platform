"""Authentication and per-organisation authorization.

Revision ID: 0011
Revises: 0010

Four new tables — `organisations`, `users`, `invites`, `sessions` — and two new
nullable columns on tables that already exist.

The engine had no identity before this: an engineer typed a handle and the
engine believed it. That handle is now retired at the entry points, but it is
**not** rewritten in the record. `audit_log.actor_id` and
`conversations.engineer_id` keep whatever they already hold; the new
`audit_log.actor_user_id` and `conversations.owner_user_id` are null on every
existing row and set from the session on every new one. Hard rule 4 says the
record of what happened is append-only, and a migration that reassigned old
rows to a user account that did not exist at the time would be a lie about the
past, not a fix.

Both new foreign keys are ON DELETE RESTRICT, for the same reason
`audit_log.conversation_id` is: nothing done to an account may remove or
rewrite the record of what it did.

`audit_log.event_type` gains seven values by the drop-and-re-add of its check
constraint that 0005 and 0006 used.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENT_CONSTRAINT = "ck_audit_log_audit_event_type"

BEFORE = (
    "run_started",
    "prompt",
    "model_response",
    "tool_call",
    "tool_result",
    "run_finished",
    "error",
    "web_search",
    "conversation_started",
    "conversation_archived",
    "context_trimmed",
    "report_generated",
    "report_reviewed",
    "report_rendered",
    "events_collected",
)
ADDED = (
    "login",
    "logout",
    "client_switched",
    "access_denied",
    "user_invited",
    "invite_accepted",
    "user_changed",
)
AFTER = (*BEFORE, *ADDED)

ORGANISATION_KINDS = ("inputiv", "franchisee")
ROLES = ("inputiv_admin", "inputiv_engineer", "franchisee_admin", "franchisee_viewer")
INPUTIV_ROLES = "'inputiv_admin', 'inputiv_engineer'"
FRANCHISEE_ROLES = "'franchisee_admin', 'franchisee_viewer'"
USER_STATUSES = ("invited", "active", "suspended")
IDENTITY_PROVIDERS = ("local", "entra")

SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{1,62}$"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def _timestamps() -> list[sa.Column[sa.DateTime]]:
    return [
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def upgrade() -> None:
    op.create_table(
        "organisations",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "identity_provider",
            sa.String(length=16),
            server_default=sa.text("'local'"),
            nullable=False,
        ),
        sa.Column(
            "identity_provider_config",
            sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        *_timestamps(),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
        sa.CheckConstraint(f"slug ~ '{SLUG_PATTERN}'", name=op.f("ck_organisations_slug_format")),
        sa.CheckConstraint(
            _in("kind", ORGANISATION_KINDS), name=op.f("ck_organisations_organisation_kind")
        ),
        sa.CheckConstraint(
            _in("identity_provider", IDENTITY_PROVIDERS),
            name=op.f("ck_organisations_identity_provider"),
        ),
        sa.CheckConstraint(
            "(kind = 'franchisee' AND client_id IS NOT NULL) OR "
            "(kind = 'inputiv' AND client_id IS NULL)",
            name=op.f("ck_organisations_client_matches_kind"),
        ),
    )
    # One set of people per client, and exactly one Inputiv organisation.
    op.create_index(
        "uq_organisations_client",
        "organisations",
        ["client_id"],
        unique=True,
        postgresql_where=sa.text("client_id IS NOT NULL"),
    )
    op.create_index(
        "uq_organisations_inputiv",
        "organisations",
        ["kind"],
        unique=True,
        postgresql_where=sa.text("kind = 'inputiv'"),
    )

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("organisation_kind", sa.String(length=16), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("role", sa.String(length=24), nullable=False),
        sa.Column(
            "status", sa.String(length=16), server_default=sa.text("'invited'"), nullable=False
        ),
        sa.Column("site_id", sa.Uuid(), nullable=True),
        sa.Column(
            "identity_provider",
            sa.String(length=16),
            server_default=sa.text("'local'"),
            nullable=False,
        ),
        sa.Column("external_subject", sa.Text(), nullable=True),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("password_changed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("totp_secret", sa.Text(), nullable=True),
        sa.Column("totp_confirmed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("failed_login_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("locked_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.TIMESTAMP(timezone=True), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["site_id"], ["sites.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_users_email_lowercase")),
        sa.CheckConstraint("position('@' in email) > 1", name=op.f("ck_users_email_shape")),
        sa.CheckConstraint(_in("role", ROLES), name=op.f("ck_users_user_role")),
        sa.CheckConstraint(_in("status", USER_STATUSES), name=op.f("ck_users_user_status")),
        sa.CheckConstraint(
            _in("organisation_kind", ORGANISATION_KINDS),
            name=op.f("ck_users_organisation_kind"),
        ),
        sa.CheckConstraint(
            _in("identity_provider", IDENTITY_PROVIDERS),
            name=op.f("ck_users_identity_provider"),
        ),
        sa.CheckConstraint(
            f"(role IN ({INPUTIV_ROLES}) AND organisation_kind = 'inputiv') OR "
            f"(role IN ({FRANCHISEE_ROLES}) AND organisation_kind = 'franchisee')",
            name=op.f("ck_users_role_matches_organisation_kind"),
        ),
        sa.CheckConstraint(
            "site_id IS NULL OR organisation_kind = 'franchisee'",
            name=op.f("ck_users_site_scope_is_franchisee_only"),
        ),
        sa.CheckConstraint(
            "identity_provider <> 'local' OR external_subject IS NULL",
            name=op.f("ck_users_external_subject_is_not_local"),
        ),
    )
    op.create_index("ix_users_organisation_id", "users", ["organisation_id"])

    op.create_table(
        "invites",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("organisation_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("role", sa.String(length=24), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("accepted_user_id", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["organisation_id"], ["organisations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["accepted_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name=op.f("uq_invites_token_hash")),
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_invites_email_lowercase")),
        sa.CheckConstraint(_in("role", ROLES), name=op.f("ck_invites_user_role")),
    )
    op.create_index("ix_invites_email", "invites", ["email"])
    op.create_index("ix_invites_organisation_id", "invites", ["organisation_id"])

    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("csrf_token", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("acting_client_id", sa.Uuid(), nullable=True),
        sa.Column("ip_address", sa.Text(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["acting_client_id"], ["clients.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name=op.f("uq_sessions_token_hash")),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])

    # --- the actor, on rows that already exist -------------------------------
    op.add_column("audit_log", sa.Column("actor_user_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_audit_log_actor_user_id"),
        "audit_log",
        "users",
        ["actor_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_audit_log_actor_user_id_created_at", "audit_log", ["actor_user_id", "created_at"]
    )

    op.add_column("conversations", sa.Column("owner_user_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_conversations_owner_user_id"),
        "conversations",
        "users",
        ["owner_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.drop_constraint(op.f(EVENT_CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(op.f(EVENT_CONSTRAINT), "audit_log", _in("event_type", AFTER))


def downgrade() -> None:
    # The rows the new event types describe cannot be rewritten as one of the
    # old ones, so they go with the constraint that allowed them.
    op.execute("DELETE FROM audit_log WHERE " + _in("event_type", ADDED))
    op.drop_constraint(op.f(EVENT_CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(op.f(EVENT_CONSTRAINT), "audit_log", _in("event_type", BEFORE))

    op.drop_constraint(op.f("fk_conversations_owner_user_id"), "conversations", type_="foreignkey")
    op.drop_column("conversations", "owner_user_id")

    op.drop_index("ix_audit_log_actor_user_id_created_at", table_name="audit_log")
    op.drop_constraint(op.f("fk_audit_log_actor_user_id"), "audit_log", type_="foreignkey")
    op.drop_column("audit_log", "actor_user_id")

    op.drop_index("ix_sessions_expires_at", table_name="sessions")
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_table("sessions")
    op.drop_index("ix_invites_organisation_id", table_name="invites")
    op.drop_index("ix_invites_email", table_name="invites")
    op.drop_table("invites")
    op.drop_index("ix_users_organisation_id", table_name="users")
    op.drop_table("users")
    op.drop_index("uq_organisations_inputiv", table_name="organisations")
    op.drop_index("uq_organisations_client", table_name="organisations")
    op.drop_table("organisations")
