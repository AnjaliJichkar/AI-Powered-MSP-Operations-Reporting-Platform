#!/usr/bin/env python
"""Create the Inputiv organisation, the first administrator, and invites.

There is a bootstrapping problem: an invite is created by an administrator, and
the first administrator has nobody to invite them. This script is the answer,
and it is the only way an account is made without one. It needs database
credentials, which is the point — it runs on the box, by whoever deploys.

    python scripts/manage_auth.py bootstrap --email you@inputiv.com
    python scripts/manage_auth.py add-organisation --client teamlogic-it-austin \
        --slug teamlogic-austin --name "TeamLogic IT of Austin"
    python scripts/manage_auth.py invite --email them@example.com --role franchisee_admin \
        --organisation teamlogic-austin
    python scripts/manage_auth.py invite --email them@example.com --role franchisee_admin \
        --organisation teamlogic-austin --reissue
    python scripts/manage_auth.py revoke-invite --email them@example.com
    python scripts/manage_auth.py list-users
    python scripts/manage_auth.py suspend --email them@example.com
    python scripts/manage_auth.py reset-password --email them@example.com

Passwords are never taken from the command line: an argument lands in your
shell history and in the process list where anyone on the box can read it. Set
`INPUTIV_ADMIN_PASSWORD`, or let the script prompt (input is not echoed).

`invite` prints the whole link, built from `PUBLIC_BASE_URL`, because a bare
token is not something the person receiving it can do anything with. The token
is printed **once**: only its hash is stored, so a lost link is reissued
(`--reissue`) rather than looked up. Send it by a channel that is not this
terminal's scrollback.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import os
import sys
from getpass import getpass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.service import (
    InviteRefused,
    count_users,
    create_invite,
    create_organisation,
    create_user,
    get_user_by_email,
    open_invites_for,
    revoke_invites_for,
)
from app.auth.sessions import revoke_all_for_user
from app.db.session import dispose_engine, session_scope
from app.models import Client, Organisation, OrganisationKind, Role, User, UserStatus
from app.security.passwords import WeakPassword, check_password_policy, hash_password
from app.settings import get_settings

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CONFLICT = 3

#: The Inputiv organisation's slug. One row, and everything that switches
#: client belongs to it, so the name is fixed rather than an argument.
INPUTIV_SLUG = "inputiv"


def _password(prompt: str, *, env: str = "INPUTIV_ADMIN_PASSWORD") -> str:
    """From the environment, or prompted twice. Never from a command line."""
    supplied = os.environ.get(env)
    if supplied:
        return supplied
    while True:
        first = getpass(f"{prompt}: ")
        try:
            check_password_policy(first)
        except WeakPassword as exc:
            print(f"  {exc}", file=sys.stderr)
            continue
        if first != getpass("Repeat it: "):
            print("  those did not match", file=sys.stderr)
            continue
        return first


async def _organisation(session: AsyncSession, slug: str) -> Organisation | None:
    return (
        await session.execute(select(Organisation).where(Organisation.slug == slug))
    ).scalar_one_or_none()


async def cmd_bootstrap(args: argparse.Namespace) -> int:
    """Create the Inputiv organisation and its first administrator.

    Safe to run twice: the organisation is reused if it exists, and a second
    administrator is only added if the address is new. It refuses to create a
    *second* first-admin silently.
    """
    async with session_scope() as session:
        organisation = await _organisation(session, INPUTIV_SLUG)
        if organisation is None:
            organisation = await create_organisation(
                session,
                kind=OrganisationKind.INPUTIV,
                slug=INPUTIV_SLUG,
                name=args.name,
            )
            print(f"created organisation {INPUTIV_SLUG!r} ({args.name})")
        else:
            print(f"organisation {INPUTIV_SLUG!r} already exists")

        email = args.email.strip().lower()
        if await get_user_by_email(session, email) is not None:
            print(f"a user already exists for {email}", file=sys.stderr)
            return EXIT_CONFLICT

        existing = await count_users(session, organisation.id)
        password = _password(f"Password for {email}")
        try:
            user = await create_user(
                session,
                organisation=organisation,
                email=email,
                role=Role.INPUTIV_ADMIN,
                password=password,
                display_name=args.display_name,
                status=UserStatus.ACTIVE,
            )
        except (ValueError, WeakPassword) as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_USAGE
        print(f"created {user.email} as {user.role.value}")
        if existing:
            print(f"note: {INPUTIV_SLUG!r} already had {existing} user(s)")
    return EXIT_OK


async def cmd_add_organisation(args: argparse.Namespace) -> int:
    """Create a franchisee's organisation, bound to an existing client."""
    async with session_scope() as session:
        client = (
            await session.execute(select(Client).where(Client.slug == args.client))
        ).scalar_one_or_none()
        if client is None:
            print(f"no client with slug {args.client!r}", file=sys.stderr)
            return EXIT_USAGE
        slug = args.slug or client.slug
        if await _organisation(session, slug) is not None:
            print(f"an organisation already exists with slug {slug!r}", file=sys.stderr)
            return EXIT_CONFLICT
        try:
            organisation = await create_organisation(
                session,
                kind=OrganisationKind.FRANCHISEE,
                slug=slug,
                name=args.name or client.name,
                client=client,
            )
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_USAGE
        print(f"created organisation {organisation.slug!r} for client {client.slug!r}")
    return EXIT_OK


async def cmd_invite(args: argparse.Namespace) -> int:
    """Create a one-time invite. The token is printed once and not stored."""
    settings = get_settings()
    role = Role(args.role)
    async with session_scope() as session:
        slug = args.organisation or (INPUTIV_SLUG if role.is_inputiv else None)
        if slug is None:
            print(
                "--organisation is required for a franchisee role; "
                "name the franchisee's organisation slug",
                file=sys.stderr,
            )
            return EXIT_USAGE
        organisation = await _organisation(session, slug)
        if organisation is None:
            print(f"no organisation with slug {slug!r}", file=sys.stderr)
            return EXIT_USAGE
        try:
            invite, token = await create_invite(
                session,
                organisation=organisation,
                email=args.email,
                role=role,
                settings=settings,
                reissue=args.reissue,
            )
        except (InviteRefused, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            if not args.reissue:
                print(
                    "Add --reissue to withdraw the outstanding invitation and send a new one.",
                    file=sys.stderr,
                )
            return EXIT_CONFLICT
        print(f"invited {invite.email} to {organisation.slug!r} as {role.value}")
        print(f"expires {invite.expires_at:%Y-%m-%d %H:%M UTC}")
        print()
        # The link, not the token: a bare token is not something the person
        # receiving it can use, and asking them to paste one into a form we
        # never built was the gap this closes.
        print("  Send them this link. It works once, and is not recoverable:")
        print(f"  {settings.url_for(f'/invite/{token}')}")
    return EXIT_OK


async def cmd_revoke_invite(args: argparse.Namespace) -> int:
    """Withdraw every outstanding invitation for an address.

    For one sent to the wrong address: reissuing to the right one does nothing
    about the wrong one, which is still a live link into an organisation.
    """
    async with session_scope() as session:
        revoked = await revoke_invites_for(session, args.email)
        if not revoked:
            print(
                f"no outstanding invitation for {args.email.strip().lower()}",
                file=sys.stderr,
            )
            return EXIT_USAGE
        print(f"withdrew {revoked} invitation(s) for {args.email.strip().lower()}")
    return EXIT_OK


async def cmd_list_users(args: argparse.Namespace) -> int:
    async with session_scope() as session:
        query = select(User).order_by(User.email)
        if args.organisation:
            organisation = await _organisation(session, args.organisation)
            if organisation is None:
                print(f"no organisation with slug {args.organisation!r}", file=sys.stderr)
                return EXIT_USAGE
            query = query.where(User.organisation_id == organisation.id)
        users = (await session.execute(query)).scalars().all()
        if not users:
            print("no users")
            return EXIT_OK
        width = max(len(user.email) for user in users)
        for user in users:
            factor = "2fa" if user.totp_enabled else "   "
            last = f"{user.last_login_at:%Y-%m-%d}" if user.last_login_at else "never"
            print(
                f"{user.email:<{width}}  {user.role.value:<18} {user.status.value:<9} "
                f"{factor}  {user.organisation.slug:<20} last login {last}"
            )
    return EXIT_OK


async def _no_such_user(session: AsyncSession, email: str) -> None:
    """Explain what this address is, rather than only what it is not.

    "No user with that address" reads as a typo, and the commonest reason for
    it is not one: the person was invited and has not accepted yet, so there
    is nothing to suspend or give a password to.
    """
    address = email.strip().lower()
    outstanding = await open_invites_for(session, address)
    if outstanding:
        print(
            f"{address} has been invited but has not accepted, so there is no account "
            f"to change. The invitation expires "
            f"{outstanding[0].expires_at:%Y-%m-%d %H:%M UTC}.",
            file=sys.stderr,
        )
        return
    print(f"no account for {address}; `list-users` shows the ones there are", file=sys.stderr)


async def _set_status(email: str, status: UserStatus) -> int:
    async with session_scope() as session:
        user = await get_user_by_email(session, email)
        if user is None:
            await _no_such_user(session, email)
            return EXIT_USAGE
        user.status = status
        if status is UserStatus.SUSPENDED:
            ended = await revoke_all_for_user(session, user.id)
            print(f"{user.email} suspended; {ended} session(s) ended")
        else:
            print(f"{user.email} is now {status.value}")
    return EXIT_OK


async def cmd_suspend(args: argparse.Namespace) -> int:
    return await _set_status(args.email, UserStatus.SUSPENDED)


async def cmd_activate(args: argparse.Namespace) -> int:
    return await _set_status(args.email, UserStatus.ACTIVE)


async def cmd_reset_password(args: argparse.Namespace) -> int:
    """Set a new password for an account, and end every session it holds."""
    async with session_scope() as session:
        user = await get_user_by_email(session, args.email)
        if user is None:
            await _no_such_user(session, args.email)
            return EXIT_USAGE
        password = _password(f"New password for {user.email}")
        try:
            user.password_hash = hash_password(password)
        except WeakPassword as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_USAGE
        user.password_changed_at = dt.datetime.now(dt.UTC)
        user.failed_login_count = 0
        user.locked_until = None
        ended = await revoke_all_for_user(session, user.id)
        print(f"password set for {user.email}; {ended} session(s) ended")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage_auth",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    boot = sub.add_parser("bootstrap", help="create the Inputiv organisation and first admin")
    boot.add_argument("--email", required=True, help="the first administrator's address")
    boot.add_argument("--name", default="Inputiv", help="the organisation's display name")
    boot.add_argument("--display-name", default=None, help="the administrator's name")
    boot.set_defaults(func=cmd_bootstrap)

    org = sub.add_parser("add-organisation", help="create a franchisee's organisation")
    org.add_argument("--client", required=True, help="the client slug it belongs to")
    org.add_argument("--slug", default=None, help="organisation slug (defaults to the client's)")
    org.add_argument("--name", default=None, help="display name (defaults to the client's)")
    org.set_defaults(func=cmd_add_organisation)

    invite = sub.add_parser("invite", help="create a one-time invite")
    invite.add_argument("--email", required=True)
    invite.add_argument("--role", required=True, choices=[role.value for role in Role])
    invite.add_argument(
        "--organisation",
        default=None,
        help="organisation slug; defaults to 'inputiv' for an Inputiv role",
    )
    invite.add_argument(
        "--reissue",
        action="store_true",
        help="withdraw an invitation this address already has and send a new one",
    )
    invite.set_defaults(func=cmd_invite)

    revoke = sub.add_parser("revoke-invite", help="withdraw an outstanding invitation")
    revoke.add_argument("--email", required=True)
    revoke.set_defaults(func=cmd_revoke_invite)

    listing = sub.add_parser("list-users", help="list accounts")
    listing.add_argument("--organisation", default=None, help="restrict to one organisation")
    listing.set_defaults(func=cmd_list_users)

    suspend = sub.add_parser("suspend", help="suspend an account and end its sessions")
    suspend.add_argument("--email", required=True)
    suspend.set_defaults(func=cmd_suspend)

    activate = sub.add_parser("activate", help="reinstate a suspended account")
    activate.add_argument("--email", required=True)
    activate.set_defaults(func=cmd_activate)

    reset = sub.add_parser("reset-password", help="set a new password and end every session")
    reset.add_argument("--email", required=True)
    reset.set_defaults(func=cmd_reset_password)

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
