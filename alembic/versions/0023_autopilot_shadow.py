"""Autopilot Level 0: the shadow probe's own three tables, and three event types.

Revision ID: 0023
Revises: 0022

What the agent would have said, stored beside the id of the ticket it was said
about — never the ticket's words. `shadow_runs` is one replay or forward pass
with the commit, model, prompt version and config that make two runs
comparable; `shadow_predictions` is one row per ticket with the predicted
labels, their confidences, which path answered and what the call cost; and
`shadow_enrichments` is what the tools said about one new ticket's machine in
the forward shadow, as counts and states, with the ticket's site beside the
device's so an enrichment of the wrong customer's machine is countable.

A prediction keeps the model's **raw** answer beside the one the franchisee's
label policy produced, so the policy's effect is measurable on the same run;
cached input is counted apart from input, because it is billed at a fraction
and a replay reads the same instructions thousands of times.
Scoring joins the predictions to `ticket_outcomes` (0022), which holds what the
ticket turned out to be, so nothing about the outcome is copied in here either.

All three CASCADE from the client: a measurement is derived, and running the
replay again is the way to get it back. `audit_log` gains `autopilot_run_started`,
`autopilot_run_finished` and `autopilot_run_failed` by the same constraint swap
0014, 0016 and 0019 used for their event types.

The ledger widening this phase deliberately does **not** do — sub-issue type,
contact, configuration item, time aggregates — is 0024.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0023"
down_revision: str | None = "0022"
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
)
#: At the end of the enum, which is where they are declared: the models'
#: constraint is generated from it and the migration check compares the two
#: text for text.
ADDED = ("autopilot_run_started", "autopilot_run_finished", "autopilot_run_failed")
AFTER = (*BEFORE, *ADDED)


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.create_table(
        "shadow_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("code_commit", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("tickets_seen", sa.Integer(), nullable=False),
        sa.Column("by_rule", sa.Integer(), nullable=False),
        sa.Column("by_model", sa.Integer(), nullable=False),
        sa.Column("errors", sa.Integer(), nullable=False),
        sa.Column("groups", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_read_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cost", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("stopped_early", sa.Boolean(), nullable=False),
        sa.Column("stopped_reason", sa.Text(), nullable=True),
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
        sa.CheckConstraint(
            "mode IN ('replay', 'forward')",
            name=op.f("ck_shadow_runs_mode_is_replay_or_forward"),
        ),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name=op.f("fk_shadow_runs_client_id"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shadow_runs")),
    )
    op.create_index(
        "ix_shadow_runs_client", "shadow_runs", ["client_id", "started_at"], unique=False
    )
    op.create_table(
        "shadow_predictions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("ticket_id", sa.Text(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=True),
        sa.Column("created_at_vendor", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("origin", sa.Text(), nullable=True),
        sa.Column("origin_confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("issue_type", sa.Text(), nullable=True),
        sa.Column("issue_type_raw", sa.Text(), nullable=True),
        sa.Column("issue_type_policy", sa.Text(), nullable=True),
        sa.Column("issue_type_confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("urgency", sa.Text(), nullable=True),
        sa.Column("urgency_confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("group_with", sa.Text(), nullable=True),
        sa.Column("group_basis", sa.Text(), nullable=True),
        sa.Column("group_score", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("rule_name", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=True),
        sa.Column("cost", sa.Numeric(precision=10, scale=6), nullable=True),
        sa.Column("error_type", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("title_quarantined", sa.Boolean(), nullable=False),
        sa.Column("queue_label_at_arrival", sa.Text(), nullable=True),
        sa.Column("has_description", sa.Boolean(), nullable=True),
        sa.CheckConstraint(
            "origin IS NULL OR origin IN ('alert', 'human', 'unsure')",
            name=op.f("ck_shadow_predictions_origin_is_alert_human_or_unsure"),
        ),
        sa.CheckConstraint(
            "path IN ('rule', 'model', 'error')",
            name=op.f("ck_shadow_predictions_path_is_rule_model_or_error"),
        ),
        sa.CheckConstraint(
            "urgency IS NULL OR urgency IN ('low', 'normal', 'high')",
            name=op.f("ck_shadow_predictions_urgency_is_low_normal_or_high"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["shadow_runs.id"],
            name=op.f("fk_shadow_predictions_run_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["site_id"],
            ["sites.id"],
            name=op.f("fk_shadow_predictions_site_id"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shadow_predictions")),
        sa.UniqueConstraint("run_id", "ticket_id", name="uq_shadow_predictions_ticket"),
    )
    op.create_index("ix_shadow_predictions_run", "shadow_predictions", ["run_id"], unique=False)
    op.create_table(
        "shadow_enrichments",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("ticket_id", sa.Text(), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=True),
        sa.Column("device_id", sa.Uuid(), nullable=True),
        sa.Column("device_site_id", sa.Uuid(), nullable=True),
        sa.Column("device_method", sa.Text(), nullable=False),
        sa.Column("device_confidence", sa.Text(), nullable=True),
        sa.Column("candidates", sa.Integer(), nullable=False),
        sa.Column("read_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("facts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("fact_count", sa.Integer(), nullable=False),
        sa.Column("reads", sa.Integer(), nullable=False),
        sa.Column("errors", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("not_assessed", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            name=op.f("fk_shadow_enrichments_device_id"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["device_site_id"],
            ["sites.id"],
            name=op.f("fk_shadow_enrichments_device_site_id"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["shadow_runs.id"],
            name=op.f("fk_shadow_enrichments_run_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["site_id"],
            ["sites.id"],
            name=op.f("fk_shadow_enrichments_site_id"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shadow_enrichments")),
        sa.UniqueConstraint("run_id", "ticket_id", name="uq_shadow_enrichments_ticket"),
    )
    op.create_index("ix_shadow_enrichments_run", "shadow_enrichments", ["run_id"], unique=False)

    op.drop_constraint(op.f(EVENT_CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(op.f(EVENT_CONSTRAINT), "audit_log", _in("event_type", AFTER))


def downgrade() -> None:
    op.execute("DELETE FROM audit_log WHERE " + _in("event_type", ADDED))
    op.drop_constraint(op.f(EVENT_CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(op.f(EVENT_CONSTRAINT), "audit_log", _in("event_type", BEFORE))

    op.drop_index("ix_shadow_enrichments_run", table_name="shadow_enrichments")
    op.drop_table("shadow_enrichments")
    op.drop_index("ix_shadow_predictions_run", table_name="shadow_predictions")
    op.drop_table("shadow_predictions")
    op.drop_index("ix_shadow_runs_client", table_name="shadow_runs")
    op.drop_table("shadow_runs")
