"""Human sessions, project memberships and write-only ingestion keys."""

import hashlib
import hmac
import os
import re
import secrets
import threading
import time
import uuid
from collections import deque
from http.cookies import CookieError, SimpleCookie

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from datalog.core.validation import Invalid, project, text

COOKIE = "datalog_session"
SESSION_SECONDS = 12 * 3600
ROLES = {"admin", "editor", "viewer"}
LOGIN_PER_USER, LOGIN_PER_CLIENT, LOGIN_GLOBAL = 10, 30, 300
# Self-service project creation for any signed-in user (off by default).
OPEN_PROJECT_CREATION = os.environ.get("DATALOG_OPEN_PROJECT_CREATION", "").lower() in ("1", "true", "yes")


class AccessError(Exception):
    def __init__(self, status, message, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def username(value):
    value = text(value, "username", limit=80).lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.@+-]*", value):
        raise Invalid("Invalid user")
    return value


def integer(value, label, low, high):
    if type(value) is not int or not low <= value <= high:
        raise Invalid(f"{label}: provide an integer between {low} and {high}")
    return value


def password_bytes(value, minimum=1):
    if not isinstance(value, str):
        raise Invalid("Invalid password")
    data = value.encode()
    if not minimum <= len(data) <= 256:
        raise Invalid(f"Password: use {minimum} to 256 bytes")
    return data


def password_hash(password, salt):
    return hashlib.scrypt(password, salt=salt, n=2**17, r=8, p=1, maxmem=192 * 1024**2)


def digest(token):
    return hashlib.sha256(token.encode()).digest()


def session_token(headers, name=COOKIE):
    try:
        cookie = SimpleCookie(headers.get("Cookie", ""))
    except CookieError:
        return None
    value = cookie[name].value if name in cookie else ""
    return value if re.fullmatch(r"dl_s_[\w-]{43}", value, re.ASCII) else None


def bearer(headers):
    value = headers.get("Authorization", "")
    scheme, separator, token = value.partition(" ")
    return token if separator and scheme.lower() == "bearer" else ""


def audit(db, p, actor, action, target):
    db.execute(
        "INSERT INTO project_audit(project,actor,action,target) VALUES(%s,%s,%s,%s)",
        (p, actor or "cli", action, target),
    )


def audit_failure(db, p, actor, action, target, status):
    db.execute(
        """
        INSERT INTO project_audit(project,actor,action,target,result)
        SELECT project,username,%s,%s,%s FROM project_members WHERE project=%s AND username=%s
    """,
        # Only members are recorded: outsiders must not be able to write into another project's audit trail.
        (action, target, f'{"denied" if status == 403 else "failed"}:{status}', p, actor),
    )


class AccessStore:
    def __init__(self, connect):
        self.connect = connect
        self.login_lock = threading.Lock()
        # Sliding one-minute windows: per account, per client address and a global ceiling.
        self.login_attempts = {}
        self.hash_slots = threading.BoundedSemaphore(2)

    def throttle_login(self, name, client):
        limits = ((("user", name), LOGIN_PER_USER), (("client", client), LOGIN_PER_CLIENT), (("all",), LOGIN_GLOBAL))
        with self.login_lock:
            now = time.monotonic()
            for key in [k for k, window in self.login_attempts.items() if not window or window[-1] <= now - 60]:
                del self.login_attempts[key]
            windows = [self.login_attempts.setdefault(key, deque()) for key, _ in limits]
            for window in windows:
                while window and window[0] <= now - 60:
                    window.popleft()
            if any(len(window) >= limit for window, (_, limit) in zip(windows, limits)):
                raise AccessError(429, "Too many attempts. Try again in a moment.", 60)
            for window in windows:
                window.append(now)

    def create_user(self, name, password, adopt_existing=False):
        name = username(name)
        salt = secrets.token_bytes(16)
        hashed = password_hash(password_bytes(password, 12), salt)
        try:
            with self.connect() as db:
                db.execute("INSERT INTO datalog_users VALUES(%s,%s,%s)", (name, salt, hashed))
                if adopt_existing:
                    db.execute("INSERT INTO project_members SELECT id,%s,'admin' FROM projects", (name,))
        except UniqueViolation as exc:
            raise AccessError(409, "User already exists") from exc

    def reset_password(self, name, password):
        name = username(name)
        salt = secrets.token_bytes(16)
        hashed = password_hash(password_bytes(password, 12), salt)
        with self.connect() as db:
            if not db.execute(
                "UPDATE datalog_users SET salt=%s,password_hash=%s WHERE username=%s", (salt, hashed, name)
            ).rowcount:
                raise AccessError(404, "User not found")
            db.execute("DELETE FROM browser_sessions WHERE username=%s", (name,))
            # Project tokens created with the old password must not outlive it.
            db.execute("DELETE FROM project_tokens WHERE username=%s", (name,))

    def login(self, name, password, previous=None, client=None):
        name, password = username(name), password_bytes(password)
        self.throttle_login(name, client)
        # Wait briefly for a hashing slot instead of failing at once under concurrent sign-ins.
        if not self.hash_slots.acquire(timeout=3):
            raise AccessError(429, "Sign-in is busy. Try again in a moment.", 2)
        try:
            with self.connect() as db:
                account = db.execute(
                    "SELECT salt,password_hash FROM datalog_users WHERE username=%s", (name,)
                ).fetchone()
            hashed = password_hash(password, account["salt"] if account else bytes(16))
        finally:
            self.hash_slots.release()
        if not hmac.compare_digest(hashed, account["password_hash"] if account else bytes(64)) or account is None:
            raise AccessError(401, "Invalid username or password")
        token = "dl_s_" + secrets.token_urlsafe(32)
        with self.connect() as db:
            current = db.execute(
                "SELECT password_hash FROM datalog_users WHERE username=%s FOR SHARE", (name,)
            ).fetchone()
            if not current or not hmac.compare_digest(current["password_hash"], hashed):
                raise AccessError(401, "Invalid username or password")
            if previous:
                db.execute("DELETE FROM browser_sessions WHERE digest=%s", (digest(previous),))
            db.execute(
                "INSERT INTO browser_sessions VALUES(%s,%s,now() + %s * interval '1 second')",
                (digest(token), name, SESSION_SECONDS),
            )
        return token

    def session_user(self, token):
        if token:
            with self.connect() as db:
                row = db.execute(
                    "SELECT username FROM browser_sessions WHERE digest=%s AND expires_at>now()", (digest(token),)
                ).fetchone()
            if row:
                return row["username"]
        raise AccessError(401, "Sign in to access Datalog")

    def logout(self, token):
        if token:
            with self.connect() as db:
                db.execute("DELETE FROM browser_sessions WHERE digest=%s", (digest(token),))

    def list_projects(self, user):
        with self.connect() as db:
            return db.execute(
                """
                SELECT p.id,m.role,p.retention_days FROM project_members m JOIN projects p ON p.id=m.project
                WHERE m.username=%s ORDER BY p.id
            """,
                (user,),
            ).fetchall()

    def create_project(self, user, name):
        if name is None:
            raise Invalid("Provide the project ID")
        name = project(name)
        try:
            with self.connect() as db:
                # Only administrators of an existing project create projects, so a viewer cannot
                # gain an admin role (and wildcard data sources or model adapters) by creating one.
                if (
                    not OPEN_PROJECT_CREATION
                    and not db.execute(
                        "SELECT 1 FROM project_members WHERE username=%s AND role='admin' LIMIT 1", (user,)
                    ).fetchone()
                ):
                    raise AccessError(403, "Only project administrators can create projects")
                db.execute("INSERT INTO projects(id) VALUES(%s)", (name,))
                db.execute("INSERT INTO project_members VALUES(%s,%s,'admin')", (name, user))
        except UniqueViolation as exc:
            raise AccessError(409, "Project already exists") from exc
        return {"id": name, "role": "admin", "retention_days": 30}

    def authorize(self, p, user, permission="read"):
        with self.connect() as db:
            policy = db.execute(
                """
                SELECT p.*,m.role FROM projects p JOIN project_members m ON m.project=p.id
                WHERE p.id=%s AND m.username=%s
            """,
                (p, user),
            ).fetchone()
        if (
            not policy
            or (permission == "admin" and policy["role"] != "admin")
            or (permission == "write" and policy["role"] == "viewer")
        ):
            raise AccessError(403, "You do not have permission in this project")
        return policy

    def ingestion_policy(self, p, token):
        if not re.fullmatch(r"dl_ing_[\w-]{43}", token, re.ASCII):
            raise AccessError(401, "Invalid ingestion key")
        with self.connect() as db:
            policy = db.execute(
                """
                SELECT p.* FROM projects p JOIN ingestion_keys k ON k.project=p.id
                WHERE k.digest=%s AND k.revoked_at IS NULL
            """,
                (digest(token),),
            ).fetchone()
        if not policy:
            raise AccessError(401, "Invalid ingestion key")
        if policy["id"] != p:
            raise AccessError(403, "Key belongs to another project")
        return policy

    def reserve(self, policy, size):
        now = int(time.time())
        minute = now // 60
        if size > policy["byte_quota"]:
            raise AccessError(413, "Batch exceeds the project's byte quota")
        with self.connect() as db:
            accepted = db.execute(
                """
                INSERT INTO ingestion_quota(project,minute,requests,bytes) VALUES(%s,%s,1,%s)
                ON CONFLICT(project) DO UPDATE SET
                    minute=greatest(ingestion_quota.minute,excluded.minute),
                    requests=CASE WHEN ingestion_quota.minute<excluded.minute THEN 1 ELSE ingestion_quota.requests+1 END,
                    bytes=CASE WHEN ingestion_quota.minute<excluded.minute THEN excluded.bytes ELSE ingestion_quota.bytes+excluded.bytes END
                WHERE ingestion_quota.minute<excluded.minute OR
                      (ingestion_quota.requests<%s AND ingestion_quota.bytes+excluded.bytes<=%s)
                RETURNING project
            """,
                (policy["id"], minute, size, policy["request_quota"], policy["byte_quota"]),
            ).fetchone()
        if not accepted:
            raise AccessError(429, "Project ingestion quota reached", 60 - now % 60)

    def charge(self, policy, extra):
        """Count bytes beyond the wire size (e.g. after gzip) against the current minute's quota."""
        if extra <= 0:
            return
        with self.connect() as db:
            accepted = db.execute(
                """UPDATE ingestion_quota SET bytes=bytes+%s
                   WHERE project=%s AND minute=%s AND bytes+%s<=%s RETURNING project""",
                (extra, policy["id"], int(time.time()) // 60, extra, policy["byte_quota"]),
            ).fetchone()
        if not accepted:
            raise AccessError(429, "Project ingestion quota reached", 60 - int(time.time()) % 60)

    @staticmethod
    def lock_admin(db, p, user):
        # Serialize mutations with membership revocation and retention policy changes.
        row = db.execute("SELECT id FROM projects WHERE id=%s FOR UPDATE", (p,)).fetchone()
        member = db.execute("SELECT role FROM project_members WHERE project=%s AND username=%s", (p, user)).fetchone()
        if not row or not member or member["role"] != "admin":
            raise AccessError(403, "Project admin role required")

    def settings(self, p, user):
        policy = self.authorize(p, user, "admin")
        with self.connect() as db:
            policy["members"] = db.execute(
                "SELECT username,role FROM project_members WHERE project=%s ORDER BY username", (p,)
            ).fetchall()
            policy["tokens"] = db.execute(
                """
                SELECT id,username,capability,extract(epoch FROM expires_at)::float8 expires_at
                FROM project_tokens WHERE project=%s AND expires_at>now() ORDER BY expires_at DESC LIMIT 200
            """,
                (p,),
            ).fetchall()
            policy["keys"] = db.execute(
                """
                SELECT id,name,extract(epoch FROM created_at)::float8 created_at,
                    (revoked_at IS NOT NULL) revoked FROM ingestion_keys
                WHERE project=%s ORDER BY created_at DESC LIMIT 100
            """,
                (p,),
            ).fetchall()
        return policy

    def audit_events(self, p, user):
        self.authorize(p, user, "admin")
        with self.connect() as db:
            return {
                "items": db.execute(
                    "SELECT * FROM project_audit WHERE project=%s ORDER BY id DESC LIMIT 200", (p,)
                ).fetchall()
            }

    def create_token(self, p, user, body):
        capability = body.get("capability", "read")
        if capability not in ("read", "evaluate", "invoke"):
            raise Invalid("Invalid scope")
        self.authorize(p, user, "read" if capability == "read" else "write")
        token, ident = "dl_project_" + secrets.token_urlsafe(32), uuid.uuid4().hex
        with self.connect() as db:
            db.execute(
                "INSERT INTO project_tokens(id,project,username,digest,capability) VALUES(%s,%s,%s,%s,%s)",
                (ident, p, user, digest(token), capability),
            )
            audit(db, p, user, "token.create", ident)
        return {"id": ident, "token": token, "capability": capability}

    def revoke_token(self, p, user, body):
        ident = text(body.get("id"), "token", limit=32)
        policy = self.authorize(p, user)
        with self.connect() as db:
            deleted = db.execute(
                "DELETE FROM project_tokens WHERE project=%s AND id=%s AND (%s OR username=%s)",
                (p, ident, policy["role"] == "admin", user),
            ).rowcount
            if not deleted:
                raise AccessError(404, "Credential not found")
            audit(db, p, user, "token.revoke", ident)
        return {"revoked": True}

    def token_user(self, p, token, capability):
        if not re.fullmatch(r"dl_project_[\w-]{43}", token, re.ASCII):
            raise AccessError(401, "Invalid project credential")
        with self.connect() as db:
            row = db.execute(
                """
                SELECT t.username,t.capability,m.role FROM project_tokens t
                JOIN project_members m ON m.project=t.project AND m.username=t.username
                WHERE t.project=%s AND t.digest=%s AND t.expires_at>now()
            """,
                (p, digest(token)),
            ).fetchone()
        if not row or row["capability"] != capability or (capability != "read" and row["role"] == "viewer"):
            raise AccessError(403, "Credential has no access to this project or operation")
        return row["username"]

    def update_settings(self, p, user, body):
        days = body.get("retention_days")
        if days is not None:
            integer(days, "retention in days", 1, 3650)
        requests = integer(body.get("request_quota"), "requests/minute", 1, 60000)
        byte_quota = integer(body.get("byte_quota"), "bytes/minute", 1048576, 10737418240)
        capture = body.get("capture_content")
        attrs = body.get("allowed_attributes", [])
        if type(capture) is not bool or not isinstance(attrs, list) or len(attrs) > 100:
            raise Invalid("Provide content capture and up to 100 allowed attributes")
        attrs = sorted({text(key, "attribute", limit=150) for key in attrs})
        signal_days = body.get("signal_retention")
        if "signal_retention" in body:
            if not isinstance(signal_days, dict) or signal_days.keys() - {"logs", "metrics", "traces", "evaluations"}:
                raise Invalid("Invalid retention per signal")
            for days_for_signal in signal_days.values():
                if days_for_signal is not None:
                    integer(days_for_signal, "retention in days", 1, 3650)
        with self.connect() as db:
            self.lock_admin(db, p, user)
            db.execute(
                """
                UPDATE projects SET retention_days=%s,request_quota=%s,byte_quota=%s,
                    capture_content=%s,allowed_attributes=%s WHERE id=%s
            """,
                (days, requests, byte_quota, capture, attrs, p),
            )
            if signal_days is not None:
                db.execute("UPDATE projects SET signal_retention=%s WHERE id=%s", (Jsonb(signal_days), p))
            audit(db, p, user, "project.settings", p)
        return self.settings(p, user)

    def set_member(self, p, actor, name, role):
        name = username(name)
        if role is not None and (not isinstance(role, str) or role not in ROLES):
            raise Invalid("Invalid role")
        if actor == name:
            raise Invalid("Another administrator must change your access")
        with self.connect() as db:
            if actor is not None:
                self.lock_admin(db, p, actor)
            if not db.execute("SELECT 1 FROM datalog_users WHERE username=%s", (name,)).fetchone():
                raise AccessError(404, "User not found")
            if role is None:
                db.execute("DELETE FROM project_members WHERE project=%s AND username=%s", (p, name))
                db.execute("DELETE FROM project_tokens WHERE project=%s AND username=%s", (p, name))
            else:
                db.execute(
                    """
                    INSERT INTO project_members VALUES(%s,%s,%s)
                    ON CONFLICT(project,username) DO UPDATE SET role=excluded.role
                """,
                    (p, name, role),
                )
                if role == "viewer":
                    # Viewers keep read tokens only.
                    db.execute(
                        "DELETE FROM project_tokens WHERE project=%s AND username=%s AND capability<>'read'", (p, name)
                    )
            audit(db, p, actor, "member.remove" if role is None else f"member.{role}", name)
        return {"username": name, "role": role}

    def create_key(self, p, user, name):
        name = text(name, "key name", limit=120)
        token, ident = "dl_ing_" + secrets.token_urlsafe(32), uuid.uuid4().hex
        with self.connect() as db:
            self.lock_admin(db, p, user)
            db.execute(
                "INSERT INTO ingestion_keys(id,project,name,digest) VALUES(%s,%s,%s,%s)",
                (ident, p, name, digest(token)),
            )
            audit(db, p, user, "ingestion_key.create", ident)
        return {"id": ident, "name": name, "token": token}

    def revoke_key(self, p, user, ident):
        ident = text(ident, "key ID", limit=32)
        with self.connect() as db:
            self.lock_admin(db, p, user)
            if not db.execute(
                """
                UPDATE ingestion_keys SET revoked_at=coalesce(revoked_at,now()) WHERE project=%s AND id=%s
            """,
                (p, ident),
            ).rowcount:
                raise AccessError(404, "Key not found")
            audit(db, p, user, "ingestion_key.revoke", ident)
        return {"revoked": True}
