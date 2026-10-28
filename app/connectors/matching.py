"""Pairing the same machine across two tools by hostname.

NinjaOne and GravityZone each know a device by their own id and spell its name
their own way: `TL-AUS-WS14`, `tl-aus-ws14.acme.local`, `TL-AUS-WS14.ACME.LOCAL`.
Within one site those are the same machine. The match is on the **normalised
hostname**: lower case, the domain suffix stripped at the first dot, and
whitespace trimmed. Nothing fuzzier: a near miss must show up as two unmatched
devices, not as a wrong pair.

Matching is always within a site. Two customers can both own a `RECEPTION`,
and a cross-site pair would attribute one customer's protection to another.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field


def normalise_hostname(name: str | None) -> str:
    """`TL-AUS-WS14.acme.local ` -> `tl-aus-ws14`. Empty for a missing name."""
    if not name:
        return ""
    return name.strip().lower().split(".", 1)[0]


@dataclass(slots=True)
class SiteMatch:
    """The pairing for one site. Names are the normalised hostnames."""

    site_slug: str
    matched: list[str] = field(default_factory=list)
    left_only: list[str] = field(default_factory=list)
    right_only: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "matched": len(self.matched),
            "left_only": len(self.left_only),
            "right_only": len(self.right_only),
        }


def match_by_hostname(
    left: Iterable[tuple[str, str | None]], right: Iterable[tuple[str, str | None]]
) -> dict[str, SiteMatch]:
    """Pair `(site_slug, hostname)` rows from two tools, site by site.

    Returns one `SiteMatch` per site that either side mentions. A device whose
    name is empty cannot be matched and is counted on its own side as unmatched
    under the name `(unnamed)`; duplicates within one side collapse to one.
    """
    by_site: dict[str, tuple[set[str], set[str]]] = {}
    for slug, name in left:
        by_site.setdefault(slug, (set(), set()))[0].add(normalise_hostname(name) or "(unnamed)")
    for slug, name in right:
        by_site.setdefault(slug, (set(), set()))[1].add(normalise_hostname(name) or "(unnamed)")
    result: dict[str, SiteMatch] = {}
    for slug in sorted(by_site):
        lefts, rights = by_site[slug]
        unnamed_left = "(unnamed)" in lefts
        unnamed_right = "(unnamed)" in rights
        lefts.discard("(unnamed)")
        rights.discard("(unnamed)")
        match = SiteMatch(
            site_slug=slug,
            matched=sorted(lefts & rights),
            left_only=sorted(lefts - rights) + (["(unnamed)"] if unnamed_left else []),
            right_only=sorted(rights - lefts) + (["(unnamed)"] if unnamed_right else []),
        )
        result[slug] = match
    return result
