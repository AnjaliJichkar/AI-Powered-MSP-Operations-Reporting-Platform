"""`web_search` as an audit step type.

Revision ID: 0005
Revises: 0004

The agent can now use Anthropic's server-side web search. Hard rule 4 says every
step is logged, and a search is a step: what the model asked the web and which
URLs it was given back. `audit_log.event_type` is a varchar with a check
constraint (see `app/models/types.py`), so a new value is a constraint swap.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_audit_log_audit_event_type"

BEFORE = (
    "run_started",
    "prompt",
    "model_response",
    "tool_call",
    "tool_result",
    "run_finished",
    "error",
)
AFTER = (*BEFORE, "web_search")


def _condition(values: tuple[str, ...]) -> str:
    return "event_type IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.drop_constraint(op.f(CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), "audit_log", _condition(AFTER))


def downgrade() -> None:
    # The audit trail is not rewritten to make a downgrade fit: if searches
    # have been recorded, the old constraint cannot hold them and this fails
    # loudly rather than deleting or relabelling audit rows.
    op.drop_constraint(op.f(CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), "audit_log", _condition(BEFORE))
