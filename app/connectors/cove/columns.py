"""Cove's column codes, and the few rules for reading a row back.

`EnumerateAccountStatistics` returns whatever columns you ask for, named by
code, in a list of single-key objects. Nothing here talks to the vendor: this
module is the vocabulary — which code means what, what the numbers in it
stand for, and how to fold a row into something with names on it.

**Three of the vendor's silences are handled here rather than at the call
site** (docs/api-notes/cove.md):

- an unrecognised or badly padded column code is ignored without an error,
  so every code we send comes from `COLUMNS` and is never typed at a call
  site. `D1F3` is not a worse `D01F03`; it is a value that quietly never
  arrives.
- values come back as strings even when they are numbers or Unix times, so
  reading one is a conversion that can fail, and a failure is `None` rather
  than a zero.
- a Microsoft 365 tenant is a row in the device list like any machine, with
  its OS type, physicality and version all undefined. `ACCOUNT_TYPE` is what
  tells the two apart, and every figure about machines has to say which it
  counted.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final

# --- the codes we ask for ---------------------------------------------------------

#: Device properties and installation details, by the name we use for them.
#: The vendor's legacy short name is in the comment where its own examples
#: use it, because its filter grammar is written in those.
COLUMNS: Final[Mapping[str, str]] = {
    "device_id": "I0",  # AU
    "device_name": "I1",  # AN
    "customer": "I8",  # AR
    "computer_name": "I18",  # MN
    "macs": "I21",  # MA
    "os_version": "I16",  # OS
    "os_type": "I32",  # OT
    "physicality": "I81",
    "used_storage": "I14",  # US
    "data_sources": "I78",  # AP
    "product": "I10",  # PN — the retention policy a report groups by
    "retention_units": "I39",  # RU — the retention scheme, coarser than the product
    "sku": "I57",  # KU — the billing article, not a retention policy
    "account_type": "I59",  # AT
    "recovery_testing": "I80",
    "created_at": "I4",  # CD
    "profile": "I56",  # OP
}

#: Statistics fields, combined with a data source as `D<source>F<field>`.
#: Both halves are zero-padded, and the vendor ignores a code that is not.
FIELDS: Final[Mapping[str, str]] = {
    "last_session_status": "F00",
    "last_session_errors": "F06",
    "protected_size": "F07",
    "colour_bar_28_days": "F08",  # B
    "last_successful_at": "F09",  # L
    "retention": "F14",  # R
    "last_session_at": "F15",  # G
    "user_mailboxes": "F20",  # M
    "shared_mailboxes": "F21",  # @
}

#: Data sources. `TOTAL` is the across-all-sources figure, which is what a
#: device's own status and last good backup come from.
TOTAL: Final = "D09"
FILES: Final = "D01"
SYSTEM_STATE: Final = "D02"
M365_SHAREPOINT: Final = "D05"
M365_EXCHANGE: Final = "D19"
M365_ONEDRIVE: Final = "D20"
M365_TEAMS: Final = "D23"

DATA_SOURCES: Final[Mapping[str, str]] = {
    FILES: "Files and folders",
    SYSTEM_STATE: "System state",
    "D03": "MsSql (deprecated)",
    "D04": "Exchange (VSS)",
    M365_SHAREPOINT: "Microsoft 365 SharePoint",
    "D06": "Network shares",
    "D07": "System state (VSS)",
    "D08": "VMware virtual machines",
    TOTAL: "Total",
    "D10": "MsSql (VSS)",
    "D11": "SharePoint (VSS)",
    "D12": "Oracle",
    "D14": "Hyper-V",
    "D15": "MySql",
    "D16": "Virtual disaster recovery",
    "D17": "Bare metal restore",
    M365_EXCHANGE: "Microsoft 365 Exchange",
    M365_ONEDRIVE: "Microsoft 365 OneDrive",
    M365_TEAMS: "Microsoft 365 Teams",
}


def stat(source: str, field: str) -> str:
    """One statistics column code, e.g. `stat(TOTAL, "last_successful_at")`.

    Built rather than typed, because a code the vendor does not recognise
    comes back as a missing value and never as an error.
    """
    try:
        return f"{source}{FIELDS[field]}"
    except KeyError:
        raise ValueError(
            f"{field!r} is not a Cove statistics field; the fields are: {', '.join(sorted(FIELDS))}"
        ) from None


# --- what the numbers in a row mean -----------------------------------------------

#: `I59`. A Microsoft 365 tenant is a device row like any other.
ACCOUNT_TYPE: Final[Mapping[int, str]] = {
    0: "unknown",
    1: "backup_manager",
    2: "microsoft365",
}
M365_ACCOUNT_TYPE: Final = 2

#: `I32`. Cove's own classification, which is undefined for a tenant.
OS_TYPE: Final[Mapping[int, str]] = {0: "undefined", 1: "workstation", 2: "server"}

#: `I80`.
RECOVERY_TESTING: Final[Mapping[int, str]] = {
    0: "none",
    1: "recovery_testing",
    2: "standby_image",
}

#: `F00`, and there are eleven of them. Anything that decides OK or failed
#: has to say where the middle five sit; this module reports the vendor's own
#: word and decides nothing.
SESSION_STATUS: Final[Mapping[int, str]] = {
    1: "in_process",
    2: "failed",
    3: "aborted",
    5: "completed",
    6: "interrupted",
    7: "not_started",
    8: "completed_with_errors",
    9: "in_progress_with_faults",
    10: "over_quota",
    11: "no_selection",
    12: "restarted",
}


# --- reading a row ----------------------------------------------------------------


def fold(settings: Sequence[Any] | None) -> dict[str, str]:
    """`[{"I14": "23630480"}, …]` into `{"I14": "23630480"}`.

    The vendor returns one object per column rather than one object of
    columns. A malformed entry is skipped rather than raising: the row's
    other values are still worth having, and a missing value is already how
    an unrecognised code comes back.
    """
    folded: dict[str, str] = {}
    for entry in settings or ():
        if not isinstance(entry, Mapping):
            continue
        for code, value in entry.items():
            if value is not None:
                folded[str(code)] = str(value)
    return folded


def as_int(value: str | None) -> int | None:
    """A column as a number, or `None` where it was absent or not one."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def as_time(value: str | None) -> dt.datetime | None:
    """A column as an instant. Cove sends Unix seconds, as a string.

    Zero is not a time: the vendor uses it for "never", and a backup dated
    1 January 1970 on a report is worse than one that says never.
    """
    seconds = as_int(value)
    if not seconds:
        return None
    try:
        return dt.datetime.fromtimestamp(seconds, tz=dt.UTC)
    except (OverflowError, OSError, ValueError):
        return None


def macs(value: str | None) -> list[str]:
    """`I21` into one MAC per entry.

    Live, 2026-10-19: every one of the pilot's 44 machines carries this, and
    a machine with several interfaces sends them comma-separated — up to
    eleven on one row. One string of eleven MACs matches nothing, so it is
    split before it is of any use.
    """
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def data_sources(value: str | None) -> list[str]:
    """`"D01D02"` into `["D01", "D02"]`, in the order the vendor sent them.

    Fixed three-character blocks; a trailing fragment is dropped rather than
    guessed at, and a code we do not know is kept, because the list of data
    sources grows and an unknown one is still a data source.
    """
    if not value:
        return []
    blocks = [value[at : at + 3] for at in range(0, len(value), 3)]
    return [block for block in blocks if len(block) == 3]


# --- Cove's own name for a machine ------------------------------------------------

#: Cove appends an underscore and five characters to a device name, so
#: `JC-LT-25` is `jc-lt-25_pqpsp` in the console. Stripping it is what lets a
#: Cove device be matched to the same machine in another tool.
_SUFFIX = re.compile(r"_[a-z0-9]{5}$")


def device_key(name: str | None) -> str:
    """The machine's name as other tools know it: lower case, suffix removed.

    Exactly five characters after the underscore, because that is the suffix
    the vendor adds. A machine genuinely named `backup_srv01` keeps its name;
    one named `report_x1a2b` would lose its tail, which is the price of the
    rule and is why matching on this key is never stated as a certainty.
    """
    lowered = (name or "").strip().casefold()
    return _SUFFIX.sub("", lowered)
