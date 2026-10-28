#!/usr/bin/env python
"""Register clients and store their connector credentials.

`validate_connector.py` reads a client from the database; this is what puts one
there. Credentials cannot be inserted by hand because `secret_ciphertext` must
be a Fernet token produced by the configured key.

    python scripts/manage_client.py list
    python scripts/manage_client.py add --slug teamlogic-austin --name "TeamLogic IT of Austin"
    python scripts/manage_client.py set-credentials --client teamlogic-austin \
        --connector ninjaone --region us
    python scripts/manage_client.py sync-rules --client teamlogic-austin \
        --connector ninjaone --internal-prefix zz --internal-prefix "TL-"
    python scripts/manage_client.py show --client teamlogic-austin
    python scripts/manage_client.py map-site --client teamlogic-austin --site acme-dental \
        --connector microsoft365 --external-id <entra tenant id>

End customers are never entered here: scripts/sync_sites.py discovers them.
`sync-rules` sets the per-client rules that sync uses to tell the franchisee's
own internal organisations from its customers. `map-site` is the one exception
to discovery: Microsoft 365 cannot be synced — a Graph token is for one
directory and cannot enumerate the others — so each customer's Entra tenant id
goes onto its site by hand (`docs/m365-connector.md`).

Secrets are never taken from the command line: an argument lands in your shell
history and in the process list where anyone on the box can read it. Set them
in the environment, or let the script prompt (input is not echoed).

    NINJAONE_CLIENT_ID=... NINJAONE_CLIENT_SECRET=... python scripts/manage_client.py ...
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from getpass import getpass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.bitdefender.client import REGION_BASE_URLS as BITDEFENDER_REGIONS
from app.connectors.connectsecure.client import POD_HOST
from app.connectors.naming import EXTRA_KEY, rules_for
from app.connectors.ninjaone.client import REGION_BASE_URLS
from app.db.session import dispose_engine, session_scope
from app.models import Client, ConnectorName, ToolCredential, ToolMapping
from app.models.client import SLUG_PATTERN
from app.models.site import Site

EXIT_OK = 0
EXIT_USAGE = 2


@dataclass(frozen=True)
class CredentialSpec:
    """What one connector needs before it can be used for a client."""

    secret_fields: tuple[str, ...]
    env_prefix: str
    config_fields: tuple[str, ...] = field(default_factory=tuple)
    #: Secrets that may be absent: read from the environment, never prompted
    #: for, and left out when they are not set. For a connector that takes a
    #: credential in more than one form — Microsoft 365 takes a certificate or
    #: a secret — with `one_of` saying which combinations are a credential.
    optional_secrets: tuple[str, ...] = field(default_factory=tuple)
    #: Alternative sets of fields, any one of which is a complete credential.
    #: Empty means every `secret_fields` entry is required and that is all.
    one_of: tuple[tuple[str, ...], ...] = field(default_factory=tuple)
    #: Config the connector cannot start without, in the order they are asked for,
    #: each with the prompt shown when its flag is absent. Not secrets, so they are
    #: typed in the clear, but still never in the command line by mistake.
    required_config: tuple[tuple[str, str], ...] = field(default_factory=tuple)


CREDENTIAL_SPECS: dict[ConnectorName, CredentialSpec] = {
    ConnectorName.NINJAONE: CredentialSpec(
        secret_fields=("client_id", "client_secret"),
        env_prefix="NINJAONE",
        config_fields=("region", "base_url", "scope"),
    ),
    ConnectorName.AUTOTASK: CredentialSpec(
        secret_fields=("api_integration_code", "username", "secret"),
        env_prefix="AUTOTASK",
        # No region: the zone is resolved from the API user at connect time.
        # base_url only exists to pin a zone or point at a stub in testing.
        config_fields=("base_url",),
    ),
    ConnectorName.AUTOTASK_NOTES: CredentialSpec(
        # A **second** Autotask API user, for the automation engine's note writer
        # and nothing else: it adds an internal note to a ticket and the client
        # refuses every other write. Requested from the franchisee with the
        # minimum security level in docs/automation-notes.md; never the same
        # user as the read credential, so the read path stays provably read-only
        # and the write path can be switched off by itself.
        secret_fields=("api_integration_code", "username", "secret"),
        env_prefix="AUTOTASK_NOTES",
        config_fields=("base_url",),
    ),
    ConnectorName.BITDEFENDER: CredentialSpec(
        # One API key, generated in GravityZone Control Center > My Account, with the
        # Companies, Network and Incidents APIs enabled. It cannot be made read-only
        # by the vendor; the connector refuses every non-read method itself.
        secret_fields=("api_key",),
        env_prefix="BITDEFENDER",
        config_fields=("region", "base_url"),
    ),
    ConnectorName.MICROSOFT365: CredentialSpec(
        # Our own multi-tenant application, not the client's: the same
        # `client_id` for every client, with a certificate (preferred) or a
        # secret. The *tenant* is not here — it is the site's mapping, because
        # each end customer consents in their own tenant and has their own
        # tenant id (docs/m365-connector.md).
        #
        # A certificate is two fields: the private key we sign the assertion
        # with and the certificate whose thumbprint tells the tenant which key
        # that was. Prefer it: a copy of our configuration is then not a copy
        # of our access.
        secret_fields=("client_id",),
        optional_secrets=("client_secret", "certificate_private_key", "certificate_pem"),
        one_of=(
            ("certificate_private_key", "certificate_pem"),
            ("client_secret",),
        ),
        env_prefix="MICROSOFT365",
        config_fields=("tenant_id", "login_url"),
    ),
    ConnectorName.COVE: CredentialSpec(
        # One Cove Data Protection console user, in a role that can only read
        # (docs/cove-connector.md). The partner name and the user's email
        # address are identifiers rather than secrets and are typed in the
        # clear; only the password is hidden, and it is never echoed, logged
        # or printed back. Cove has no read-only API scope, so the connector
        # refuses every non-read method itself whatever the role allows.
        secret_fields=("password",),
        env_prefix="COVE",
        config_fields=("partner", "username", "base_url"),
        required_config=(
            ("partner", "partner name (the company name used to sign in to Cove)"),
            ("username", "the API user's email address"),
        ),
    ),
    ConnectorName.CONNECTSECURE: CredentialSpec(
        # The client id and secret of one portal user (Global > Settings > Users >
        # API Key), with the tenant name and the pod number from the console's Info
        # button. The user's role should be a viewer; the connector refuses every
        # non-read request itself and the pre-flight warns about a wider role.
        secret_fields=("client_id", "client_secret"),
        env_prefix="CONNECTSECURE",
        config_fields=("tenant", "pod", "base_url"),
        required_config=(
            ("tenant", "tenant name (the organisation used to log in to the portal)"),
            ("base_url", "base URL (the pod, e.g. https://pod401.myconnectsecure.com)"),
        ),
    ),
}


def collect_secrets(spec: CredentialSpec, *, assume_yes: bool) -> dict[str, str]:
    """Read secret fields from the environment, prompting for anything missing.

    An *optional* secret is read from the environment and never prompted for:
    a connector that takes its credential in more than one form (Microsoft 365
    takes a certificate or a secret) would otherwise ask for all of them. What
    makes a credential complete is `one_of`.
    """
    secrets: dict[str, str] = {}
    for name in spec.secret_fields:
        env_name = f"{spec.env_prefix}_{name.upper()}"
        value = os.environ.get(env_name, "").strip()
        source = "environment"
        if not value:
            if assume_yes or not sys.stdin.isatty():
                raise SystemExit(
                    f"{env_name} is not set and there is no terminal to prompt on.\n"
                    f"Set {env_name} in the environment and run again."
                )
            value = getpass(f"{name} (input hidden): ").strip()
            source = "prompt"
        if not value:
            raise SystemExit(f"{name} cannot be empty.")
        secrets[name] = value
        print(f"  {name}: read from {source} ({len(value)} characters)")
    for name in spec.optional_secrets:
        env_name = f"{spec.env_prefix}_{name.upper()}"
        value = os.environ.get(env_name, "").strip()
        if value:
            secrets[name] = value
            print(f"  {name}: read from environment ({len(value)} characters)")
    if spec.one_of and not any(all(secrets.get(f) for f in group) for group in spec.one_of):
        wanted = " or ".join(
            " + ".join(f"{spec.env_prefix}_{f.upper()}" for f in group) for group in spec.one_of
        )
        raise SystemExit(
            f"No complete credential was given. Set {wanted} in the environment and run again."
        )
    return secrets


def collect_config(
    spec: CredentialSpec, args: argparse.Namespace, *, assume_yes: bool
) -> dict[str, str]:
    """The connector's config from the flags, prompting for a required field that is absent.

    ConnectSecure's `--pod` stands in for `--base-url`: the pod number gives the
    host, and both are stored so `show` tells the pod apart from an override.
    """
    config: dict[str, str] = {}
    for name in spec.config_fields:
        value = getattr(args, name.replace("-", "_"), None)
        if value:
            config[name] = str(value).strip()
    if config.get("pod") and not config.get("base_url"):
        config["base_url"] = POD_HOST.format(pod=config["pod"].lower().removeprefix("pod"))
    for name, prompt in spec.required_config:
        if config.get(name):
            continue
        flag = f"--{name.replace('_', '-')}"
        if assume_yes or not sys.stdin.isatty():
            raise SystemExit(
                f"{flag} is required for {spec.env_prefix.lower()} and there is no terminal to "
                f"prompt on.\nPass {flag} and run again."
            )
        value = input(f"{prompt}: ").strip()
        if not value:
            raise SystemExit(f"{name} cannot be empty.")
        config[name] = value
    return config


async def cmd_list(_: argparse.Namespace) -> int:
    async with session_scope() as session:
        clients = (await session.execute(select(Client).order_by(Client.slug))).scalars().all()
        if not clients:
            print("No clients configured. Add one with:")
            print('  python scripts/manage_client.py add --slug my-client --name "My Client"')
            return EXIT_OK

        for client in clients:
            mappings = (
                (
                    await session.execute(
                        select(ToolMapping).where(ToolMapping.client_id == client.id)
                    )
                )
                .scalars()
                .all()
            )
            configured = ", ".join(sorted(m.connector_name.value for m in mappings)) or "none"
            state = "" if client.is_active else "  [INACTIVE]"
            print(f"{client.slug}{state}")
            print(f"  id:         {client.id}")
            print(f"  name:       {client.name}")
            print(f"  connectors: {configured}")
    return EXIT_OK


async def _find_client(session: AsyncSession, reference: str) -> Client:
    try:
        parsed: uuid.UUID | None = uuid.UUID(reference)
    except ValueError:
        parsed = None
    query = (
        select(Client).where(Client.id == parsed)
        if parsed is not None
        else select(Client).where(Client.slug == reference)
    )
    client = (await session.execute(query)).scalar_one_or_none()
    if client is None:
        raise SystemExit(f"No client matches {reference!r}. Run 'manage_client.py list'.")
    return client


async def cmd_add(args: argparse.Namespace) -> int:
    import re

    if not re.match(SLUG_PATTERN, args.slug):
        raise SystemExit(
            f"{args.slug!r} is not a valid slug. Use lowercase letters, digits and "
            "hyphens, 2-63 characters, not starting with a hyphen."
        )

    async with session_scope() as session:
        existing = (
            await session.execute(select(Client).where(Client.slug == args.slug))
        ).scalar_one_or_none()
        if existing is not None:
            print(f"Client {args.slug!r} already exists ({existing.id}).")
            return EXIT_OK

        client = Client(slug=args.slug, name=args.name, notes=args.notes)
        session.add(client)
        await session.flush()
        print(f"Added client {client.slug!r}\n  id: {client.id}")
        print("\nNext: store its connector credentials, e.g.")
        print(
            f"  python scripts/manage_client.py set-credentials --client {client.slug} "
            "--connector ninjaone"
        )
    return EXIT_OK


async def cmd_set_credentials(args: argparse.Namespace) -> int:
    connector = ConnectorName(args.connector)
    spec = CREDENTIAL_SPECS.get(connector)
    if spec is None:
        raise SystemExit(
            f"No credential layout is defined for {connector.value}. "
            f"Known: {', '.join(sorted(c.value for c in CREDENTIAL_SPECS))}"
        )

    if args.region and "region" not in spec.config_fields:
        raise SystemExit(f"--region does not apply to {connector.value}")
    regions = BITDEFENDER_REGIONS if connector is ConnectorName.BITDEFENDER else REGION_BASE_URLS
    if args.region and args.region not in regions:
        raise SystemExit(f"--region must be one of {', '.join(sorted(regions))}")

    async with session_scope() as session:  # refused before anyone types a secret
        client = await _find_client(session, args.client)
        if client.is_demo:
            raise SystemExit(
                f"{client.slug} is the demo tenant: it holds no credentials and reaches no "
                "vendor. Its connectors are the fakes in app/demo/."
            )

    config: dict[str, Any] = collect_config(spec, args, assume_yes=args.no_prompt)

    print(f"Reading {connector.value} credentials:")
    secrets = collect_secrets(spec, assume_yes=args.no_prompt)

    async with session_scope() as session:
        client = await _find_client(session, args.client)

        credential = (
            await session.execute(
                select(ToolCredential).where(
                    ToolCredential.client_id == client.id,
                    ToolCredential.connector_name == connector,
                )
            )
        ).scalar_one_or_none()
        if credential is None:
            credential = ToolCredential(client_id=client.id, connector_name=connector)
            session.add(credential)
            credential_action = "created"
        else:
            credential_action = "replaced"

        credential.set_secrets(secrets)
        credential.config = config
        credential.is_active = True
        await session.flush()

        print(
            f"\ncredential {credential_action}: encrypted with key {credential.encryption_key_id!r}"
        )
        if config:
            for name, value in config.items():
                print(f"config {name + ':':<14}{value}")
        else:
            print("config:              {} (defaults)")
        print("\nNow discover this client's end customers, then make a live call:")
        print(
            f"  python scripts/sync_sites.py --client {client.slug} --connector {connector.value}"
        )
        print(f"  python scripts/validate_connector.py --client {client.slug} --tool list_devices")
    return EXIT_OK


async def _tenant_mapping(
    session: AsyncSession, client: Client, connector: ConnectorName
) -> ToolMapping | None:
    return (
        await session.execute(
            select(ToolMapping).where(
                ToolMapping.client_id == client.id,
                ToolMapping.connector_name == connector,
                ToolMapping.site_id.is_(None),
            )
        )
    ).scalar_one_or_none()


async def cmd_sync_rules(args: argparse.Namespace) -> int:
    """Set which of a vendor's organisations sync treats as the franchisee's own.

    The rules live in `extra["sync"]` of the client's tenant-level mapping for
    the connector. Only the keys given are written; the rest keep their
    defaults (see `app/connectors/naming.py`).
    """
    connector = ConnectorName(args.connector)
    async with session_scope() as session:
        client = await _find_client(session, args.client)
        mapping = await _tenant_mapping(session, client, connector)

        if args.show or not (
            args.internal_prefix
            or args.internal_pattern
            or args.match_tenant_name is not None
            or args.reset
        ):
            effective = rules_for(connector, mapping.extra if mapping else None)
            stored = ((mapping.extra if mapping else None) or {}).get(EXTRA_KEY) or {}
            print(f"{client.slug} / {connector.value}")
            print(f"  stored override: {stored or '{} (defaults apply)'}")
            print(f"  internal prefixes: {list(effective.internal_prefixes) or '-'}")
            print(f"  internal patterns: {list(effective.internal_name_patterns) or '-'}")
            print(
                f"  organisation named like the client is internal: {effective.match_tenant_name}"
            )
            return EXIT_OK

        if mapping is None:
            mapping = ToolMapping(client_id=client.id, site_id=None, connector_name=connector)
            session.add(mapping)
        elif mapping.external_id is not None:
            # A row from before sites still carries a vendor id. Writing to it
            # now would trip the check constraint; sync_sites adopts it first.
            raise SystemExit(
                f"The tenant-level {connector.value} row for {client.slug!r} still carries "
                f"vendor id {mapping.external_id!r} from before sites existed. Run "
                f"scripts/sync_sites.py --client {client.slug} --connector {connector.value} "
                "so it is adopted onto its site, then set the rules."
            )

        override: dict[str, Any] = (
            {} if args.reset else dict((mapping.extra or {}).get(EXTRA_KEY) or {})
        )
        if args.internal_prefix:
            override["internal_prefixes"] = list(args.internal_prefix)
        if args.internal_pattern:
            override["internal_name_patterns"] = list(args.internal_pattern)
        if args.match_tenant_name is not None:
            override["match_tenant_name"] = args.match_tenant_name
        # Assign a new dict: in-place edits to a JSONB column are not tracked.
        mapping.extra = {**(mapping.extra or {}), EXTRA_KEY: override}
        await session.flush()
        effective = rules_for(connector, mapping.extra)

    print(f"{client.slug} / {connector.value}: rules stored")
    print(f"  internal prefixes: {list(effective.internal_prefixes) or '-'}")
    print(f"  internal patterns: {list(effective.internal_name_patterns) or '-'}")
    print(f"  organisation named like the client is internal: {effective.match_tenant_name}")
    print("\nThey apply on the next run of scripts/sync_sites.py.")
    return EXIT_OK


async def cmd_map_site(args: argparse.Namespace) -> int:
    """Map one vendor organisation onto one of a client's sites, by hand.

    `sync_sites` does this from the vendor's own list of organisations, and is
    how every connector but one is mapped. **Microsoft 365 cannot be synced**:
    a Graph token is for one directory and cannot enumerate the others, so
    each end customer's Entra tenant id is put on its site by a person, from
    the consent that customer's administrator granted
    (`docs/m365-connector.md`). This is that command.

    It refuses an id another client already holds, as the sync does: silently
    re-pointing a mapping would move one franchisee's customer into another
    franchisee's view (hard rule 1).
    """
    connector = ConnectorName(args.connector)
    external = args.external_id.strip()
    if not external:
        raise SystemExit(
            "--external-id is the vendor's own id for the customer; it cannot be empty"
        )
    async with session_scope() as session:
        client = await _find_client(session, args.client)
        site = (
            await session.execute(
                select(Site).where(Site.client_id == client.id, Site.slug == args.site)
            )
        ).scalar_one_or_none()
        if site is None:
            raise SystemExit(
                f"{args.site!r} is not a site of {client.slug!r}. "
                f"Run 'manage_client.py show --client {client.slug}' for its customers."
            )
        held = (
            await session.execute(
                select(ToolMapping).where(
                    ToolMapping.connector_name == connector,
                    ToolMapping.external_id == external,
                )
            )
        ).scalar_one_or_none()
        if held is not None and held.client_id != client.id:
            raise SystemExit(
                f"{connector.value} id {external!r} is already mapped under another client. "
                "Nothing was changed."
            )
        if held is not None and held.site_id == site.id:
            print(f"{client.slug} / {site.slug}: {connector.value} {external} was already mapped")
            return EXIT_OK
        if held is not None:
            raise SystemExit(
                f"{connector.value} id {external!r} is mapped onto another of this client's "
                f"sites. Move it with scripts/link_sites.py rather than mapping it twice."
            )
        session.add(
            ToolMapping(
                client_id=client.id,
                site_id=site.id,
                connector_name=connector,
                external_id=external,
                external_name=args.name or site.name,
            )
        )
        await session.flush()
        slug, name = site.slug, site.name
    print(f"{client.slug} / {slug} ({name}): {connector.value} {external} mapped")
    if connector is ConnectorName.MICROSOFT365:
        print(
            f"\nRead it with: python scripts/m365.py profile --client {args.client} --site {slug}"
        )
    return EXIT_OK


def _report_files(
    args: argparse.Namespace,
) -> tuple[
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
    dict[str, Any] | None,
    str | None,
]:
    """Read what `report-config` was pointed at, before any database work starts."""
    replacement = None
    if args.file:
        replacement = json.loads(Path(args.file).read_text(encoding="utf-8"))
    resolved = None
    if getattr(args, "resolved_file", None):
        resolved = json.loads(Path(args.resolved_file).read_text(encoding="utf-8"))
    autopilot = None
    if getattr(args, "autopilot_file", None):
        autopilot = json.loads(Path(args.autopilot_file).read_text(encoding="utf-8"))
    automation = None
    if getattr(args, "automation_file", None):
        automation = json.loads(Path(args.automation_file).read_text(encoding="utf-8"))
    backup = None
    if getattr(args, "backup_file", None):
        backup = json.loads(Path(args.backup_file).read_text(encoding="utf-8"))
    logo = None
    if args.logo:
        if not Path(args.logo).is_file():
            raise SystemExit(f"No file at {args.logo}")
        logo = str(Path(args.logo).resolve())
    return replacement, resolved, autopilot, automation, backup, logo


async def cmd_report_config(args: argparse.Namespace) -> int:
    """Show, replace or amend a franchisee's business-review settings.

    `--file` replaces the settings with a JSON document of the shape `--show`
    prints. The individual flags amend what is there. The price book is the
    only place a price or an hour figure on a review can come from: a
    recommendation type with no entry prints "Quote pending".

    `--autopilot-file` replaces the triage agent's label policy alone — which
    issue types it may not predict for this franchisee, and which ones they
    file as something else (CLAUDE.md, "Autopilot").

    `--resolved-file` replaces the Resolved half alone — SLA targets by
    priority label, the statuses that pause a clock, holidays, the class rules,
    which Autotask resources are ours, and the loaded hourly cost per engineer
    (CLAUDE.md, "Resolved: clocks, ledger, baselines"). It is a document of its
    own because changing one SLA target should not mean retyping a price book,
    and because every key in it is a **label**, never a picklist number.

    `--backup-file` replaces the backup settings alone, which are per site:
    `scope` (auto, servers, or servers_and_workstations — which machines count
    as a backup gap there), `no_backup_needed` (machines that are not expected
    to have one) and `coverage_reviewed` (whether somebody has been through
    that site's list). Per site because what a customer bought differs by
    customer (CLAUDE.md, "Business reviews").

    `--automation-file` replaces the automation settings alone: who at this
    franchisee's clients may authorise a run (`approvers`, each a name and
    optionally a role and an address), and what each workflow is told about
    this client (`workflows`, keyed by workflow name). Nothing a workflow needs
    to know about a client is written in code (CLAUDE.md, "Automation").
    """
    from app.reports.tbr.config import (
        FranchiseeConfig,
        PriceBookEntry,
        load_config,
        save_config,
        set_people_names,
    )

    replacement, resolved, autopilot, automation, backup, logo = _report_files(args)
    async with session_scope() as session:
        client = await _find_client(session, args.client)
        config = await load_config(session, client.id, client_name=client.name)
        changed = False
        if replacement is not None:
            config = FranchiseeConfig.model_validate({"display_name": client.name, **replacement})
            changed = True
        if resolved is not None:
            from app.resolved.config import ResolvedConfig

            config.resolved = ResolvedConfig.model_validate(resolved)
            changed = True
        if autopilot is not None:
            from app.autopilot.config import AutopilotConfig

            config.autopilot = AutopilotConfig.model_validate(autopilot)
            changed = True
        if backup is not None:
            from app.reports.tbr.backup_config import SCOPES, BackupConfig

            config.backup = BackupConfig.model_validate(backup)
            # A scope nobody recognises would read back as `auto` and look
            # like it had been accepted, so it is refused here instead.
            wrong = {
                site: value for site, value in config.backup.scope.items() if value not in SCOPES
            }
            if wrong:
                raise SystemExit(
                    f"--backup-file: scope must be one of {', '.join(SCOPES)}; got {wrong}"
                )
            changed = True
        if automation is not None:
            import app.automation  # noqa: F401  loads the definitions
            from app.automation import library
            from app.automation.config import AutomationConfig

            config.automation = AutomationConfig.model_validate(automation)
            # Each workflow's settings against what its definition declares:
            # the types, and no key the definition does not know. The
            # per-trigger rule overrides (`triggers`) are always allowed.
            problems: list[str] = []
            for name, given in config.automation.workflows.items():
                loaded = library.find(name)
                if loaded is None:
                    names = ", ".join(d.name for d in library.definitions())
                    problems.append(f"{name}: not a workflow (the definitions are: {names})")
                    continue
                problems.extend(f"{name}.{p}" for p in loaded.wrong_settings(given))
            if problems:
                raise SystemExit("--automation-file: " + "; ".join(problems))
            changed = True
        if args.franchisee_can_post is not None:
            config.automation.notes.franchisee_can_post = args.franchisee_can_post
            changed = True
        if args.display_name:
            config.display_name, changed = args.display_name, True
        if logo:
            config.logo_path, changed = logo, True
        if args.months:
            config.review_period_months, changed = args.months, True
        if args.timezone:
            from app.reports.tbr.config import TimeZoneUnavailable, resolve_zone

            try:
                resolve_zone(args.timezone)
            except TimeZoneUnavailable as exc:
                raise SystemExit(f"--timezone: {exc}") from exc
            config.business_hours.timezone, changed = args.timezone, True
        if args.business_hours:
            try:
                opens, _, closes = args.business_hours.partition("-")
                config.business_hours.start = dt.time.fromisoformat(opens.strip())
                config.business_hours.end = dt.time.fromisoformat(closes.strip())
            except ValueError as exc:
                raise SystemExit(f"--business-hours: expected 08:00-17:00 ({exc})") from exc
            changed = True
        for entry in args.price or []:
            try:
                key, _, figures = entry.partition("=")
                fields = dict(part.split(":", 1) for part in figures.split(",") if part)
                config.price_book[key.strip()] = PriceBookEntry.model_validate(fields)
            except ValueError as exc:
                raise SystemExit(
                    f"--price {entry!r}: expected KEY=cost:1450,hours:3,per:device ({exc})"
                ) from exc
            changed = True
        for key in args.unprice or []:
            changed = config.price_book.pop(key, None) is not None or changed
        if args.generation_limit is not None:
            config.self_service.generations_per_site_per_month = args.generation_limit
            changed = True
        if changed:
            config = FranchiseeConfig.model_validate(config.model_dump())
            await save_config(session, client.id, config, updated_by=args.user)
            print(f"Saved report settings for {client.slug}.")
        people_changed, touched = False, []
        if args.people_names is not None:
            people_changed, touched = await set_people_names(
                session,
                client.id,
                client_name=client.name,
                show=args.people_names,
                by=args.user,
            )
            config.show_people_names = args.people_names
        client_id, client_slug = client.id, client.slug
        print(json.dumps(config.model_dump(mode="json"), indent=2))
    if people_changed:
        # Every current review's renders are now stale; the change is on the record.
        from app.models.enums import AuditEventType, AuditOutcome
        from app.reports.tbr.audit import ReportAudit

        await ReportAudit(actor_id=args.user, client_id=client_id, client_ref=client_slug).write(
            AuditEventType.REPORT_CONFIG_CHANGED,
            AuditOutcome.SUCCESS,
            payload={
                "setting": "show_people_names",
                "value": args.people_names,
                "reviews_made_stale": touched,
            },
        )
        print(
            f"show_people_names is now {str(args.people_names).lower()}; {len(touched)} "
            "review(s) marked changed, so their decks and PDFs must be rendered again."
        )
    elif args.people_names is not None:
        print(f"show_people_names was already {str(args.people_names).lower()}.")
    return 0


async def cmd_show(args: argparse.Namespace) -> int:
    # Imported here for the reason the report-config command does: the report
    # package reaches the connectors, and this module is loaded by every
    # subcommand (CLAUDE.md, "Working conventions").
    from app.reports.tbr.config import load_config

    async with session_scope() as session:
        client = await _find_client(session, args.client)
        print(f"{client.slug}")
        print(f"  id:      {client.id}")
        print(f"  name:    {client.name}")
        print(f"  active:  {client.is_active}")

        mappings = (
            (await session.execute(select(ToolMapping).where(ToolMapping.client_id == client.id)))
            .scalars()
            .all()
        )
        credentials = {
            credential.connector_name: credential
            for credential in (
                await session.execute(
                    select(ToolCredential).where(ToolCredential.client_id == client.id)
                )
            )
            .scalars()
            .all()
        }
        site_count = (
            await session.execute(
                select(func.count()).select_from(Site).where(Site.client_id == client.id)
            )
        ).scalar_one()

        connectors = sorted(
            {*credentials} | {mapping.connector_name for mapping in mappings},
            key=lambda name: name.value,
        )
        if not connectors:
            print("\n  No connectors configured.")
            return EXIT_OK

        print(f"  sites:   {site_count} (scripts/sync_sites.py --list for the detail)")
        # The one number that decides what this franchisee may do on their own,
        # printed where somebody looking for it would look.
        report_config = await load_config(session, client.id, client_name=client.name)
        allowance = report_config.self_service.generations_per_site_per_month
        print(
            f"  reviews: {allowance} generation(s) a customer a month for their own "
            f"administrators" + (" (off)" if allowance == 0 else "")
        )
        for connector in connectors:
            credential = credentials.get(connector)
            tenant = next(
                (m for m in mappings if m.connector_name == connector and m.site_id is None), None
            )
            site_rows = [m for m in mappings if m.connector_name == connector and m.site_id]
            print(f"\n  {connector.value}")
            print(f"    site mappings: {len(site_rows)}")
            if tenant is not None:
                rules = tenant.extra.get(EXTRA_KEY) or {}
                print(f"    sync rules:    {rules or '{} (defaults)'}")
                if tenant.external_id is not None:
                    print(
                        f"    NOTE:          tenant-level row still carries vendor id "
                        f"{tenant.external_id!r}; run sync_sites to adopt it onto a site"
                    )
            if credential is None:
                print("    credentials:   MISSING - the connector cannot be built")
                continue
            # Field names only. The values stay encrypted and are never printed.
            fields = ", ".join(sorted(credential.get_secrets())) or "none"
            print(f"    credentials:   present, {'active' if credential.is_active else 'DISABLED'}")
            print(f"    secret fields: {fields}")
            print(f"    encrypted by:  key {credential.encryption_key_id!r}")
            if credential.config:
                for name, value in credential.config.items():
                    print(f"    config {name + ':':<8}{value}")
            else:
                print("    config:        {} (defaults)")
            print(f"    updated:       {credential.updated_at}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage_client",
        description="Register clients and store their connector credentials.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list configured clients").set_defaults(func=cmd_list)

    add = sub.add_parser("add", help="register a new client")
    add.add_argument("--slug", required=True, help="lowercase handle, e.g. teamlogic-austin")
    add.add_argument("--name", required=True, help="display name")
    add.add_argument("--notes", help="free text")
    add.set_defaults(func=cmd_add)

    creds = sub.add_parser(
        "set-credentials", help="store or replace a client's credentials for one connector"
    )
    creds.add_argument("--client", required=True, help="client slug or UUID")
    creds.add_argument(
        "--connector",
        required=True,
        choices=sorted(c.value for c in CREDENTIAL_SPECS),
        help="which connector",
    )
    creds.add_argument(
        "--region",
        help=f"NinjaOne: one of {', '.join(sorted(REGION_BASE_URLS))}; Bitdefender: one of "
        f"{', '.join(sorted(BITDEFENDER_REGIONS))} (the GravityZone cloud instance)",
    )
    creds.add_argument(
        "--base-url",
        help="the vendor host; ConnectSecure: required (or --pod), the tenant's pod URL, "
        "e.g. https://pod401.myconnectsecure.com. Prompted for if absent",
    )
    creds.add_argument(
        "--tenant",
        help="ConnectSecure: required, the tenant (portal login) name. Prompted for if absent",
    )
    creds.add_argument(
        "--pod",
        help="ConnectSecure: the pod number from the console's Info button, e.g. 401; "
        "stands in for --base-url",
    )
    creds.add_argument(
        "--partner",
        help="Cove: required, the partner (company) name used to sign in. Prompted for if "
        "absent. Not a secret, so it may be a flag; the password never may",
    )
    creds.add_argument(
        "--username",
        help="Cove: required, the API user's email address. Prompted for if absent",
    )
    creds.add_argument("--scope", help="OAuth scope (default: monitoring)")
    creds.add_argument(
        "--no-prompt",
        action="store_true",
        help="fail instead of prompting if a secret env var or a required config flag is missing",
    )
    creds.set_defaults(func=cmd_set_credentials)

    report = sub.add_parser(
        "report-config",
        help="business-review settings: name, logo, service catalog, price book, period",
        description=cmd_report_config.__doc__,
    )
    report.add_argument("--client", required=True, help="client slug or UUID")
    report.add_argument("--file", help="JSON file that replaces the settings")
    report.add_argument(
        "--resolved-file",
        help="JSON file that replaces the Resolved settings alone: SLA targets by priority "
        "label, pausing statuses, holidays, class rules, our resources, the cost book",
    )
    report.add_argument(
        "--autopilot-file",
        help="JSON file that replaces the Autopilot label policy alone: the issue-type "
        "labels the triage agent may not predict, and the ones this franchisee files as "
        "something else",
    )
    report.add_argument(
        "--automation-file",
        help="JSON file that replaces the automation settings alone: the client's "
        "approvers, and per-workflow settings keyed by workflow name",
    )
    report.add_argument(
        "--backup-file",
        help="JSON file that replaces the backup settings alone: per site, which machines "
        "are expected to have a backup (scope), which need none (no_backup_needed), and "
        "whether somebody has reviewed the coverage list (coverage_reviewed)",
    )
    who_posts = report.add_mutually_exclusive_group()
    who_posts.add_argument(
        "--franchisee-can-post",
        dest="franchisee_can_post",
        action="store_const",
        const=True,
        help="automation.notes.franchisee_can_post on: the franchisee's administrators "
        "and technicians see Desk notes on tickets and may post or dismiss them",
    )
    who_posts.add_argument(
        "--inputiv-posts-only",
        dest="franchisee_can_post",
        action="store_const",
        const=False,
        help="the default: Desk notes are shown to and posted by Inputiv staff only",
    )
    report.add_argument(
        "--timezone",
        help="the customers' business-hours time zone: America/Chicago, or a fixed offset "
        "such as UTC-06:00. Without one, out-of-hours figures are left off the review",
    )
    report.add_argument("--business-hours", help="opening hours, e.g. 08:00-17:00")
    report.add_argument("--display-name", help='printed after "Presented by:"')
    report.add_argument("--logo", help="image file for the title slide")
    report.add_argument("--months", type=int, help="default review period length")
    report.add_argument(
        "--price",
        action="append",
        metavar="KEY=cost:N,hours:N,per:UNIT",
        help="set one price book entry, e.g. device_replacement=cost:1450,hours:3,per:device",
    )
    report.add_argument("--unprice", action="append", metavar="KEY", help="remove an entry")
    report.add_argument(
        "--generation-limit",
        type=int,
        metavar="N",
        help="how many reviews this franchisee's own administrators may generate of one "
        "customer in a calendar month (default 10). Generations by Inputiv staff are "
        "neither counted nor limited. 0 stops them generating at all",
    )
    people = report.add_mutually_exclusive_group()
    people.add_argument(
        "--show-people-names",
        dest="people_names",
        action="store_const",
        const=True,
        help="client and internal renders name people: the contact chart, the lifecycle "
        "tables' users, person-named locations. Makes every current review's renders stale",
    )
    people.add_argument(
        "--hide-people-names",
        dest="people_names",
        action="store_const",
        const=False,
        help="Contact A, User A and withheld locations on client and internal renders (the "
        "default). Makes every current review's renders stale",
    )
    report.set_defaults(people_names=None)
    report.add_argument(
        "--user", default=os.environ.get("USERNAME") or os.environ.get("USER") or "cli"
    )
    report.set_defaults(func=cmd_report_config)

    show = sub.add_parser("show", help="show one client's configuration")
    show.add_argument("--client", required=True, help="client slug or UUID")
    show.set_defaults(func=cmd_show)

    rules = sub.add_parser(
        "sync-rules",
        help="set which vendor organisations sync_sites records as the franchisee's own",
    )
    rules.add_argument("--client", required=True, help="client slug or UUID")
    rules.add_argument(
        "--connector",
        required=True,
        choices=sorted(c.value for c in CREDENTIAL_SPECS),
        help="which connector the rules apply to",
    )
    rules.add_argument(
        "--internal-prefix",
        action="append",
        metavar="PREFIX",
        help="organisation names starting with this are internal (repeatable; replaces)",
    )
    rules.add_argument(
        "--internal-pattern",
        action="append",
        metavar="REGEX",
        help="organisation names matching this regex are internal (repeatable; replaces)",
    )
    rules.add_argument(
        "--match-tenant-name",
        dest="match_tenant_name",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="whether an organisation named like the client itself is internal",
    )
    rules.add_argument("--reset", action="store_true", help="drop the stored override first")
    rules.add_argument("--show", action="store_true", help="print the effective rules and exit")
    rules.set_defaults(func=cmd_sync_rules)

    mapping = sub.add_parser(
        "map-site",
        help="map a vendor organisation onto a site by hand (Microsoft 365 cannot be synced)",
    )
    mapping.add_argument("--client", required=True, help="client slug or UUID")
    mapping.add_argument("--site", required=True, help="the end customer's slug")
    mapping.add_argument(
        "--connector",
        required=True,
        choices=sorted(c.value for c in CREDENTIAL_SPECS),
        help="which connector the id belongs to",
    )
    mapping.add_argument(
        "--external-id",
        required=True,
        help="the vendor's own id for that customer (Microsoft 365: the Entra tenant id)",
    )
    mapping.add_argument("--name", default=None, help="what the vendor calls them, for the record")
    mapping.set_defaults(func=cmd_map_site)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_main(args))
    except SystemExit as error:
        print(str(error), file=sys.stderr)
        return EXIT_USAGE


async def _main(args: argparse.Namespace) -> int:
    try:
        result: int = await args.func(args)
        return result
    finally:
        await dispose_engine()


if __name__ == "__main__":
    raise SystemExit(main())
