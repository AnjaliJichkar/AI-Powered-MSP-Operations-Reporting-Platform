"""A review can be about a finding, a next step, or the card itself.

Revision ID: 0026
Revises: 0025

The card an engineer sees now leads with what the tools found rather than with
a classification, so the things they can have an opinion about have grown: each
finding, the suggested next step, which machine it is, and the card as a whole.
Two constraints widen for that.

`field` keeps its fixed list and gains a **prefix**: `finding:<kind>`, where
the kind is the finding's own stable id (`app/autopilot/findings.py`). A
pattern rather than an enumeration because the kinds are a vocabulary that
grows — a new finding should not need a migration — while still refusing
anything that is not one of the shapes we read back.

`verdict` gains `useful` and `not_useful`. A finding is not right or wrong the
way a classification is: it is either worth the space it took on the card or it
is not, and asking "was this correct?" about "machine offline for 10 days"
would get the answer "yes, and I did not need telling".

Nothing is rewritten. Rows written under the old lists still satisfy the new
ones, which is what a widening means.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FIELD_CONSTRAINT = "ck_suggestion_reviews_field_is_a_known_one"
VERDICT_CONSTRAINT = "ck_suggestion_reviews_verdict_is_a_known_one"

#: The classification fields, as 0025 had them.
CLASSIFICATION = ("issue_type", "origin", "urgency", "repeat")
#: And what the redesigned card adds beside them.
ADDED_FIELDS = ("machine", "next_step", "card")

BEFORE_FIELD = "field IN ('issue_type', 'origin', 'urgency', 'repeat')"
AFTER_FIELD = (
    "field IN ('issue_type', 'origin', 'urgency', 'repeat', 'machine', 'next_step', 'card')"
    " OR field LIKE 'finding:%'"
)

BEFORE_VERDICT = "verdict IN ('correct', 'changed', 'wrong')"
AFTER_VERDICT = "verdict IN ('correct', 'changed', 'wrong', 'useful', 'not_useful')"


def upgrade() -> None:
    op.drop_constraint(op.f(FIELD_CONSTRAINT), "suggestion_reviews", type_="check")
    op.create_check_constraint(op.f(FIELD_CONSTRAINT), "suggestion_reviews", AFTER_FIELD)
    op.drop_constraint(op.f(VERDICT_CONSTRAINT), "suggestion_reviews", type_="check")
    op.create_check_constraint(op.f(VERDICT_CONSTRAINT), "suggestion_reviews", AFTER_VERDICT)


def downgrade() -> None:
    # The rows the widened constraints allowed go first, or the narrow ones
    # cannot be put back. They are verdicts on findings and on the card, which
    # the older schema has nowhere to keep.
    op.execute(
        "DELETE FROM suggestion_reviews WHERE NOT ("
        + BEFORE_FIELD
        + ") OR NOT ("
        + BEFORE_VERDICT
        + ")"
    )
    op.drop_constraint(op.f(VERDICT_CONSTRAINT), "suggestion_reviews", type_="check")
    op.create_check_constraint(op.f(VERDICT_CONSTRAINT), "suggestion_reviews", BEFORE_VERDICT)
    op.drop_constraint(op.f(FIELD_CONSTRAINT), "suggestion_reviews", type_="check")
    op.create_check_constraint(op.f(FIELD_CONSTRAINT), "suggestion_reviews", BEFORE_FIELD)
