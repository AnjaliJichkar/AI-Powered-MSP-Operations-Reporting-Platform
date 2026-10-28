"""Do the join and the links say the same thing? Computed daily, kept, shown.

The identity layer is the coverage join written down, so the board read
through the links must give the counts the inline join gives, site by site.
`compare` puts the two reports side by side and lists every count that
differs; `run_agreement` computes both boards after the day's resolution and
stores the answer as a `device_agreements` row, which the Devices page shows
Inputiv staff. A difference is recorded and shown, never reconciled here:
the two paths are meant to be one join, and a gap between them is a finding
for a person (`docs/validation-log.md` has the first ones).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.coverage.compute import CoverageReport, compute_coverage
from app.coverage.core import Exclusion
from app.models.device import DeviceAgreement

SITE_FIELDS = ("managed", "rmm_stale", "unmanaged", "discovered")


def compare(off: CoverageReport, on: CoverageReport) -> list[dict[str, Any]]:
    """Every per-site count that differs between the join and the links.

    One entry per (site, field): the site's own four counts, then every
    tool's state counts. A site present on one side only is one entry.
    """
    left = {s.slug: s for s in off.sites}
    right = {s.slug: s for s in on.sites}
    differences: list[dict[str, Any]] = []
    for slug in sorted(set(left) | set(right)):
        a, b = left.get(slug), right.get(slug)
        if a is None or b is None:
            present = a or b
            assert present is not None
            differences.append(
                {
                    "site_slug": slug,
                    "site": present.name,
                    "field": "site",
                    "off": "present" if a else "missing",
                    "on": "present" if b else "missing",
                }
            )
            continue
        for field in SITE_FIELDS:
            if getattr(a, field) != getattr(b, field):
                differences.append(
                    {
                        "site_slug": slug,
                        "site": a.name,
                        "field": field,
                        "off": getattr(a, field),
                        "on": getattr(b, field),
                    }
                )
        for tool, counts in a.tools.items():
            other = b.tools.get(tool)
            for state, value in counts.as_dict().items():
                theirs = other.as_dict().get(state) if other is not None else None
                if value != theirs:
                    differences.append(
                        {
                            "site_slug": slug,
                            "site": a.name,
                            "field": f"{tool}.{state}",
                            "off": value,
                            "on": theirs,
                        }
                    )
    return differences


def summary_of(off: CoverageReport, on: CoverageReport) -> dict[str, Any]:
    """The fleet totals both ways, for the one-line verdict on the page."""
    return {
        "sites": len(off.sites),
        "managed": [len(off.devices), len(on.devices)],
        "unmanaged": [len(off.unmanaged), len(on.unmanaged)],
        "discovered": [len(off.discovered), len(on.discovered)],
        "covered_percent": {
            key: [off.totals()[key].covered_percent, on.totals()[key].covered_percent]
            for key in off.totals()
        },
    }


async def run_agreement(
    session: AsyncSession,
    client_id: uuid.UUID,
    *,
    exclusions: list[Exclusion],
    actor: str,
    now: dt.datetime | None = None,
) -> DeviceAgreement:
    """Compute the board both ways and keep the comparison. The caller commits."""
    now = now or dt.datetime.now(dt.UTC)
    off = await compute_coverage(client_id, exclusions=exclusions, now=now, through_identity=False)
    on = await compute_coverage(client_id, exclusions=exclusions, now=now, through_identity=True)
    differences = compare(off, on)
    row = DeviceAgreement(
        client_id=client_id,
        computed_at=now,
        agree=not differences,
        sites_compared=len({s.slug for s in off.sites} | {s.slug for s in on.sites}),
        summary=summary_of(off, on),
        differences=differences,
        actor=actor,
    )
    session.add(row)
    await session.flush()
    return row


async def latest_agreement(session: AsyncSession, client_id: uuid.UUID) -> DeviceAgreement | None:
    return (
        await session.execute(
            select(DeviceAgreement)
            .where(DeviceAgreement.client_id == client_id)
            .order_by(DeviceAgreement.computed_at.desc(), DeviceAgreement.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def agreement_view(row: DeviceAgreement | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "computed_at": row.computed_at,
        "agree": row.agree,
        "sites_compared": row.sites_compared,
        "summary": row.summary,
        "differences": row.differences,
        "sites_differing": sorted({d["site_slug"] for d in row.differences}),
    }
