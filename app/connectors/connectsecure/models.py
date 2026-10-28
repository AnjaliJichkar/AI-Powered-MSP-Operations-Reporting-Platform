"""Pydantic models for the ConnectSecure v4 responses we read. Not the whole API.

Field names are the live ones (pilot tenant, 2026-09-22; docs/api-notes/connectsecure.md).
Every field except an id is optional: the vendor declares nothing required and
several documented fields arrive null.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Severity = Literal["critical", "high", "medium", "low", "info"]

#: The order findings are ranked in, worst first.
SEVERITY_ORDER: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

#: Problem group ids the vendor documents (api-notes, "Problem group ids"). The
#: per-company and per-asset stats are keyed by them; 34 is CISA KEV.
PROBLEM_GROUP_SEVERITY: dict[str, Severity] = {
    "1": "critical",
    "2": "high",
    "3": "medium",
    "4": "low",
}
PROBLEM_GROUP_CISA_KEV = "34"

#: A Microsoft KB number inside a fix text ("KB5030219"). Live, no fix text on the
#: pilot carries one (0 of 300); kept for OS updates that might.
KB_PATTERN = re.compile(r"\bKB\s?(\d{6,7})\b", re.IGNORECASE)

#: The company risk score's letter grades, from the vendor's scoring standard
#: (api-notes): 0 to 100, **higher is worse**. Confirmed live: an asset graded F
#: scores 100 with a critical and a known-exploited finding; a B scores 50 with one.
RISK_GRADES: tuple[tuple[float, str], ...] = (
    (40.0, "A"),
    (45.0, "B"),
    (60.0, "C"),
    (75.0, "D"),
    (90.0, "E"),
    (100.0, "F"),
)

RemediationKind = Literal["patch", "uninstall", "script", "other"]

#: `remediation_action` values seen live -> what kind of work they are.
REMEDIATION_KINDS: dict[str, RemediationKind] = {
    "software patch": "patch",
    "update": "patch",
    "os update": "patch",
    "uninstall": "uninstall",
    "run remediation script": "script",
}


def risk_grade(score: float | None) -> str | None:
    """`48.0` -> `"C"`, per the vendor's bands. None when there is no score."""
    if score is None:
        return None
    for upper, grade in RISK_GRADES:
        if score <= upper:
            return grade
    return "F"


def _iso(value: Any) -> dt.datetime | None:
    """A vendor timestamp to an aware datetime; None for anything unreadable.

    Live: ISO-8601 with microseconds and no zone on assets and agents
    (`2026-09-21T02:10:00.123456`), with a `Z` on the vulnerability views. A
    naive value is read as UTC. An epoch number is accepted too.
    """
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if isinstance(value, int | float):
        return dt.datetime.fromtimestamp(value, tz=dt.UTC)
    text = str(value).strip()
    if text.isdigit():
        return dt.datetime.fromtimestamp(int(text), tz=dt.UTC)
    if "T" not in text:
        text = text.replace(" ", "T", 1)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).rstrip("%"))
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _first_of(value: Any) -> Any:
    """`[x]` -> `x`; a scalar stays; an empty list is None."""
    if isinstance(value, list):
        return value[0] if value else None
    return value


def normalise_severity(value: Any) -> Severity | None:
    """`"CRITICAL"`, `"Critical"`, `"critical"` and `9.8` -> `"critical"`. Unknown -> None."""
    if value is None or value == "":
        return None
    if isinstance(value, int | float) and not isinstance(value, bool):
        score = float(value)
        if score >= 9.0:
            return "critical"
        if score >= 7.0:
            return "high"
        if score >= 4.0:
            return "medium"
        if score > 0:
            return "low"
        return "info"
    text = str(value).strip().casefold()
    if text in SEVERITY_ORDER:
        return text  # type: ignore[return-value]
    if text in {"informational", "information", "none"}:
        return "info"
    return None


def kb_numbers(*texts: str | None) -> set[str]:
    """Every KB number named in the given texts, as `KB5030219`."""
    found: set[str] = set()
    for text in texts:
        if text:
            found.update(f"KB{m}" for m in KB_PATTERN.findall(text))
    return found


#: A parenthetical (`(x64 en-US)`, `(R)`), a bare architecture token, a version
#: number, and anything that is not a letter, digit or space.
_PRODUCT_PARENS = re.compile(r"\([^)]*\)")
_PRODUCT_NOISE = re.compile(
    r"\b(?:x64|x86|64-bit|32-bit|64bit|32bit)\b|\b\d+(?:\.\d+)+\b|[^a-z0-9 ]",
    re.IGNORECASE,
)


def normalise_product(name: str | None) -> str:
    """`"Mozilla Firefox (x64 en-US) 156.0"` and `"mozilla firefox"` meet here.

    Lower case; any parenthetical (architecture, locale, edition), bare
    architecture tokens, version numbers and punctuation removed; whitespace
    collapsed. Nothing fuzzier: two products that differ in a word stay
    different, so "Google Chrome" never matches "Google Chrome Beta".
    """
    if not name:
        return ""
    cleaned = _PRODUCT_NOISE.sub(" ", _PRODUCT_PARENS.sub(" ", name.casefold()))
    return " ".join(cleaned.split())


_VERSION = re.compile(r"\d+(?:\.\d+)*")


def version_key(text: str | None) -> tuple[int, ...] | None:
    """The first dotted number in a text as a comparable tuple: `"156.0"` -> `(156, 0)`."""
    if not text:
        return None
    match = _VERSION.search(text)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(0).split("."))


def version_at_least(candidate: str | None, target: str | None) -> bool:
    """True when `candidate`'s version is at or above `target`'s, compared part by part."""
    a, b = version_key(candidate), version_key(target)
    if a is None or b is None:
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) >= b + (0,) * (width - len(b))


class CsCompany(BaseModel):
    """`/r/company/companies` row. `id` is an integer in ConnectSecure; kept as text."""

    model_config = ConfigDict(extra="ignore")

    id: str
    name: str | None = None
    is_deleted: bool | None = None
    is_assessment: bool | None = Field(
        default=None, description="An assessment (prospect) company, not a managed customer."
    )
    created: dt.datetime | None = None
    updated: dt.datetime | None = None

    _times = field_validator("created", "updated", mode="before")(_iso)
    _id = field_validator("id", mode="before")(_text)


class CsAgent(BaseModel):
    """`/r/company/agents` row: the documented shape, plus the live flags."""

    model_config = ConfigDict(extra="ignore")

    id: str
    host_name: str | None = None
    ip: str | None = None
    os_type: str | None = None
    os_platform: str | None = None
    os_name: str | None = None
    os_version: str | None = None
    agent_type: str | None = None
    agent_version: str | None = None
    last_scanned_time: dt.datetime | None = None
    last_reported: dt.datetime | None = None
    last_ping_time: dt.datetime | None = None
    is_deprecated: bool | None = None
    is_retired: bool | None = None
    agent_deleted: bool | None = None
    reported: bool | None = None
    company_id: str | None = None

    _times = field_validator("last_scanned_time", "last_reported", "last_ping_time", mode="before")(
        _iso
    )
    _ids = field_validator("id", "company_id", "os_type", mode="before")(_text)

    @property
    def hostname(self) -> str | None:
        return self.host_name

    @property
    def is_live(self) -> bool:
        """An agent that still counts: not deprecated, retired or deleted, and reporting."""
        return (
            not self.is_deprecated
            and not self.is_retired
            and not self.agent_deleted
            and self.reported is not False
        )


class CsAsset(BaseModel):
    """`/r/asset/asset_view` row: one scanned machine, with the vendor's own counts.

    The row carries no scan time; `last_scanned` is set by the client from the
    agent the row names by `agent_id` (live: never null). The severity counts
    are the vendor's confirmed-problem counts for the asset; `cisa_vul` is how
    many of its findings are on CISA's Known Exploited Vulnerabilities list.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    host_name: str | None = None
    name: str | None = None
    """The vendor's display name, which can differ from `host_name`. Read by the
    coverage diagnostic only; the join matches on `host_name`."""
    ip: str | None = None
    mac: str | None = None
    serial_number: str | None = None
    domain: str | None = None
    os_name: str | None = None
    os_version: str | None = None
    platform: str | None = None
    asset_type: str | None = None
    importance: int | None = None
    agent_id: str | None = None
    agent_type: str | None = None
    agent_version: str | None = None
    company_id: str | None = None
    is_deprecated: bool | None = None
    online_status: bool | None = None
    security_grade: str | None = Field(default=None, description="A (best) to F (worst).")
    max_risk_score: float | None = Field(default=None, description="0 to 100, higher is worse.")
    avg_risk_score: float | None = None
    critical: int | None = None
    high: int | None = None
    medium: int | None = None
    low: int | None = None
    total_vul: int | None = None
    cisa_vul: int | None = Field(default=None, description="Findings on CISA's KEV list.")
    epss_vul: int | None = None
    discovered: dt.datetime | None = None
    last_discovered_time: dt.datetime | None = None
    last_ping_time: dt.datetime | None = None
    last_scanned: dt.datetime | None = Field(
        default=None, description="From the agent named by agent_id; set by the client."
    )
    agent: CsAgent | None = Field(default=None, description="Joined by agent_id by the client.")

    _times = field_validator(
        "discovered", "last_discovered_time", "last_ping_time", "last_scanned", mode="before"
    )(_iso)
    _ids = field_validator("id", "agent_id", "company_id", "serial_number", mode="before")(_text)
    _ints = field_validator(
        "importance",
        "critical",
        "high",
        "medium",
        "low",
        "total_vul",
        "cisa_vul",
        "epss_vul",
        mode="before",
    )(_int)
    _floats = field_validator("max_risk_score", "avg_risk_score", mode="before")(_float)

    @property
    def risk_score(self) -> float | None:
        return self.max_risk_score

    @property
    def last_scan(self) -> dt.datetime | None:
        return self.last_scanned


class CsVulnerability(BaseModel):
    """One vulnerability on one asset (`/r/report_queries/asset_wise_vulnerabilities`).

    Live shape: `problem_name` (the CVE), `severity`, `base_score`, `epss_score`,
    `software_name` (a one-element list), `asset_ids` and `company_ids` (one
    element each), `host_name`, `ip`, `discovered`, `last_discovered_time`. The
    row carries **no fix and no known-exploited flag**: the fix is joined from
    the remediation rows by asset id and normalised product, and the flag is
    set from the problem-group-34 read, both by the client.
    """

    model_config = ConfigDict(extra="ignore")

    problem_id: str | None = None
    problem: str = Field(description="The CVE id or check name.")
    title: str | None = None
    severity: Severity | None = None
    base_score: float | None = None
    epss_score: float | None = None
    exploitability_score: float | None = None
    impact_score: float | None = None
    product: str | None = Field(default=None, description="The affected software.")
    asset_id: str | None = None
    company_id: str | None = None
    host_name: str | None = None
    ip: str | None = None
    port: int | None = None
    first_discovered: dt.datetime | None = None
    last_discovered: dt.datetime | None = None
    known_exploited: bool | None = Field(
        default=None, description="On CISA's KEV list; set by the client from the group-34 read."
    )
    fix: str | None = Field(
        default=None, description="Joined from the remediation rows; None is 'fix not reported'."
    )
    fix_kind: RemediationKind | None = None

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        row = dict(data)
        row["problem"] = row.get("problem_name") or row.get("problem") or ""
        row["title"] = row.get("description") or row.get("title")
        row["severity"] = normalise_severity(row.get("severity"))
        row["product"] = _first_of(row.get("software_name")) or row.get("product")
        row["asset_id"] = (
            _first_of(row.get("asset_ids")) if "asset_ids" in row else row.get("asset_id")
        )
        row["company_id"] = (
            _first_of(row.get("company_ids")) if "company_ids" in row else row.get("company_id")
        )
        row["first_discovered"] = row.get("discovered") or row.get("first_discovered")
        row["last_discovered"] = row.get("last_discovered_time") or row.get("last_discovered")
        return row

    _times = field_validator("first_discovered", "last_discovered", mode="before")(_iso)
    _ids = field_validator("problem_id", "asset_id", "company_id", mode="before")(_text)
    _floats = field_validator(
        "base_score", "epss_score", "exploitability_score", "impact_score", mode="before"
    )(_float)
    _port = field_validator("port", mode="before")(_int)

    @property
    def product_key(self) -> str:
        return normalise_product(self.product)

    @property
    def rank(self) -> int:
        """Worst first: known exploited, then by severity."""
        if self.known_exploited:
            return -1
        return SEVERITY_ORDER.get(self.severity or "info", 9)


def _script_text(value: Any) -> str | None:
    """A remediation script as one text: the string the vendor used to send, or
    its newer list of per-OS scripts folded into lines. None for nothing."""
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        lines: list[str] = []
        for item in value:
            if isinstance(item, dict):
                script = item.get("script") or item.get("fix_script") or ""
                os_type = item.get("os_type")
                lines.append(f"[{os_type}] {script}" if os_type else str(script))
            else:
                lines.append(str(item))
        return chr(10).join(line for line in lines if line.strip()) or None
    return str(value)


class CsRemediation(BaseModel):
    """One fix on one asset (`/r/report_queries/get_remediation`).

    Live shape: `solution_id`, `asset_id`, `company_id`, `host_name`, `product`,
    `fix` (**the target version**, e.g. `156.0`, never a KB on the pilot),
    `remediation_action` (Software Patch, update, OS Update, Uninstall, Run
    remediation script), `severity`, the `*_vuls_count` fields, `url`,
    `first_vul_discovered`, `last_vul_discovered`, `fix_script`. One solution
    appears once per asset it applies to. `fix_script` was a string; since
    late September 2026 the vendor sends a list of `{os_type, script}` objects
    for some solutions, which is folded into one text here, one script per
    line prefixed by its OS. Nothing reads it yet; it must not break the read.
    """

    model_config = ConfigDict(extra="ignore")

    solution_id: str | None = None
    asset_id: str | None = None
    agent_id: str | None = None
    company_id: str | None = None
    host_name: str | None = None
    product: str | None = None
    fix: str = Field(description="The target version, or what the vendor says to do.")
    remediation_action: str | None = None
    os_type: str | None = None
    severity: Severity | None = None
    total_vulnerabilities: int | None = None
    critical: int | None = None
    high: int | None = None
    medium: int | None = None
    low: int | None = None
    epss_vulnerabilities: float | None = None
    url: str | None = None
    fix_script: str | None = None
    first_discovered: dt.datetime | None = None
    last_discovered: dt.datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        row = dict(data)
        row["fix"] = str(row.get("fix") or "").strip()
        row["fix_script"] = _script_text(row.get("fix_script"))
        row["severity"] = normalise_severity(row.get("severity"))
        row["total_vulnerabilities"] = row.get("total_vuls_count", row.get("total_vulnerabilities"))
        for level in ("critical", "high", "medium", "low"):
            row[level] = row.get(f"{level}_vuls_count", row.get(level))
        row["epss_vulnerabilities"] = row.get("epss_vuls", row.get("epss_vulnerabilities"))
        row["first_discovered"] = row.get("first_vul_discovered") or row.get("first_discovered")
        row["last_discovered"] = row.get("last_vul_discovered") or row.get("last_discovered")
        return row

    _times = field_validator("first_discovered", "last_discovered", mode="before")(_iso)
    _ids = field_validator("solution_id", "asset_id", "agent_id", "company_id", mode="before")(
        _text
    )
    _ints = field_validator(
        "total_vulnerabilities", "critical", "high", "medium", "low", mode="before"
    )(_int)
    _floats = field_validator("epss_vulnerabilities", mode="before")(_float)

    @property
    def kind(self) -> RemediationKind:
        return REMEDIATION_KINDS.get((self.remediation_action or "").strip().casefold(), "other")

    @property
    def product_key(self) -> str:
        return normalise_product(self.product)

    @property
    def kbs(self) -> set[str]:
        return kb_numbers(self.fix, self.fix_script)

    @property
    def instruction(self) -> str:
        """What to do, in words: `Update Mozilla Firefox to 156.0`, `Uninstall Zoom`."""
        product = self.product or "the software"
        if self.kind == "uninstall":
            return f"Uninstall {product}"
        if self.kind == "script":
            return f"Run the vendor's remediation script for {product}"
        if self.fix and version_key(self.fix) is not None:
            return f"Update {product} to {self.fix}"
        return f"{self.remediation_action or 'Remediate'} {product}: {self.fix}".strip(": ")


class CsCompanyStats(BaseModel):
    """`/r/company/company_stats` row: one company's totals, computed by the vendor.

    Live shape: `total_assets`, `asset_problem_stats` (counts keyed by problem
    group id: "1" critical, "2" high, "3" medium, "4" low, "34" CISA KEV, ...),
    `asset_vul_stats {avg_risk_score, max_risk_score}`. The company risk score
    is `avg_risk_score` (the mean of the assets' maximum scores, which is how
    the scoring standard defines it).
    """

    model_config = ConfigDict(extra="ignore")

    company_id: str | None = None
    total_assets: int | None = None
    problem_counts: dict[str, int] = Field(default_factory=dict)
    risk_score: float | None = None
    max_risk_score: float | None = None
    date: dt.datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        row = dict(data)
        stats = row.get("asset_problem_stats")
        row["problem_counts"] = (
            {str(k): _int(v) or 0 for k, v in stats.items()} if isinstance(stats, dict) else {}
        )
        vul = row.get("asset_vul_stats")
        if isinstance(vul, dict):
            row["risk_score"] = vul.get("avg_risk_score")
            row["max_risk_score"] = vul.get("max_risk_score")
        return row

    _date = field_validator("date", mode="before")(_iso)
    _ids = field_validator("company_id", mode="before")(_text)
    _ints = field_validator("total_assets", mode="before")(_int)
    _floats = field_validator("risk_score", "max_risk_score", mode="before")(_float)

    def count(self, severity: Severity) -> int:
        group = next(g for g, s in PROBLEM_GROUP_SEVERITY.items() if s == severity)
        return self.problem_counts.get(group, 0)

    @property
    def known_exploited(self) -> int:
        return self.problem_counts.get(PROBLEM_GROUP_CISA_KEV, 0)

    @property
    def total(self) -> int:
        return sum(self.count(s) for s in ("critical", "high", "medium", "low"))


class CsUser(BaseModel):
    """`/r/user/get_users` row: enough to see what the API key's user may do."""

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    email: str | None = None
    roles: list[str] = Field(default_factory=list)
    state: str | None = None

    @field_validator("roles", mode="before")
    @classmethod
    def _roles(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [r.strip().upper() for r in value.split(",") if r.strip()]
        return [str(r).strip().upper() for r in value if str(r).strip()]

    _ids = field_validator("id", "state", mode="before")(_text)

    @property
    def role_set(self) -> set[str]:
        return set(self.roles)


#: Roles that can only read (api-notes, "Roles"). Anything else on the API key's
#: user is a write surface the connector's allowlist guards against in code only.
READ_ONLY_ROLES: frozenset[str] = frozenset(
    {
        "ASSETVIEWER",
        "VULNERABILITYVIEWER",
        "COMPLIANCEVIEWER",
        "ACTIVEDIRECTORYVIEWER",
        "PIIVIEWER",
        "APPROVER",
    }
)
