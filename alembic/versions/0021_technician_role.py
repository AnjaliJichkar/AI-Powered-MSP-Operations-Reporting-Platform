"""One role: `franchisee_technician`.

Revision ID: 0021
Revises: 0020

The people at a franchisee who fix things, as opposed to the administrator who
runs the account and the viewer who only reads what was released. Three check
constraints name the roles by hand — `users.role`, `invites.role`, and the
pairing of a role with its organisation kind — so all three are swapped.

Down again, any technician account and any outstanding technician invite would
violate the narrower constraints, so they are refused rather than deleted: a
person's account is not something a schema migration removes quietly. Change
those accounts to another role first, and withdraw those invites.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BEFORE = ("inputiv_admin", "inputiv_engineer", "franchisee_admin", "franchisee_viewer")
ADDED = ("franchisee_technician",)
# In the enum's order: `Role` declares the technician between the admin and the
# viewer, and `tests/test_migrations.py` compares the two text for text.
AFTER = (
    "inputiv_admin",
    "inputiv_engineer",
    "franchisee_admin",
    "franchisee_technician",
    "franchisee_viewer",
)

INPUTIV = "'inputiv_admin', 'inputiv_engineer'"
FRANCHISEE_BEFORE = "'franchisee_admin', 'franchisee_viewer'"
FRANCHISEE_AFTER = "'franchisee_admin', 'franchisee_technician', 'franchisee_viewer'"

ROLE_CONSTRAINTS = (("users", "ck_users_user_role"), ("invites", "ck_invites_user_role"))
PAIRING = "ck_users_role_matches_organisation_kind"


def _in(values: tuple[str, ...]) -> str:
    return "role IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def _pairing(franchisee: str) -> str:
    return (
        f"(role IN ({INPUTIV}) AND organisation_kind = 'inputiv') OR "
        f"(role IN ({franchisee}) AND organisation_kind = 'franchisee')"
    )


def _swap(roles: tuple[str, ...], franchisee: str) -> None:
    for table, name in ROLE_CONSTRAINTS:
        op.drop_constraint(op.f(name), table, type_="check")
        op.create_check_constraint(op.f(name), table, _in(roles))
    op.drop_constraint(op.f(PAIRING), "users", type_="check")
    op.create_check_constraint(op.f(PAIRING), "users", _pairing(franchisee))


def upgrade() -> None:
    _swap(AFTER, FRANCHISEE_AFTER)


def downgrade() -> None:
    for table, _ in ROLE_CONSTRAINTS:
        held = (
            op.get_bind()
            .exec_driver_sql(f"SELECT count(*) FROM {table} WHERE {_in(ADDED)}")
            .scalar_one()
        )
        if held:
            raise RuntimeError(
                f"{held} row(s) in {table} carry a role this migration removes. Change those "
                "accounts to another role, and withdraw those invites, before downgrading."
            )
    _swap(BEFORE, FRANCHISEE_BEFORE)
