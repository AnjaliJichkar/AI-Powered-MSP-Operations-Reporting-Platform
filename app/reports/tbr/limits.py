"""How many reviews a franchisee may generate of their own accord.

Generating a review is the expensive thing in this product: it reads every
connector the customer is on and then makes one model call per section. An
engineer at Inputiv doing that is work somebody asked for. A franchisee's
administrator doing it from a page, on a customer whose numbers did not move,
is a cost nobody decided on — and the page gives them no reason not to, because
pressing the button again is free to them.

So there is a limit, it is a setting rather than a constant, and it is per
customer rather than per franchisee: a franchisee with forty customers is not
the same spender as one with four, and a single number across the estate would
either stop the small one working or let the large one run up a bill.

**It counts the franchisee's own generations and not ours.** The limit is a
budget for what they do without asking; an Inputiv engineer drafting a review
on the same customer neither consumes it nor is stopped by it. A franchisee
blocked by work they did not do would be a limit they cannot reason about.

**It counts reviews that exist, not attempts.** Every successful generation
writes a `tbr_reports` row and those rows are superseded rather than deleted,
so the count is a read of what was actually produced. A generation that failed
before anything was stored does not count against them, which errs towards
letting somebody retry a broken run — the right direction, because the
alternative is a franchisee locked out by our outage.

The window is the **calendar month the generation happened in**, which is not
the review's own period: regenerating August's review four times in October is
four generations in October. Reviews are monthly, so a month is the unit the
franchisee already thinks in.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import OrganisationKind
from app.models.organisation import Organisation, User
from app.models.report import TbrReport

#: What a franchisee gets without anybody setting anything. Ten is more reviews
#: of one customer in one month than a monthly review needs, so a franchisee
#: working normally never meets it and a loop does so quickly.
DEFAULT_GENERATIONS_PER_SITE_PER_MONTH = 10


class SelfServiceConfig(BaseModel):
    """What a franchisee's own administrator may do without asking us."""

    generations_per_site_per_month: int = Field(
        default=DEFAULT_GENERATIONS_PER_SITE_PER_MONTH,
        ge=0,
        le=1000,
        description=(
            "How many reviews this franchisee's own people may generate of one customer "
            "in one calendar month. Generations by Inputiv staff are neither counted nor "
            "limited. Zero stops the franchisee generating at all, which is how a "
            "franchisee is put back to reading reviews we make for them."
        ),
    )


class GenerationLimitReached(Exception):
    """The franchisee has used this customer's allowance for the month.

    Carries the numbers because the message a person reads has to say what the
    limit was and when it lifts, not that something was "not permitted": the
    first is a fact they can act on and the second is a door with no handle.
    """

    def __init__(self, *, used: int, allowed: int, site_name: str, resets_on: dt.date) -> None:
        self.used = used
        self.allowed = allowed
        self.site_name = site_name
        self.resets_on = resets_on
        super().__init__(self.message)

    @property
    def message(self) -> str:
        if self.allowed == 0:
            return (
                f"Generating reviews is switched off for this account. "
                f"Ask Inputiv to generate {self.site_name}'s review."
            )
        # The day is formatted rather than `strftime`'d: the directive for a
        # day without a leading zero differs between platforms and this string
        # is read by a person on a page.
        when = f"{self.resets_on.day} {self.resets_on:%B}"
        return (
            f"You have generated {self.used} of {self.allowed} reviews for {self.site_name} "
            f"this month. The allowance starts again on {when}. "
            f"Ask Inputiv if you need another one before then."
        )


def month_bounds(now: dt.datetime) -> tuple[dt.datetime, dt.date]:
    """The first instant of `now`'s month, and the date the next one starts.

    Both in UTC, which is what `created_at` is stored in. A franchisee's own
    time zone would move the boundary by hours and the allowance by at most one
    generation on one day a month; two definitions of "this month" in one
    product is the worse trade.
    """
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = (start + dt.timedelta(days=32)).replace(day=1)
    return start, nxt.date()


async def _franchisee_actor_ids(session: AsyncSession, client_id: uuid.UUID) -> list[str]:
    """The user ids of the people at this franchisee, as `created_by` stores them.

    `TbrReport.created_by` is text, and for a signed-in user it is `str(user.id)`
    (`Scope.actor_id`). Rows written before sign-in existed carry an operator
    handle instead and so match nothing here, which is right: nobody's allowance
    should be spent by a row from before anybody had an account.
    """
    rows = await session.execute(
        select(User.id)
        .join(Organisation, Organisation.id == User.organisation_id)
        .where(
            Organisation.client_id == client_id,
            Organisation.kind == OrganisationKind.FRANCHISEE,
        )
    )
    return [str(user_id) for user_id in rows.scalars()]


async def generations_this_month(
    session: AsyncSession,
    *,
    client_id: uuid.UUID,
    site_id: uuid.UUID,
    now: dt.datetime | None = None,
) -> int:
    """How many reviews this franchisee's own people have generated of this customer.

    Superseded rows are counted, because superseding one is what regenerating
    does: a franchisee who regenerates nine times has generated nine times.
    """
    now = now or dt.datetime.now(dt.UTC)
    start, _ = month_bounds(now)
    actors = await _franchisee_actor_ids(session, client_id)
    if not actors:
        return 0
    total = await session.execute(
        select(func.count())
        .select_from(TbrReport)
        .where(
            TbrReport.client_id == client_id,
            TbrReport.site_id == site_id,
            TbrReport.created_at >= start,
            TbrReport.created_by.in_(actors),
        )
    )
    return int(total.scalar_one())


async def check_generation_allowed(
    session: AsyncSession,
    *,
    client_id: uuid.UUID,
    site_id: uuid.UUID,
    site_name: str,
    allowed: int,
    now: dt.datetime | None = None,
) -> int:
    """Raise `GenerationLimitReached` if this one would be over. Returns the count.

    Checked before the work rather than after it: the point is not to bill for
    the reads and the model calls and then refuse to show the result.
    """
    now = now or dt.datetime.now(dt.UTC)
    used = await generations_this_month(session, client_id=client_id, site_id=site_id, now=now)
    if used >= allowed:
        _, resets_on = month_bounds(now)
        raise GenerationLimitReached(
            used=used, allowed=allowed, site_name=site_name, resets_on=resets_on
        )
    return used
