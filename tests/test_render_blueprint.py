"""The Blueprint is a deployment, so it is read here rather than trusted.

`render.yaml` is the only description of what runs on Render and when. A line
edited in it is a service created, a schedule changed or a secret dropped, and
none of that is visible from the application — so the few things that would be
silently wrong are asserted.

The daily collector gets most of the attention because it is the only thing
that recomputes a client's day. It is **parked** -- commented out by decision
on 20 October 2026 -- so the assertions are about the parked block rather than
a running service: that nothing is scheduled, and that what is written down to
be enabled one day is still right on the day somebody uncomments it. A
commented block nobody reads is how a Blueprint comes to carry a slug that was
correct for one franchisee two years ago.
"""

from __future__ import annotations

import pathlib
from typing import Any

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
BLUEPRINT = ROOT / "render.yaml"


def _services() -> list[dict[str, Any]]:
    loaded = yaml.safe_load(BLUEPRINT.read_text(encoding="utf-8"))
    return list(loaded["services"])


def _parked_collector() -> dict[str, Any]:
    """The commented collector block, uncommented and parsed.

    Read rather than skipped, because a parked block is still a decision
    somebody will act on: the next person to enable it types nothing but
    `git commit`, so what is written down has to be right on the day they do
    and not only on the day it was parked.
    """
    text = BLUEPRINT.read_text(encoding="utf-8")
    start = text.index("  # - type: cron")
    lines: list[str] = []
    for line in text[start:].splitlines():
        if not line.strip().startswith("#"):
            break
        lines.append(line.replace("  # ", "  ", 1))
    parsed = yaml.safe_load("\n".join(lines))
    assert isinstance(parsed, list) and len(parsed) == 1
    return dict(parsed[0])


def test_the_daily_collector_is_parked_and_nothing_is_scheduled() -> None:
    """Parked by decision, 20 October 2026: no cron runs on Render.

    Which means nothing recomputes a client's day on its own. The coverage
    board says how old it is for exactly that reason, and the runbook says the
    cron is parked rather than leaving somebody to infer it from a commented
    block they may never open.
    """
    assert not [s for s in _services() if s.get("type") == "cron"]
    assert "Parked by decision" in BLUEPRINT.read_text(encoding="utf-8")
    runbook = (ROOT / "docs" / "deploy.md").read_text(encoding="utf-8")
    assert "parked" in runbook.lower()


def test_the_parked_collector_is_still_right_when_somebody_enables_it() -> None:
    cron = _parked_collector()
    assert cron["type"] == "cron"
    # Daily, at a fixed hour. A schedule with a step or a list in the day field
    # is a cron that runs more than once a day, and the collector's writes are
    # one row per client per day: a second run replaces the first, which is
    # harmless and is still an hour of vendor reads nobody asked for.
    minute, hour, day, month, weekday = str(cron["schedule"]).split()
    assert (day, month, weekday) == ("*", "*", "*")
    assert minute.isdigit() and hour.isdigit()

    command = cron["startCommand"]
    assert "scripts/collect_events.py" in command
    # Every active client, not one named in the Blueprint. A slug here is a
    # second franchisee onboarded and never collected for, with nothing on any
    # screen to say so -- and a slug in a parked block is one nobody looks at
    # again until the day it runs.
    assert "--all-clients" in command
    assert "--client" not in command


def test_the_parked_collector_has_what_it_needs_and_nothing_it_does_not() -> None:
    env = _parked_collector()["envVars"]
    keys = {entry.get("key") for entry in env if "key" in entry}
    groups = {entry.get("fromGroup") for entry in env if "fromGroup" in entry}

    # The same config as the web service, so a setting cannot mean one thing on
    # a page and another on the schedule that fills it.
    assert groups == {"inputiv-engine-config"}
    # The database is wired from the instance rather than typed.
    database = next(entry for entry in env if entry.get("key") == "DATABASE_URL")
    assert database["fromDatabase"]["property"] == "connectionString"
    # The credential key, or it cannot decrypt a single stored credential and
    # every vendor read fails with what looks like a vendor's auth error.
    assert "CREDENTIAL_ENCRYPTION_KEYS" in keys
    # A secret is never in the Blueprint: both are prompted for on sync.
    for entry in env:
        if entry.get("key") in {"CREDENTIAL_ENCRYPTION_KEYS", "PUBLIC_BASE_URL"}:
            assert entry.get("sync") is False
            assert "value" not in entry
    # No model is called on this path, so no model key is attached to it.
    assert "ANTHROPIC_API_KEY" not in keys


def test_nothing_that_writes_to_a_vendor_is_scheduled() -> None:
    """The two live switches are off, and no uncommented service turns one on.

    `AUTOPILOT_SHADOW_LIVE` and `AUTOMATION_TRIGGERS_LIVE` are what let a cron
    read a vendor on a schedule and, for the second, post a note. Each belongs
    to its own service and neither is on today; a Blueprint that quietly
    carried one would make a scheduled vendor write a `git push` away.
    """
    for service in _services():
        for entry in service.get("envVars") or []:
            if entry.get("key") in {"AUTOPILOT_SHADOW_LIVE", "AUTOMATION_TRIGGERS_LIVE"}:
                assert str(entry.get("value")).lower() != "true", (
                    f"{service['name']} switches on {entry['key']}"
                )
