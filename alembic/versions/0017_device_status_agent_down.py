"""Device status `rmm_agent_down`: NinjaOne has lost the machine, a vendor still has it.

Revision ID: 0017
Revises: 0016

The coverage join now lets an RMM-stale device take part in the passes; one
that pairs with a vendor row the tool still hears from is alive with its
NinjaOne agent down, and the device identity layer records that as its own
status rather than folding it into `rmm_stale`. The check constraint on
`devices.status` gains the value; nothing else changes.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_devices_status"
BEFORE = "status IN ('active', 'rmm_stale', 'retired')"
AFTER = "status IN ('active', 'rmm_stale', 'rmm_agent_down', 'retired')"


def upgrade() -> None:
    # `op.f`: the name is already in the convention's form (as 0014 created it).
    op.drop_constraint(op.f(CONSTRAINT), "devices", type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), "devices", AFTER)


def downgrade() -> None:
    # A device the new status names is a lost one to the old code.
    op.execute("UPDATE devices SET status = 'rmm_stale' WHERE status = 'rmm_agent_down'")
    op.drop_constraint(op.f(CONSTRAINT), "devices", type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), "devices", BEFORE)
