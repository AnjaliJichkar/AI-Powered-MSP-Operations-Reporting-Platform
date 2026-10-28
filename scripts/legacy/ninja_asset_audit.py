#!/usr/bin/env python3
"""
ninja_asset_audit.py  —  READ-ONLY asset audit

What it does
  1. Pulls the full NinjaOne device inventory and saves a dated snapshot (JSON).
  2. ADDED   = NinjaOne devices whose `created` timestamp falls in the audit month.
  3. REMOVED = devices present in the previous snapshot but not in the current one,
               plus any NinjaOne activity-log entries in the month that look like deletions.
  4. Pulls Bitdefender GravityZone endpoints and ConnectSecure agents.
  5. Compares by normalised hostname and writes CSVs:
       ninja_added_<month>.csv
       ninja_removed_<month>.csv
       orphans_bitdefender.csv     -> in Bitdefender, NOT in NinjaOne  (cleanup candidates)
       orphans_connectsecure.csv   -> in ConnectSecure, NOT in NinjaOne (cleanup candidates)
       missing_in_bitdefender.csv  -> in NinjaOne, NOT in Bitdefender  (coverage gaps)
       missing_in_connectsecure.csv
       ninja_activities_deletions_<month>.csv  (raw activity rows, for tuning the filter)

Nothing is modified on any platform. Only GET / read calls are made.

Usage
  pip install requests
  set the env vars below (or a .env loaded by your scheduler), then:
  python ninja_asset_audit.py                # audits the current month to date
  python ninja_asset_audit.py --month 2026-08

Env vars
  NINJA_BASE            e.g. https://app.ninjarmm.com  (eu.ninjarmm.com / oc.ninjarmm.com / ca.ninjarmm.com)
  NINJA_CLIENT_ID, NINJA_CLIENT_SECRET      (API client with scope "monitoring", client-credentials grant)
  BD_API_KEY            GravityZone API key (Network API + Companies API enabled)
  BD_BASE               default https://cloud.gravityzone.bitdefender.com
  CS_POD                e.g. pod106.myconnectsecure.com   (no https://)
  CS_TENANT, CS_CLIENT_ID, CS_CLIENT_SECRET
  AUDIT_DIR             where snapshots/reports go (default ./audit)
"""

import argparse
import base64
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

BILLABLE_CLASSES = {
    "WINDOWS_WORKSTATION",
    "WINDOWS_SERVER",
    "MAC",
    "LINUX_WORKSTATION",
    "LINUX_SERVER",
}
ACTIVE_DAYS = 14  # a device that checked in within this many days counts as active

# --------------------------------------------------------------------------- helpers


def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        sys.exit(f"Missing env var: {name}")
    return v


def norm_host(name):
    """Lower-case, strip domain suffix, strip whitespace. 'PC-01.corp.local' -> 'pc-01'."""
    if not name:
        return ""
    return name.strip().lower().split(".")[0][:15]


def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"  wrote {path}  ({len(rows)} rows)")


def epoch_to_iso(e):
    try:
        return datetime.fromtimestamp(float(e), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return ""


# --------------------------------------------------------------------------- NinjaOne


class Ninja:
    def __init__(self):
        self.base = env("NINJA_BASE", "https://app.ninjarmm.com").rstrip("/")
        r = requests.post(
            f"{self.base}/ws/oauth/token",
            data={
                "grant_type": "client_credentials",
                "client_id": env("NINJA_CLIENT_ID", required=True),
                "client_secret": env("NINJA_CLIENT_SECRET", required=True),
                "scope": "monitoring",
            },
            timeout=30,
        )
        r.raise_for_status()
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

    def get(self, path, **params):
        r = self.s.get(f"{self.base}/v2/{path.lstrip('/')}", params=params, timeout=60)
        r.raise_for_status()
        return r.json()

    def organizations(self):
        return {o["id"]: o["name"] for o in self.get("organizations", pageSize=1000)}

    def devices(self):
        """Cursor pagination on /v2/devices-detailed using `after=<last id>`."""
        out, after = [], 0
        while True:
            page = self.get("devices-detailed", pageSize=1000, after=after)
            if not page:
                break
            out.extend(page)
            after = page[-1]["id"]
            if len(page) < 1000:
                break
        return out

    def activities(self, start_epoch, end_epoch):
        """All activities in the window. Filtered for deletions later."""
        out, older = [], None
        while True:
            params = dict(
                pageSize=1000,
                after=int(start_epoch),
                before=int(end_epoch),
                **({"olderThan": older} if older else {}),
            )
            data = self.get("activities", **params)
            acts = data.get("activities", []) if isinstance(data, dict) else data
            if not acts:
                break
            out.extend(acts)
            older = acts[-1]["id"]
            if len(acts) < 1000:
                break
        return out


def ninja_device_row(d, orgs):
    sysinfo = d.get("system") or {}
    return {
        "ninja_id": d.get("id"),
        "hostname": d.get("systemName") or d.get("dnsName") or "",
        "host_key": norm_host(d.get("systemName") or d.get("dnsName")),
        "organization": orgs.get(d.get("organizationId"), d.get("organizationId")),
        "node_class": d.get("nodeClass"),
        "serial": sysinfo.get("serialNumber") or sysinfo.get("biosSerialNumber") or "",
        "manufacturer": sysinfo.get("manufacturer") or "",
        "model": sysinfo.get("model") or "",
        "created": epoch_to_iso(d.get("created")),
        "created_epoch": d.get("created"),
        "last_contact": epoch_to_iso(d.get("lastContact")),
        "last_contact_epoch": d.get("lastContact"),
        "offline": d.get("offline"),
    }


# --------------------------------------------------------------------------- Bitdefender GravityZone


class Bitdefender:
    """JSON-RPC. API key goes in HTTP Basic username, password empty."""

    def __init__(self):
        key = env("BD_API_KEY", required=True)
        self.base = (
            env("BD_BASE", "https://cloud.gravityzone.bitdefender.com").rstrip("/")
            + "/api/v1.0/jsonrpc"
        )
        self.s = requests.Session()
        self.s.headers["Authorization"] = "Basic " + base64.b64encode(f"{key}:".encode()).decode()
        self.s.headers["Content-Type"] = "application/json"
        self._companies = {}

    def call(self, api, method, params=None):
        body = {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": "audit"}
        r = self.s.post(f"{self.base}/{api}", json=body, timeout=60)
        r.raise_for_status()
        j = r.json()
        if "error" in j:
            raise RuntimeError(f"GravityZone {method}: {j['error']}")
        return j["result"]

    def company_name(self, cid):
        if not cid:
            return ""
        if cid not in self._companies:
            try:
                self._companies[cid] = self.call(
                    "companies", "getCompanyDetails", {"companyId": cid}
                ).get("name", cid)
            except Exception:
                self._companies[cid] = cid
        return self._companies[cid]

    def endpoints(self):
        # # 1. every container in the tree (companies, sub-companies, folders), skipping "Deleted"
        # containers, stack = [], [None]
        # while stack:
        #     parent = stack.pop()
        #     page = 1
        #     while True:
        #         params = {"page": page, "perPage": 100}
        #         if parent:
        #             params["parentId"] = parent
        #         res = self.call("network", "getNetworkInventoryItems", params)
        #         for it in res.get("items", []):
        #             if parent is None:
        #                 print("  ROOT ITEM:", it.get("type"), it.get("name"), it.get("id"), "details" in it)
        #             if not it.get("details") and (it.get("name") or "").strip().lower() != "deleted":
        #                 containers.append((it["id"], it.get("name", "")))
        #                 stack.append(it["id"])
        #         if page >= res.get("pagesCount", 1):
        #             break
        #         page += 1
        # 1. companies from the Companies API (no filter = all companies under this partner)
        try:
            comps = self.call("companies", "findCompaniesByName", {"nameFilter": ""})
        except RuntimeError:
            comps = []
        if not comps:
            # fallback: children of the root "Companies" node, whatever their shape
            root = self.call("network", "getNetworkInventoryItems", {"perPage": 100})
            node = next((i for i in root.get("items", []) if i.get("name") == "Companies"), None)
            comps, page = [], 1
            while node:
                res = self.call(
                    "network",
                    "getNetworkInventoryItems",
                    {"parentId": node["id"], "page": page, "perPage": 100},
                )
                comps.extend(res.get("items", []))
                if page >= res.get("pagesCount", 1):
                    break
                page += 1
        companies = [(c["id"], c.get("name", "")) for c in comps]

        # 2. getEndpointsList per container; folders reject parentId and are skipped
        seen, rows, ok = set(), [], 0
        for cid, cname in [(None, "(partner root)")] + companies:
            page = 1
            while True:
                params = {"page": page, "perPage": 100}
                if cid:
                    params["parentId"] = cid
                try:
                    res = self.call("network", "getEndpointsList", params)
                except RuntimeError as ex:
                    print(f"  skipped company {cname}: {ex}")
                    break
                if page == 1:
                    ok += 1
                for e in res.get("items", []):
                    if e["id"] in seen:
                        continue
                    seen.add(e["id"])
                    name = e.get("fqdn") or e.get("name") or ""
                    rows.append(
                        {
                            "bd_id": e["id"],
                            "hostname": name,
                            "host_key": norm_host(name),
                            "fqdn": e.get("fqdn", ""),
                            "label": e.get("label") or e.get("name", ""),
                            "company": cname,
                            "os": e.get("operatingSystemVersion", ""),
                            "ip": e.get("ip", ""),
                            "macs": ";".join(e.get("macs") or []),
                            "managed": e.get("isManaged"),
                            "first_seen": e.get("firstSeen", ""),
                        }
                    )
                if page >= res.get("pagesCount", 1):
                    break
                page += 1
        print(f"  {len(companies)} companies, {ok} accepted as endpoint parents")
        return rows

    def diag(self, company_name):
        comps = self.call("companies", "findCompaniesByName", {"nameFilter": company_name})
        print(json.dumps(comps, indent=1)[:1500])
        for c in comps:
            cid = c["id"]
            for method, params in (
                ("getNetworkInventoryItems", {"parentId": cid, "perPage": 5}),
                ("getEndpointsList", {"parentId": cid, "perPage": 5}),
            ):
                try:
                    r = self.call("network", method, params)
                    print(
                        f"\n{method} parentId={cid}: total={r.get('total')} items={len(r.get('items', []))}"
                    )
                    print(json.dumps(r.get("items", [])[:2], indent=1)[:2000])
                except RuntimeError as ex:
                    print(f"\n{method} parentId={cid}: ERROR {ex}")


# --------------------------------------------------------------------------- ConnectSecure (v4)


class ConnectSecure:
    """POST /w/authorize with Client-Auth-Token = base64(tenant+client_id:client_secret)."""

    def __init__(self):
        pod = env("CS_POD", required=True).replace("https://", "").rstrip("/")
        self.base = f"https://{pod}"
        raw = f"{env('CS_TENANT', required=True)}+{env('CS_CLIENT_ID', required=True)}:{env('CS_CLIENT_SECRET', required=True)}"
        tok = base64.b64encode(raw.encode("utf-8")).decode()
        r = requests.post(
            f"{self.base}/w/authorize", headers={"Client-Auth-Token": tok}, timeout=30
        )
        r.raise_for_status()
        auth = r.json()
        token = (auth.get("data") or {}).get("access_token") or auth.get("access_token")
        if not token or token.count(".") != 2:
            sys.exit(
                f"ConnectSecure: no usable access_token. Response was: {json.dumps(auth)[:500]}"
            )
        self.s = requests.Session()
        self.s.headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NinjaAssetAudit/1.0"
        )
        self.s.headers["Accept"] = "application/json"
        self.s.headers["Authorization"] = f"Bearer {token}"
        uid = (auth.get("data") or {}).get("user_id") or auth.get("user_id")
        if uid:
            self.s.headers["X-USER-ID"] = str(uid)
        self._companies = None

    def get(self, path, **params):
        r = self.s.get(f"{self.base}/{path.lstrip('/')}", params=params, timeout=60)
        if r.status_code >= 400:
            print(f"  ConnectSecure {r.status_code} on {r.url}\n  body: {r.text[:500]}")
            print(
                "  WWW-Authenticate:",
                r.headers.get("WWW-Authenticate"),
                "| server:",
                r.headers.get("Server"),
            )
        r.raise_for_status()
        j = r.json()
        return j.get("data", j) if isinstance(j, dict) else j

    def paged(self, path, limit=5000):
        out, skip = [], 0
        while True:
            data = self.get(path, skip=skip, limit=limit)
            if not data:
                break
            out.extend(data)
            if len(data) < limit:
                break
            skip += limit
        return out

    def company_name(self, cid):
        if self._companies is None:
            try:
                self._companies = {
                    c.get("id"): c.get("name") for c in self.paged("r/company/companies")
                }
            except Exception:
                self._companies = {}
        return self._companies.get(cid, cid)

    def agents(self):
        rows = []
        for a in self.paged("r/company/agents"):
            rows.append(
                {
                    "cs_id": a.get("id"),
                    "hostname": a.get("host_name") or a.get("name") or "",
                    "host_key": norm_host(a.get("host_name") or a.get("name")),
                    "company": self.company_name(a.get("company_id")),
                    "ip": a.get("ip", ""),
                    "os": a.get("os_name", ""),
                    "agent_type": a.get("agent_type", ""),
                    "last_reported": a.get("last_reported", ""),
                    "deprecated": a.get("is_deprecated"),
                }
            )
        return rows


def resolve_baseline(arg, snaps):
    """Pick a snapshot: None -> newest; filename or path -> that file; YYYY-MM-DD -> newest on/before that date."""
    if not snaps:
        return None
    if not arg:
        return snaps[-1]
    p = Path(arg)
    if p.exists():
        return p
    by_name = {s.name: s for s in snaps}
    if arg in by_name:
        return by_name[arg]
    try:
        cutoff = datetime.strptime(arg, "%Y-%m-%d").strftime("%Y%m%d") + "_235959"
    except ValueError:
        sys.exit(
            f"--baseline '{arg}' is not a snapshot file or a YYYY-MM-DD date. Use --list-snapshots."
        )
    eligible = [s for s in snaps if s.name[len("ninja_") : -len(".json")] <= cutoff]
    if not eligible:
        sys.exit(f"No snapshot on or before {arg}. Use --list-snapshots.")
    return eligible[-1]


# --------------------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month", help="YYYY-MM (default: current month)")
    ap.add_argument("--skip-bitdefender", action="store_true")
    ap.add_argument("--skip-connectsecure", action="store_true")
    ap.add_argument(
        "--baseline",
        help="Snapshot to diff against: filename, full path, or YYYY-MM-DD (newest on/before that date). Default: most recent.",
    )
    ap.add_argument("--list-snapshots", action="store_true", help="List saved snapshots and exit")
    ap.add_argument("--no-save", action="store_true", help="Don't save a new snapshot this run")
    ap.add_argument(
        "--bd-diag", help="Dump raw Bitdefender API responses for one company name and exit"
    )

    args = ap.parse_args()
    if args.bd_diag:
        Bitdefender().diag(args.bd_diag)
        return

    now = datetime.now(timezone.utc)
    month = args.month or now.strftime("%Y-%m")
    y, m = map(int, month.split("-"))
    start = datetime(y, m, 1, tzinfo=timezone.utc)
    end = datetime(y + (m == 12), (m % 12) + 1, 1, tzinfo=timezone.utc)
    if end > now:
        end = now

    audit_dir = Path(env("AUDIT_DIR", "./audit"))
    snap_dir = audit_dir / "snapshots"
    rep_dir = audit_dir / "reports" / month
    snap_dir.mkdir(parents=True, exist_ok=True)
    rep_dir.mkdir(parents=True, exist_ok=True)

    # ---- NinjaOne
    print("NinjaOne: pulling inventory...")
    nj = Ninja()
    orgs = nj.organizations()
    ninja = [ninja_device_row(d, orgs) for d in nj.devices()]
    print(f"  {len(ninja)} devices")

    all_snaps = sorted(snap_dir.glob("ninja_*.json"))
    if args.list_snapshots:
        for p in all_snaps:
            print(p.name)
        return

    snap_path = snap_dir / f"ninja_{now:%Y%m%d_%H%M%S}.json"
    if not args.no_save:
        snap_path.write_text(json.dumps(ninja, indent=1))

    baseline = resolve_baseline(args.baseline, [p for p in all_snaps if p != snap_path])

    added = [
        r
        for r in ninja
        if r["created_epoch"] and start.timestamp() <= float(r["created_epoch"]) < end.timestamp()
    ]

    removed = []
    added_billable = [r for r in added if r["node_class"] in BILLABLE_CLASSES]
    removed_billable = [r for r in removed if r["node_class"] in BILLABLE_CLASSES]
    if baseline:
        prev = json.loads(baseline.read_text())
        cur_ids = {r["ninja_id"] for r in ninja}
        removed = [
            dict(r, source=f"snapshot diff vs {baseline.name}")
            for r in prev
            if r["ninja_id"] not in cur_ids
        ]
        print(f"  baseline: {baseline.name}")
    else:
        print("  no baseline snapshot — removals from diff will appear once one exists")

    # Removed: activity log (deletion-looking entries). Tune DELETE_TERMS after reviewing the raw CSV.
    DELETE_TERMS = ("DELET",)
    print("NinjaOne: pulling activities...")
    acts = nj.activities(start.timestamp(), end.timestamp())
    del_acts = []
    for a in acts:
        hay = " ".join(
            str(a.get(k, ""))
            for k in ("activityType", "statusCode", "status", "type", "message", "subject")
        ).upper()
        if a.get("activityType") == "SYSTEM" and any(t in hay for t in DELETE_TERMS):
            del_acts.append(
                {
                    "activity_id": a.get("id"),
                    "time": epoch_to_iso(a.get("activityTime")),
                    "activityType": a.get("activityType"),
                    "statusCode": a.get("statusCode"),
                    "device_id": a.get("deviceId"),
                    "user_id": a.get("userId"),
                    "message": a.get("message"),
                    "subject": a.get("subject"),
                }
            )
    print(f"  {len(acts)} activities in window, {len(del_acts)} look like deletions")

    ninja_fields = [
        "ninja_id",
        "hostname",
        "organization",
        "node_class",
        "serial",
        "manufacturer",
        "model",
        "created",
        "last_contact",
        "offline",
        "source",
    ]
    write_csv(rep_dir / f"ninja_added_{month}.csv", added_billable, ninja_fields)
    write_csv(rep_dir / f"ninja_removed_{month}.csv", removed_billable, ninja_fields)
    write_csv(rep_dir / f"ninja_added_{month}_all_classes.csv", added, ninja_fields)
    write_csv(rep_dir / f"ninja_removed_{month}_all_classes.csv", removed, ninja_fields)
    write_csv(
        rep_dir / f"ninja_activities_deletions_{month}.csv",
        del_acts,
        [
            "activity_id",
            "time",
            "activityType",
            "statusCode",
            "device_id",
            "user_id",
            "message",
            "subject",
        ],
    )

    ninja_keys = {r["host_key"] for r in ninja if r["host_key"]}
    removed_keys = {r["host_key"] for r in removed if r["host_key"]}

    # ---- Bitdefender
    if not args.skip_bitdefender:
        print("Bitdefender: pulling endpoints...")
        bd = Bitdefender().endpoints()
        print(f"  {len(bd)} endpoints")
        write_csv(
            rep_dir / "debug_bitdefender_raw.csv",
            bd,
            ["bd_id", "hostname", "fqdn", "label", "company", "ip", "managed"],
        )
        bd_keys = {r["host_key"] for r in bd if r["host_key"]}
        bd_fields = [
            "bd_id",
            "hostname",
            "fqdn",
            "label",
            "company",
            "os",
            "ip",
            "macs",
            "managed",
            "first_seen",
            "note",
        ]
        orphans = [
            dict(
                r, note="removed from NinjaOne this month" if r["host_key"] in removed_keys else ""
            )
            for r in bd
            if r["host_key"] not in ninja_keys
        ]
        write_csv(rep_dir / "orphans_bitdefender.csv", orphans, bd_fields)
        cutoff = now.timestamp() - ACTIVE_DAYS * 86400
        missing_bd = [r for r in ninja if r["host_key"] not in bd_keys]
        missing_bd_active = [
            r
            for r in missing_bd
            if r["node_class"] in BILLABLE_CLASSES
            and r["last_contact_epoch"]
            and float(r["last_contact_epoch"]) >= cutoff
        ]
        write_csv(rep_dir / "missing_in_bitdefender.csv", missing_bd, ninja_fields)
        write_csv(rep_dir / "missing_in_bitdefender_active.csv", missing_bd_active, ninja_fields)

    # ---- ConnectSecure
    if not args.skip_connectsecure:
        print("ConnectSecure: pulling agents...")
        cs = ConnectSecure().agents()
        print(f"  {len(cs)} agents")
        cs_keys = {r["host_key"] for r in cs if r["host_key"]}
        cs_fields = [
            "cs_id",
            "hostname",
            "company",
            "ip",
            "os",
            "agent_type",
            "last_reported",
            "deprecated",
            "note",
        ]
        orphans = [
            dict(
                r, note="removed from NinjaOne this month" if r["host_key"] in removed_keys else ""
            )
            for r in cs
            if r["host_key"] not in ninja_keys
        ]
        write_csv(rep_dir / "orphans_connectsecure.csv", orphans, cs_fields)
        write_csv(
            rep_dir / "missing_in_connectsecure.csv",
            [r for r in ninja if r["host_key"] not in cs_keys],
            ninja_fields,
        )

    print(f"\nDone. Reports in {rep_dir}")


if __name__ == "__main__":
    main()
