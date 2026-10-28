"""Suggestions on live tickets: what the agent said, and what an engineer made of it.

Revision ID: 0025
Revises: 0024

Three tables, and the split between the first two is the whole point of them.
`ticket_suggestions` is what the agent said about a ticket as it arrived.
`suggestion_reviews` is what the engineer working that ticket thought of it,
having read it — approval, which is reported and never gated, because an
engineer who has seen the answer is agreeing with it. `blind_checks` is what an
engineer said about a share of those tickets *before* anything was revealed,
which is the only thing here the gate may score.

Both judgement tables are **append only**, like `labelling_answers` and the
audit log: a change of mind is a new row and the latest one scores.
`reviewer_user_id` is RESTRICT on both, for the reason every actor column is —
nothing done to an account may remove the record of what that person judged.

**No ticket text.** A suggestion is labels, confidences and ids; it points at
the enrichment snapshot rather than copying it. The display threshold in force
is stored on every row, so a figure read later knows which bar the suggestions
it counted had to clear, and `blind_check` is decided once and written down
rather than re-rolled per page load — the card would otherwise flip between
asking and telling when somebody refreshed.

`audit_log` gains `autopilot_suggestion_reviewed`, by the same constraint swap
0019, 0023 and 0024 used: a review changes a figure we report, so who said what
and when belongs on the record.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
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
    "report_config_changed",
    "events_collected",
    "login",
    "logout",
    "client_switched",
    "access_denied",
    "user_invited",
    "invite_accepted",
    "user_changed",
    "coverage_excluded",
    "coverage_snapshot",
    "devices_resolved",
    "device_linked",
    "ticket_device_chosen",
    "ticket_plan_generated",
    "ticket_plan_reviewed",
    "ticket_outcome_recorded",
    "autopilot_run_started",
    "autopilot_run_finished",
    "autopilot_run_failed",
    "autopilot_labelled",
)
#: One type for both kinds of judgement, told apart by the payload's `action`:
#: a review of a field, a blind answer, or the whole card marked correct.
ADDED = ("autopilot_suggestion_reviewed",)
AFTER = (*BEFORE, *ADDED)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.create_table(
        "ticket_suggestions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("prediction_id", sa.BigInteger(), nullable=True),
        sa.Column("enrichment_id", sa.BigInteger(), nullable=True),
        sa.Column("ticket_id", sa.Text(), nullable=False),
        sa.Column("ticket_number", sa.Text(), nullable=True),
        sa.Column("site_id", sa.Uuid(), nullable=True),
        sa.Column("issue_type", sa.Text(), nullable=True),
        sa.Column("issue_type_confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("origin", sa.Text(), nullable=True),
        sa.Column("origin_confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("urgency", sa.Text(), nullable=True),
        sa.Column("urgency_confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("group_with", sa.Text(), nullable=True),
        sa.Column("group_with_number", sa.Text(), nullable=True),
        sa.Column("group_score", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("display_threshold", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("blind_check", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_ticket_suggestions_client_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["enrichment_id"],
            ["shadow_enrichments.id"],
            name=op.f("fk_ticket_suggestions_enrichment_id"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["prediction_id"],
            ["shadow_predictions.id"],
            name=op.f("fk_ticket_suggestions_prediction_id"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["shadow_runs.id"],
            name=op.f("fk_ticket_suggestions_run_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["site_id"],
            ["sites.id"],
            name=op.f("fk_ticket_suggestions_site_id"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ticket_suggestions")),
        sa.UniqueConstraint("run_id", "ticket_id", name="uq_ticket_suggestions_run_ticket"),
    )
    op.create_index(
        "ix_ticket_suggestions_client", "ticket_suggestions", ["client_id", "created_at"]
    )
    op.create_index(
        "ix_ticket_suggestions_ticket", "ticket_suggestions", ["client_id", "ticket_id"]
    )

    op.create_table(
        "suggestion_reviews",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("suggestion_id", sa.Uuid(), nullable=False),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column("corrected_value", sa.Text(), nullable=True),
        sa.Column("reviewer_user_id", sa.Uuid(), nullable=True),
        sa.Column("reviewer_ref", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "field IN ('issue_type', 'origin', 'urgency', 'repeat')",
            name=op.f("ck_suggestion_reviews_field_is_a_known_one"),
        ),
        sa.CheckConstraint(
            "verdict IN ('correct', 'changed', 'wrong')",
            name=op.f("ck_suggestion_reviews_verdict_is_a_known_one"),
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_user_id"],
            ["users.id"],
            name=op.f("fk_suggestion_reviews_reviewer_user_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["suggestion_id"],
            ["ticket_suggestions.id"],
            name=op.f("fk_suggestion_reviews_suggestion_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_suggestion_reviews")),
    )
    op.create_index(
        "ix_suggestion_reviews_suggestion", "suggestion_reviews", ["suggestion_id", "created_at"]
    )

    op.create_table(
        "blind_checks",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("suggestion_id", sa.Uuid(), nullable=True),
        sa.Column("ticket_id", sa.Text(), nullable=False),
        sa.Column("field", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("reviewer_user_id", sa.Uuid(), nullable=True),
        sa.Column("reviewer_ref", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revealed", sa.Boolean(), nullable=False),
        sa.CheckConstraint(
            "field IN ('origin', 'issue_type')",
            name=op.f("ck_blind_checks_field_is_a_known_one"),
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_blind_checks_client_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_user_id"],
            ["users.id"],
            name=op.f("fk_blind_checks_reviewer_user_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["suggestion_id"],
            ["ticket_suggestions.id"],
            name=op.f("fk_blind_checks_suggestion_id"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_blind_checks")),
    )
    op.create_index("ix_blind_checks_client", "blind_checks", ["client_id", "created_at"])
    op.create_index("ix_blind_checks_ticket", "blind_checks", ["client_id", "ticket_id"])

    op.drop_constraint(op.f(EVENT_CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(op.f(EVENT_CONSTRAINT), "audit_log", _in("event_type", AFTER))


def downgrade() -> None:
    op.execute("DELETE FROM audit_log WHERE " + _in("event_type", ADDED))
    op.drop_constraint(op.f(EVENT_CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(op.f(EVENT_CONSTRAINT), "audit_log", _in("event_type", BEFORE))

    op.drop_index("ix_blind_checks_ticket", table_name="blind_checks")
    op.drop_index("ix_blind_checks_client", table_name="blind_checks")
    op.drop_table("blind_checks")
    op.drop_index("ix_suggestion_reviews_suggestion", table_name="suggestion_reviews")
    op.drop_table("suggestion_reviews")
    op.drop_index("ix_ticket_suggestions_ticket", table_name="ticket_suggestions")
    op.drop_index("ix_ticket_suggestions_client", table_name="ticket_suggestions")
    op.drop_table("ticket_suggestions")
