"""OIDC authorization-code login with pre-provisioned issuer/subject bindings."""

import base64
import hashlib
import hmac
import json
import os
import secrets
from urllib.parse import quote_plus, urlencode, urlsplit
from urllib.request import Request, build_opener

from datalog.access import AccessError, SESSION_SECONDS, digest
from datalog.alerts import NoRedirect


def https(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("OIDC requires HTTPS endpoints without credentials in the URL")
    return url


def document(request):
    with build_opener(NoRedirect).open(request, timeout=5) as response:
        raw = response.read(262145)
    if len(raw) > 262144:
        raise ValueError("OIDC document exceeds 256 KiB")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Invalid OIDC document")
    return data


class OIDC:
    cookie = "__Host-datalog_oidc"

    def __init__(self, connect, origin):
        import jwt

        self.jwt = jwt
        self.connect = connect
        self.issuer = https(os.environ["DATALOG_OIDC_ISSUER"].rstrip("/"))
        self.client_id = os.environ["DATALOG_OIDC_CLIENT_ID"]
        self.secret = os.environ["DATALOG_OIDC_CLIENT_SECRET"]
        if not origin or not self.client_id or not self.secret:
            raise ValueError("Configure a public HTTPS origin and OIDC credential")
        self.redirect = origin + "/api/auth/oidc/callback"

    def discover(self):
        data = document(self.issuer + "/.well-known/openid-configuration")
        if data.get("issuer") != self.issuer:
            raise ValueError("OIDC issuer mismatch")
        endpoints = {key: https(data[key]) for key in ("authorization_endpoint", "token_endpoint", "jwks_uri")}
        endpoints["auth_methods"] = data.get("token_endpoint_auth_methods_supported", ["client_secret_basic"])
        return endpoints

    def start(self):
        state, nonce, verifier, binding = (secrets.token_urlsafe(32) for _ in range(4))
        try:
            authorization = self.discover()["authorization_endpoint"]
        except (OSError, ValueError, KeyError) as exc:
            raise AccessError(502, "OIDC provider unavailable") from exc
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        with self.connect() as db:
            db.execute(
                "INSERT INTO oidc_flows VALUES(%s,%s,%s,%s,now()+interval '5 minutes')",
                (digest(state), verifier, nonce, digest(binding)),
            )
        query = urlencode(
            {
                "response_type": "code",
                "scope": "openid",
                "client_id": self.client_id,
                "redirect_uri": self.redirect,
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return authorization + ("&" if "?" in authorization else "?") + query, binding

    def callback(self, code, state, binding, previous):
        if not code or not state or not binding or len(code) > 4096 or len(state) > 64 or len(binding) > 64:
            raise AccessError(400, "Incomplete OIDC response")
        with self.connect() as db:
            flow = db.execute(
                """DELETE FROM oidc_flows WHERE state=%s AND binding=%s AND expires_at>now()
                RETURNING verifier,nonce""",
                (digest(state), digest(binding)),
            ).fetchone()
        if not flow:
            raise AccessError(403, "OIDC session expired or invalid")
        try:
            endpoints = self.discover()
            fields = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect,
                "code_verifier": flow["verifier"],
                "client_id": self.client_id,
            }
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            if "client_secret_basic" in endpoints["auth_methods"]:
                credentials = f"{quote_plus(self.client_id)}:{quote_plus(self.secret)}".encode()
                headers["Authorization"] = "Basic " + base64.b64encode(credentials).decode()
            elif "client_secret_post" in endpoints["auth_methods"]:
                fields["client_secret"] = self.secret
            else:
                raise ValueError("Unsupported OIDC client authentication")
            token = document(Request(endpoints["token_endpoint"], urlencode(fields).encode(), headers))["id_token"]
            header = self.jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise ValueError("Unsupported OIDC algorithm or key")
            keys = document(endpoints["jwks_uri"])["keys"]
            key = self.jwt.PyJWK.from_dict(
                next(item for item in keys if item.get("kid") == header["kid"]), algorithm="RS256"
            )
            claims = self.jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=self.client_id,
                issuer=self.issuer,
                options={"require": ["exp", "iat", "iss", "sub", "aud", "nonce"]},
            )
            if (
                not hmac.compare_digest(claims["nonce"], flow["nonce"])
                or (isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and claims.get("azp") != self.client_id)
                or ("azp" in claims and claims["azp"] != self.client_id)
            ):
                raise ValueError("OIDC nonce or client mismatch")
        except (OSError, ValueError, TypeError, KeyError, StopIteration, self.jwt.PyJWTError) as exc:
            raise AccessError(403, "OIDC identity not verified") from exc
        token = "dl_s_" + secrets.token_urlsafe(32)
        with self.connect() as db:
            identity = db.execute(
                "SELECT username FROM oidc_identities WHERE issuer=%s AND subject=%s", (self.issuer, claims["sub"])
            ).fetchone()
            if not identity:
                raise AccessError(403, "Identity is not linked to a Datalog account")
            if previous:
                db.execute("DELETE FROM browser_sessions WHERE digest=%s", (digest(previous),))
            db.execute(
                "INSERT INTO browser_sessions VALUES(%s,%s,now()+%s*interval '1 second')",
                (digest(token), identity["username"], SESSION_SECONDS),
            )
        return token
