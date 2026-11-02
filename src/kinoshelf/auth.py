"""Sign-in: one admin account, argon2id hashes, session cookies, lockout, CSRF, and Cloudflare Access."""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

from .db import Db

log = logging.getLogger(__name__)
_ph = PasswordHasher()
SESSION_DAYS = 30
MAX_FAILS = 5
LOCK_SECONDS = 15 * 60


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self, db: Db):
        self.db = db
        self.setup_code: str | None = None
        if not self.has_users():
            self.new_setup_code()

    def has_users(self) -> bool:
        return self.db.one("SELECT 1 FROM users LIMIT 1") is not None

    def new_setup_code(self) -> str:
        """A one-time code printed to the log; only someone who can read the container log can create
        the first account."""
        self.setup_code = "-".join(secrets.token_hex(2).upper() for _ in range(3))
        log.warning("No account yet. Open the app and create one with the setup code: %s", self.setup_code)
        return self.setup_code

    def create_first_user(self, code: str, username: str, password: str) -> str | None:
        if self.has_users():
            return "An account already exists"
        if not self.setup_code or not hmac.compare_digest(code.strip().upper(), self.setup_code):
            return "That setup code is not right. It is printed in the container log."
        err = check_password(username, password)
        if err:
            return err
        self.db.run("INSERT INTO users(username,pw_hash,created) VALUES(?,?,?)",
                    (username.strip(), _ph.hash(password), time.time()))
        self.setup_code = None
        return None

    def set_password(self, username: str, password: str) -> str | None:
        err = check_password(username, password)
        if err:
            return err
        if self.db.one("SELECT 1 FROM users WHERE username=?", (username,)) is None:
            self.db.run("INSERT INTO users(username,pw_hash,created) VALUES(?,?,?)",
                        (username, _ph.hash(password), time.time()))
        else:
            self.db.run("UPDATE users SET pw_hash=? WHERE username=?", (_ph.hash(password), username))
            self.db.run("DELETE FROM sessions WHERE user_id=(SELECT id FROM users WHERE username=?)", (username,))
        self.setup_code = None
        return None

    # lockout ----------------------------------------------------------------
    def _locked(self, key: str) -> float:
        r = self.db.one("SELECT locked_until FROM login_fail WHERE key=?", (key,))
        return max(0.0, (r["locked_until"] or 0) - time.time()) if r else 0.0

    def _fail(self, key: str) -> None:
        now = time.time()
        r = self.db.one("SELECT count, first FROM login_fail WHERE key=?", (key,))
        count = (r["count"] + 1) if r and now - (r["first"] or now) < LOCK_SECONDS else 1
        first = r["first"] if r and count > 1 else now
        locked = now + LOCK_SECONDS if count >= MAX_FAILS else None
        self.db.run("INSERT INTO login_fail(key,count,first,locked_until) VALUES(?,?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET count=excluded.count, first=excluded.first, "
                    "locked_until=excluded.locked_until", (key, count, first, locked))

    def login(self, username: str, password: str, ip: str) -> tuple[str | None, str | None]:
        """(session token, error)."""
        keys = [f"user:{username.strip().lower()}", f"ip:{ip}"]
        wait = max(self._locked(k) for k in keys)
        if wait:
            return None, f"Too many wrong tries. Try again in {int(wait // 60) + 1} minutes."
        r = self.db.one("SELECT id, pw_hash FROM users WHERE username=?", (username.strip(),))
        ok = False
        if r is not None:
            try:
                ok = _ph.verify(r["pw_hash"], password)
            except (VerifyMismatchError, VerificationError):
                ok = False
        else:
            _ph.hash(password)  # same time either way
        if not ok:
            for k in keys:
                self._fail(k)
            return None, "Wrong name or password."
        for k in keys:
            self.db.run("DELETE FROM login_fail WHERE key=?", (k,))
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.db.run("INSERT INTO sessions(token_hash,user_id,created,expires,csrf) VALUES(?,?,?,?,?)",
                    (_h(token), r["id"], now, now + SESSION_DAYS * 86400, secrets.token_urlsafe(24)))
        self.db.run("DELETE FROM sessions WHERE expires < ?", (now,))
        return token, None

    def session(self, token: str | None) -> dict | None:
        if not token:
            return None
        r = self.db.one("SELECT s.user_id, s.csrf, s.expires, u.username FROM sessions s JOIN users u "
                        "ON u.id=s.user_id WHERE s.token_hash=?", (_h(token),))
        if r is None or r["expires"] < time.time():
            return None
        return dict(r)

    def logout(self, token: str | None) -> None:
        if token:
            self.db.run("DELETE FROM sessions WHERE token_hash=?", (_h(token),))


def check_password(username: str, password: str) -> str | None:
    if not username.strip() or len(username.strip()) > 64:
        return "Pick a name of 1 to 64 characters."
    if len(password) < 10:
        return "Use a password of at least 10 characters."
    return None


class CloudflareAccess:
    """Requests that arrive through Cloudflare must carry a valid Cloudflare Access token.

    KS_CF_ACCESS_TEAM (your-team.cloudflareaccess.com) and KS_CF_ACCESS_AUD (the application's
    audience tag) turn the check on. Without them, every request that came through Cloudflare is
    refused, so the app is never served through a tunnel without Access in front of it.
    """

    def __init__(self):
        self.team = os.environ.get("KS_CF_ACCESS_TEAM", "").strip().removeprefix("https://").rstrip("/")
        self.aud = os.environ.get("KS_CF_ACCESS_AUD", "").strip()
        self._jwks = None

    @staticmethod
    def via_cloudflare(headers) -> bool:
        return bool(headers.get("cf-ray") or headers.get("cf-connecting-ip"))

    def check(self, headers) -> str | None:
        """None when the request may go on, else the reason it may not."""
        if not self.via_cloudflare(headers):
            return None
        if not (self.team and self.aud):
            return ("This app is reached through Cloudflare, but Cloudflare Access is not set up "
                    "(KS_CF_ACCESS_TEAM, KS_CF_ACCESS_AUD).")
        token = headers.get("cf-access-jwt-assertion")
        if not token:
            return "Cloudflare Access token missing."
        try:
            import jwt

            if self._jwks is None:
                self._jwks = jwt.PyJWKClient(f"https://{self.team}/cdn-cgi/access/certs", cache_keys=True)
            key = self._jwks.get_signing_key_from_jwt(token)
            jwt.decode(token, key.key, algorithms=["RS256"], audience=self.aud,
                       issuer=f"https://{self.team}")
        except Exception as e:  # any failure means no entry
            return f"Cloudflare Access token not accepted ({type(e).__name__})."
        return None
