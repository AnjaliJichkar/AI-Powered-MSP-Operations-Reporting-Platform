"""Demo decks: a real review with every real name replaced, and every number kept.

A demo render exists to show a prospect what a review looks like without
showing them a customer. The figures are what make it convincing, so they are
untouched; what identifies the customer is replaced:

- the **site** (the end customer), its slug and its initials where they are used
  as an abbreviation (device names and project names often start with them);
- the **client** (the franchisee), its display name and any named presenter;
- every **device**, **contact** and **logged-in user** the collectors saw, used
  on a slide or not (`FactSet.names`), plus any in the tables and charts of older
  documents;
- every **engineer** who generated, edited, confirmed or approved anything.

**Consistency.** One mapping is built per document, before anything is
replaced, from the sorted set of real names. The same real name therefore maps
to the same pseudonym on every slide, in every chart and in the notes, and the
mapping is returned so the render gate can check that no real name survived.

**Matching.** Device and contact names are matched as whole tokens and
case-sensitively: a machine called `RECEPTION` must not rewrite the word
"reception" in a sentence. Site and client names are matched without regard
to case. Longer names are replaced first, so "Acme Legal Group" is not left as
"Sample Client Co Group".

Nothing here touches the stored review. `pseudonymise` works on a copy.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.reports.tbr.model import ReviewDocument

SITE_NAME = "Sample Client Co"
SITE_SLUG = "sample-client"
CLIENT_NAME = "Sample IT Provider"
STAMP = "Sample data"

#: Labels the collectors write into charts and tables that are not people.
NOT_A_PERSON = frozenset(
    {
        "Automation and system accounts",
        "Monitoring and automation",
        "Unnamed contact",
        "not reported",  # the lifecycle tables' value when NinjaOne holds no user
    }
)

#: Values under these keys are identifiers or code-written definitions, never names.
SKIP_KEYS = frozenset(
    {"id", "key", "slot", "source", "kind", "role", "status", "narrative", "definition",
     "fact_ids", "finding_ids", "service_key", "recommendation_type", "columns", "sources",
     "severity", "author", "url", "template", "franchisee"}
)  # fmt: skip


def _initials(name: str) -> str:
    words = [word for word in re.findall(r"[A-Za-z]+", name) if word[0].isupper()]
    return "".join(word[0] for word in words)


def _letters(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    text = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        text = chr(65 + remainder) + text
    return text


def contact_label(index: int) -> str:
    """0 -> Contact A. Shared with the client deck's anonymised contact chart."""
    return f"Contact {_letters(index)}"


@dataclass(slots=True)
class Pseudonyms:
    """The mapping for one document, and the means to apply and to check it."""

    exact: dict[str, str] = field(default_factory=dict)
    """Case-sensitive, whole-token: devices, contacts, engineers, initials."""
    loose: dict[str, str] = field(default_factory=dict)
    """Case-insensitive, whole-token: the site and the client."""

    def _pattern(self, names: list[str], *, ignore_case: bool) -> re.Pattern[str] | None:
        wanted = sorted((n for n in names if n), key=len, reverse=True)
        if not wanted:
            return None
        body = "|".join(re.escape(name) for name in wanted)
        return re.compile(
            rf"(?<![A-Za-z0-9])(?:{body})(?![A-Za-z0-9])", re.IGNORECASE if ignore_case else 0
        )

    def apply(self, text: str) -> str:
        loose = self._pattern(list(self.loose), ignore_case=True)
        if loose is not None:
            lowered = {real.casefold(): fake for real, fake in self.loose.items()}
            text = loose.sub(lambda match: lowered[match.group(0).casefold()], text)
        exact = self._pattern(list(self.exact), ignore_case=False)
        if exact is not None:
            text = exact.sub(lambda match: self.exact[match.group(0)], text)
        return text

    def survivors(self, text: str) -> list[str]:
        """Real names still present in `text`. Empty when the deck is clean."""
        found: list[str] = []
        loose = self._pattern(list(self.loose), ignore_case=True)
        exact = self._pattern(list(self.exact), ignore_case=False)
        for pattern in (loose, exact):
            if pattern is not None:
                found.extend(match.group(0) for match in pattern.finditer(text))
        return found


@dataclass(slots=True)
class RealNames:
    """Every real name a review holds, by kind, each list sorted."""

    devices: list[str]
    contacts: list[str]
    engineers: list[str]
    users: list[str]
    locations: list[str]


def _real_names(document: ReviewDocument) -> RealNames:
    facts = document.facts
    devices = set(facts.names.get("devices", []))
    contacts = set(facts.names.get("contacts", []))
    users = set(facts.names.get("users", []))
    locations = set(facts.names.get("locations", []))
    for table in facts.tables.values():
        if table.columns and table.columns[0] == "Device":
            devices.update(row[0] for row in table.rows if row)
        if "Last logged-in user" in table.columns:
            column = table.columns.index("Last logged-in user")
            users.update(row[column] for row in table.rows if len(row) > column)
        if "Location" in table.columns:
            column = table.columns.index("Location")
            locations.update(row[column] for row in table.rows if len(row) > column)
    chart = facts.charts.get("autotask.chart.top_contacts")
    if chart is not None:
        contacts.update(name for name in chart.categories if name not in NOT_A_PERSON)

    engineers = {document.generated_by}
    franchisee = document.franchisee
    presenter = franchisee.get("presenter") if isinstance(franchisee, dict) else None
    if isinstance(presenter, str) and presenter:
        engineers.add(presenter)
    for finding in document.findings:
        if finding.confirmed_by:
            engineers.add(finding.confirmed_by)
    for section in document.sections:
        engineers.update(name for name in (section.approved_by, section.talk_track_by) if name)
        for item in section.all_items():
            engineers.update(
                cite.removeprefix("engineer:")
                for cite in item.cites
                if cite.startswith("engineer:")
            )
    return RealNames(
        devices=sorted(name for name in devices if name),
        contacts=sorted(name for name in contacts if name),
        engineers=sorted(name for name in engineers if name),
        users=sorted(name for name in users if name and name not in NOT_A_PERSON),
        locations=sorted(name for name in locations if name and name not in NOT_A_PERSON),
    )


def build_pseudonyms(document: ReviewDocument) -> Pseudonyms:
    real = _real_names(document)
    names = Pseudonyms()
    width = max(3, len(str(len(real.devices))))
    for index, name in enumerate(real.devices, start=1):
        names.exact[name] = f"DEVICE-{index:0{width}d}"
    for index, name in enumerate(real.contacts):
        names.exact[name] = contact_label(index)
    for index, name in enumerate(real.users):
        # A login (`DOMAIN\jsmith`, `jsmith@example.com`) is a person's name too.
        names.exact.setdefault(name, f"User {_letters(index)}")
    for index, name in enumerate(real.engineers):
        names.exact[name] = f"Engineer {_letters(index)}"
    # A NinjaOne location is free text the franchisee typed ("Acme Legal - Houston",
    # "Dana's office"): the whole label is replaced, case-insensitively, and before
    # the site name is, so a label that embeds the site name maps to one pseudonym
    # rather than to the site's pseudonym with the rest of the label left standing.
    for index, name in enumerate(real.locations):
        names.loose.setdefault(name, f"Location {_letters(index)}")

    names.loose[document.site_name] = SITE_NAME
    names.loose[document.site_slug] = SITE_SLUG
    names.loose[document.client_name] = CLIENT_NAME
    names.loose[document.client_slug] = "sample-it-provider"
    display = document.franchisee.get("display_name")
    if isinstance(display, str) and display:
        names.loose[display] = CLIENT_NAME
    # "SKG Network Refresh": the customer's initials used as an abbreviation.
    initials = _initials(document.site_name)
    if len(initials) >= 3:
        names.exact.setdefault(initials, _initials(SITE_NAME))
    return names


#: A name shorter than this is not checked as a substring: two letters occur in
#: ordinary words too often to mean anything. Such a name is still replaced by
#: the whole-token mapping above.
MIN_TERM = 3


def real_name_terms(document: ReviewDocument) -> list[str]:
    """Every real name the review holds, for the demo gate's substring check: the
    site and client (names and slugs, the franchisee's display name), every contact,
    device, logged-in user and location the facts name. Longest first."""
    real = _real_names(document)
    terms = {
        document.site_name,
        document.site_slug,
        document.client_name,
        document.client_slug,
        *real.devices,
        *real.contacts,
        *real.users,
        *real.locations,
    }
    display = document.franchisee.get("display_name")
    if isinstance(display, str):
        terms.add(display)
    return sorted(
        {term.strip() for term in terms if term and len(term.strip()) >= MIN_TERM},
        key=lambda t: (-len(t), t),
    )


def substring_survivors(text: str, terms: list[str]) -> list[str]:
    """The terms present anywhere in `text` as a substring, case-insensitive.

    Stricter than `Pseudonyms.survivors`, which matches whole tokens: a real name
    inside a longer label the mapping never saw (a location, a free-text field a
    vendor returns) is caught here. This is the gate's check, never the
    replacement, so a device called `RECEPTION` still does not rewrite the word."""
    lowered = text.casefold()
    return [term for term in terms if term.casefold() in lowered]


def real_name_checker(names: Pseudonyms, terms: list[str]) -> Callable[[str], list[str]]:
    """The demo gate's check on one piece of text: the mapping's whole-token
    survivors and every term found as a substring, together and deduplicated."""

    def found(text: str) -> list[str]:
        return sorted(set(names.survivors(text)) | set(substring_survivors(text, terms)))

    return found


def _walk(value: Any, names: Pseudonyms, key: str = "") -> Any:
    if isinstance(value, dict):
        return {k: (v if k in SKIP_KEYS else _walk(v, names, k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_walk(item, names, key) for item in value]
    if isinstance(value, str):
        if key == "cites" and value.startswith("engineer:"):
            return "engineer:" + names.apply(value.removeprefix("engineer:"))
        return value if key == "cites" else names.apply(value)
    return value


def pseudonymise(document: ReviewDocument) -> tuple[ReviewDocument, Pseudonyms]:
    """A copy of the review with every real name replaced, and the mapping used."""
    names = build_pseudonyms(document)
    data = _walk(document.model_dump(mode="json"), names)
    # The franchisee block is skipped by the walk (it holds keys and prices); only
    # the two names in it are replaced.
    franchisee = dict(document.franchisee)
    franchisee["display_name"] = CLIENT_NAME
    if franchisee.get("presenter"):
        franchisee["presenter"] = names.apply(str(franchisee["presenter"]))
    franchisee["logo_path"] = None  # the franchisee's logo is a name too
    data["franchisee"] = franchisee
    for section in data["sections"]:
        if section["key"] == "title":
            section["data"]["logo"] = ""
    # `names` holds the real names themselves: the copy must not carry them.
    data["facts"]["names"] = {}
    for prior in ("prior_facts",):
        if data.get(prior):
            data[prior]["names"] = {}
    return ReviewDocument.model_validate(data), names
