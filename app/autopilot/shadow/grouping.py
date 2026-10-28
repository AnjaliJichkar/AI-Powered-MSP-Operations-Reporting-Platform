"""Which earlier ticket this one is the same thing as.

Deterministic and backwards-only. Two reasons it is not a model call:

* **A replay must not see the future.** Grouping is the one prediction with a
  direction: a ticket may join an earlier ticket and never a later one, or the
  measurement is of hindsight rather than of triage. A function walking tickets
  in creation order with a window that only looks back cannot cheat; a prompt
  holding the day's tickets could.
* **It is cheap and reproducible.** The same tickets give the same groups on
  every run, so when a number moves it moved because the threshold or the
  window moved, both of which are on the run's config.

The score is a similarity on the *normalised* subject — lower-cased, stop words
and digits dropped — which is what makes "Disk space low on SRV-01" and "Disk
space low on SRV-04" the same thing and "printer jam" and "printer offline"
different ones. The threshold and the window are settings, because this is the
knob that decides whether a franchisee sees one ticket or eleven.

**Only within one site.** Two customers with the same monitoring tool raise the
same subject line all week, and they are not one incident. This is the same
rule the coverage join follows for hostnames, for the same reason.
"""

from __future__ import annotations

import datetime as dt
import difflib
import re
from dataclasses import dataclass

#: Words that carry no signal in a subject line and would lift every score.
STOP_WORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "for",
        "to",
        "and",
        "in",
        "on",
        "of",
        "is",
        "not",
        "with",
        "at",
        "from",
        "please",
        "can",
        "cannot",
        "unable",
        "issue",
        "issues",
        "help",
        "new",
        "user",
        "users",
        "re",
        "fw",
        "fwd",
        "request",
        "need",
        "needs",
        "has",
        "have",
        "been",
        "it",
        "we",
        "our",
        "my",
        "this",
        "that",
        "are",
        "was",
        "will",
        "would",
        "about",
    }
)
#: A token has to be this long to count as shared evidence. Short tokens match
#: too easily and would group a site's whole week together.
MIN_TOKEN = 4


@dataclass(frozen=True, slots=True)
class Grouped:
    """The earlier ticket this one joins, and why."""

    ticket_id: str
    basis: str
    score: float


@dataclass(frozen=True, slots=True)
class Candidate:
    """A ticket already seen, as grouping remembers it. No title text leaves
    this module: what is stored is the id, the basis and the score."""

    ticket_id: str
    created_at: dt.datetime
    normalised: str
    tokens: frozenset[str]


def normalise(title: str) -> tuple[str, frozenset[str]]:
    """A subject line reduced to the words that carry its meaning.

    Digits go: a monitoring alert's subject differs from the next one only by a
    hostname's number or a percentage, and those are what make two instances of
    one problem look like two problems.
    """
    words = re.findall(r"[a-z]+", title.lower())
    kept = [word for word in words if word not in STOP_WORDS and len(word) > 2]
    return " ".join(kept), frozenset(word for word in kept if len(word) >= MIN_TOKEN)


class Grouper:
    """Walks tickets in creation order, grouping each with what came before.

    One instance per run. `seen` is per site, and each site's list is kept in
    creation order, so looking back is a walk from the end until the window
    closes.
    """

    __slots__ = ("_seen", "similarity", "window")

    def __init__(self, *, window: dt.timedelta, similarity: float) -> None:
        self.window = window
        self.similarity = similarity
        self._seen: dict[object, list[Candidate]] = {}

    def consider(
        self, *, ticket_id: str, site_id: object, created_at: dt.datetime, title: str
    ) -> Grouped | None:
        """Group this ticket with an earlier one at the same site, or open a
        group of its own. Call once per ticket, in creation order."""
        normalised, tokens = normalise(title)
        earlier = self._seen.setdefault(site_id, [])
        best: Grouped | None = None
        for candidate in reversed(earlier):
            if created_at - candidate.created_at > self.window:
                break  # the list is in creation order, so everything older is too old
            if not (tokens & candidate.tokens):
                continue  # no shared word of substance: not worth scoring
            score = difflib.SequenceMatcher(None, normalised, candidate.normalised).ratio()
            if score < self.similarity:
                continue
            if best is None or score > best.score:
                best = Grouped(
                    ticket_id=candidate.ticket_id,
                    basis="same_site_similar_title",
                    score=round(score, 3),
                )
            if score == 1.0:
                break  # an exact match after normalising; nothing will beat it
        earlier.append(
            Candidate(
                ticket_id=ticket_id,
                created_at=created_at,
                normalised=normalised,
                tokens=tokens,
            )
        )
        return best

    @property
    def sites_seen(self) -> int:
        return len(self._seen)
