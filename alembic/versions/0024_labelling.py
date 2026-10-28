"""Where a person's judgement is written down: three labelling tables.

Revision ID: 0024
Revises: 0023

Two of the four things Autopilot Level 0 measures have no ground truth in any
table we hold — where a ticket came from, and whether a group is one thing —
so somebody has to read some tickets and say. `labelling_sets` is one sample of
one run (the 200 origin tickets, or that run's 30 groups), `labelling_items` is
one thing to judge, and `labelling_answers` is what one person said about it
once.

**Answers are appended, never updated.** A change of mind is a new row and an
undo is a row saying `withdrawn`; the latest row per labeller scores and the
rest is how a disputed label is looked into. `labeller_user_id` is RESTRICT
like every other actor column: nothing done to an account may remove the record
of what that person judged.

**No ticket text is stored.** An item is an id and a position; the words a
labeller reads are fetched from the ledger, scrubbed, at display time. A note
the labeller writes is scrubbed on the way in, because somebody typing a note
is as likely to paste a password as somebody typing a ticket.

A set belongs to a run and CASCADEs from it, as the run CASCADEs from the
client: a group only exists as that run's opinion, so a sample of it cannot
outlive the run that made it.

`audit_log` also gains `autopilot_labelled`, by the same constraint swap 0019
and 0023 used: a label is ground truth the gate is read against, so who judged
what and when belongs on the record.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0024"
down_revision: str | None = "0023"
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
)
#: A label is ground truth the gate is read against, so who judged what is on
#: the record like any other act. At the end of the enum, where it is declared.
ADDED = ("autopilot_labelled",)
AFTER = (*BEFORE, *ADDED)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.create_table(
        "labelling_sets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("double_check", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('origin', 'groups')",
            name=op.f("ck_labelling_sets_kind_is_origin_or_groups"),
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_labelling_sets_client_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["shadow_runs.id"],
            name=op.f("fk_labelling_sets_run_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_labelling_sets")),
        sa.UniqueConstraint("run_id", "kind", name="uq_labelling_sets_run_kind"),
    )
    op.create_index(
        "ix_labelling_sets_client", "labelling_sets", ["client_id", "created_at"], unique=False
    )
    op.create_table(
        "labelling_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("set_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("double_check", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["set_id"],
            ["labelling_sets.id"],
            name=op.f("fk_labelling_items_set_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_labelling_items")),
        sa.UniqueConstraint("set_id", "subject_id", name="uq_labelling_items_subject"),
    )
    op.create_index(
        "ix_labelling_items_set", "labelling_items", ["set_id", "position"], unique=False
    )
    op.create_table(
        "labelling_answers",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("item_id", sa.BigInteger(), nullable=False),
        sa.Column("labeller_user_id", sa.Uuid(), nullable=True),
        sa.Column("labeller_ref", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("excluded", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "answer IN ('alert', 'human', 'not_same', 'partly', 'same', 'skipped', "
            "'unsure', 'withdrawn')",
            name=op.f("ck_labelling_answers_answer_is_a_known_one"),
        ),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["labelling_items.id"],
            name=op.f("fk_labelling_answers_item_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["labeller_user_id"],
            ["users.id"],
            name=op.f("fk_labelling_answers_labeller_user_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_labelling_answers")),
    )
    op.create_index(
        "ix_labelling_answers_item", "labelling_answers", ["item_id", "created_at"], unique=False
    )

    op.drop_constraint(op.f(EVENT_CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(op.f(EVENT_CONSTRAINT), "audit_log", _in("event_type", AFTER))


def downgrade() -> None:
    op.execute("DELETE FROM audit_log WHERE " + _in("event_type", ADDED))
    op.drop_constraint(op.f(EVENT_CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(op.f(EVENT_CONSTRAINT), "audit_log", _in("event_type", BEFORE))

    op.drop_index("ix_labelling_answers_item", table_name="labelling_answers")
    op.drop_table("labelling_answers")
    op.drop_index("ix_labelling_items_set", table_name="labelling_items")
    op.drop_table("labelling_items")
    op.drop_index("ix_labelling_sets_client", table_name="labelling_sets")
    op.drop_table("labelling_sets")
