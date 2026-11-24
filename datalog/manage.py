"""Local operator commands. Access to the database is required."""

import argparse
import os
from getpass import getpass

from datalog.access import AccessError, ROLES, username
from datalog.app import Store
from datalog.retention import sweep
from datalog.validation import Invalid, project, text
from datalog.sso import https


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-user")
    create.add_argument("username")
    create.add_argument(
        "--adopt-existing", action="store_true", help="Give this user admin access to all existing projects"
    )
    reset = commands.add_parser("reset-password")
    reset.add_argument("username")
    grant = commands.add_parser("grant")
    grant.add_argument("project")
    grant.add_argument("username")
    grant.add_argument("role", choices=sorted(ROLES))
    identity = commands.add_parser("link-oidc")
    identity.add_argument("issuer")
    identity.add_argument("subject")
    identity.add_argument("username")
    commands.add_parser("sweep")
    args = parser.parse_args()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        parser.error("Configure DATABASE_URL")
    store = Store(dsn)
    try:
        if args.command in {"create-user", "reset-password"}:
            password = getpass("Password (at least 12 bytes): ")
            if password != getpass("Repeat the password: "):
                parser.error("Passwords do not match")
            if args.command == "create-user":
                store.access.create_user(args.username, password, args.adopt_existing)
            else:
                store.access.reset_password(args.username, password)
        elif args.command == "grant":
            p = project(args.project)
            with store.connect() as db:
                if not db.execute("SELECT 1 FROM projects WHERE id=%s", (p,)).fetchone():
                    parser.error("Project not found")
            store.access.set_member(p, None, args.username, args.role)
        elif args.command == "link-oidc":
            account = username(args.username)
            with store.connect() as db:
                if not db.execute("SELECT 1 FROM datalog_users WHERE username=%s", (account,)).fetchone():
                    parser.error("User not found")
                if not db.execute(
                    "INSERT INTO oidc_identities VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                    (https(args.issuer.rstrip("/")), text(args.subject, "subject", limit=255), account),
                ).rowcount:
                    parser.error("Identity already linked")
        else:
            print(sweep(store.connect))
    except (Invalid, AccessError) as exc:
        parser.error(str(exc))
    finally:
        store.close()


if __name__ == "__main__":
    main()
