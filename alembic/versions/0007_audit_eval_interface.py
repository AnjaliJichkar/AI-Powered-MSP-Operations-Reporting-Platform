"""`eval` as an audit interface, so harness runs can be told from real traffic.

Revision ID: 0007
Revises: 0006

`scripts/selection_check.py` asks the agent scripted questions to measure which
tool the model picks. Hard rule 4 still applies — every prompt and model
response is logged — but those rows are not engineers' questions, and usage
figures, feedback reviews and cost reports must be able to leave them out.
They are recorded with `interface = 'eval'`, which no real entry point uses:
`WHERE interface <> 'eval'` is normal traffic.

`audit_log.interface` is a varchar with a check constraint (see
`app/models/types.py`), so a new value is a constraint swap.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_audit_log_interface"

BEFORE = ("slack", "api", "cli", "system")
AFTER = (*BEFORE, "eval")


def _condition(values: tuple[str, ...]) -> str:
    return "interface IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    op.drop_constraint(op.f(CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), "audit_log", _condition(AFTER))


def downgrade() -> None:
    # The audit trail is not rewritten to make a downgrade fit: if harness runs
    # have been recorded, the old constraint cannot hold them and this fails
    # loudly rather than deleting or relabelling audit rows.
    op.drop_constraint(op.f(CONSTRAINT), "audit_log", type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), "audit_log", _condition(BEFORE))
