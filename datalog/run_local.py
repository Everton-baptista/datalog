#!/usr/bin/env python3
"""Start a dedicated, socket-only development PostgreSQL and Datalog."""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / ".data"
CLUSTER = DATA / "postgres"
SOCKET = DATA / "socket"


def main():
    parser = argparse.ArgumentParser(description="Datalog + dedicated local PostgreSQL")
    parser.add_argument("--port", type=int, default=4310)
    parser.add_argument("--stop-db", action="store_true")
    args = parser.parse_args()
    if not shutil.which("pg_config"):
        parser.error("PostgreSQL not found. Configure DATABASE_URL and run python -m datalog, or use compose.yaml.")
    binaries = Path(subprocess.check_output(["pg_config", "--bindir"], text=True).strip())
    if args.stop_db:
        if (CLUSTER / "PG_VERSION").exists():
            subprocess.run([str(binaries / "pg_ctl"), "-D", str(CLUSTER), "-m", "fast", "stop"], check=True)
        return
    DATA.mkdir(mode=0o700, exist_ok=True)
    SOCKET.mkdir(mode=0o700, exist_ok=True)
    SOCKET.chmod(0o700)
    if not (CLUSTER / "PG_VERSION").exists():
        subprocess.run(
            [
                str(binaries / "initdb"),
                "-D",
                str(CLUSTER),
                "--username=datalog",
                "--auth-local=trust",
                "--auth-host=scram-sha-256",
                "--encoding=UTF8",
                "--no-instructions",
            ],
            check=True,
        )
    status = subprocess.run(
        [str(binaries / "pg_ctl"), "-D", str(CLUSTER), "status"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    if status.returncode:
        # No TCP listener; access requires permission to this user's socket directory.
        options = f"-k '{SOCKET}' -h '' -p 55433"
        subprocess.run(
            [
                str(binaries / "pg_ctl"),
                "-D",
                str(CLUSTER),
                "-l",
                str(DATA / "postgres.log"),
                "-o",
                options,
                "-w",
                "start",
            ],
            check=True,
        )
    env = dict(os.environ, PGHOST=str(SOCKET), PGPORT="55433", PGUSER="datalog")
    result = subprocess.check_output(
        [str(binaries / "psql"), "-d", "postgres", "-Atc", "SELECT 1 FROM pg_database WHERE datname='datalog'"],
        env=env,
        text=True,
    ).strip()
    if result != "1":
        subprocess.run([str(binaries / "createdb"), "datalog"], env=env, check=True)
    env["DATABASE_URL"] = "postgresql://datalog@/datalog?" + urlencode({"host": str(SOCKET), "port": "55433"})
    (DATA / "database-url").write_text(env["DATABASE_URL"])
    python = REPO / ".venv" / "bin" / "python"
    python = str(python) if python.exists() else sys.executable
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO), env.get("PYTHONPATH")]))
    os.execve(python, [python, "-m", "datalog", "--port", str(args.port)], env)


if __name__ == "__main__":
    main()
