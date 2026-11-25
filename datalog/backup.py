"""Create a private PostgreSQL archive or restore it into an empty database."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import psycopg


def command(name):
    bindir = subprocess.check_output(["pg_config", "--bindir"], text=True).strip() if shutil.which("pg_config") else ""
    candidate = Path(bindir) / name if bindir else None
    if candidate and candidate.is_file():
        return str(candidate)
    return name


def backup(dsn, archive):
    archive = Path(archive)
    fd, tmp = tempfile.mkstemp(prefix=".datalog-backup-", dir=archive.resolve().parent)
    try:
        with os.fdopen(fd, "wb") as output:
            subprocess.run(
                [
                    command("pg_dump"),
                    "--format=custom",
                    "--no-owner",
                    "--no-privileges",
                    "--dbname",
                    "connect_timeout=10",
                ],
                stdout=output,
                env=restore_env(dsn),
                check=True,
            )
        os.link(tmp, archive)  # Never overwrite another backup.
    finally:
        os.unlink(tmp)


def restore(dsn, archive):
    with psycopg.connect(dsn) as db:
        if db.execute("""SELECT 1 FROM pg_class c JOIN pg_namespace n ON c.relnamespace=n.oid
            WHERE n.nspname NOT IN ('pg_catalog','information_schema')
            AND n.nspname NOT LIKE 'pg_%' AND c.relkind IN ('r','p','v','m') LIMIT 1""").fetchone():
            raise ValueError("Use an empty destination database for the restore")
    subprocess.run(
        [
            command("pg_restore"),
            "--single-transaction",
            "--exit-on-error",
            "--no-owner",
            "--no-privileges",
            "--dbname",
            "connect_timeout=10",
            str(archive),
        ],
        env=restore_env(dsn),
        check=True,
    )


def restore_env(dsn):
    # The positional --dbname avoids pg_restore's default of writing SQL to stdout.
    # libpq's environment keeps the database credentials out of the command line.
    from psycopg.conninfo import conninfo_to_dict

    options = conninfo_to_dict(dsn)
    mapping = {
        "dbname": "PGDATABASE",
        "host": "PGHOST",
        "hostaddr": "PGHOSTADDR",
        "port": "PGPORT",
        "user": "PGUSER",
        "password": "PGPASSWORD",
        "passfile": "PGPASSFILE",
        "sslmode": "PGSSLMODE",
        "sslrootcert": "PGSSLROOTCERT",
        "sslcert": "PGSSLCERT",
        "sslkey": "PGSSLKEY",
        "connect_timeout": "PGCONNECT_TIMEOUT",
        "target_session_attrs": "PGTARGETSESSIONATTRS",
        "options": "PGOPTIONS",
        "service": "PGSERVICE",
    }
    if options.keys() - mapping.keys():
        raise ValueError("Restore DSN contains options with no supported libpq equivalent")
    return {**os.environ, **{mapping[k]: v for k, v in options.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("backup", "restore"))
    parser.add_argument("archive")
    args = parser.parse_args()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        parser.error("Configure DATABASE_URL")
    (backup if args.action == "backup" else restore)(dsn, args.archive)


if __name__ == "__main__":
    main()
