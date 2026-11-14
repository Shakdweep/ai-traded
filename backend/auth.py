"""Invite-only accounts (private beta).

Two kinds of account:
  1. Fixed accounts from the TRADER_USERS environment variable, e.g.
         TRADER_USERS=sagar:long-password-1,friend:long-password-2
  2. Accounts people create themselves on /signup. Creating one needs the invite
     code in TRADER_INVITE_CODE; passwords are stored only as salted PBKDF2 hashes.

Set these in Render's Environment tab (never commit them). Passwords are never logged
or sent to the browser; a signed, HttpOnly session cookie is issued on login.

- Nothing configured on your own PC -> login is off (local use is unchanged).
- Nothing configured on Render      -> the site stays LOCKED (fail-safe).
"""

import base64
import hashlib
import hmac
import os
import re
import secrets
import threading
import time

from . import db

COOKIE = "aitraded_session"
SESSION_DAYS = 14
MAX_FAILS = 8            # failed logins / sign-ups allowed per IP ...
FAIL_WINDOW = 15 * 60    # ... in this many seconds
PBKDF2_ROUNDS = 200_000
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,20}$")
MIN_PASSWORD = 8

# Paths anyone can open without logging in ("/" shows the home page when logged out).
PUBLIC_PATHS = {"/", "/login", "/signup", "/robots.txt", "/favicon.ico",
                "/api/auth/login", "/api/auth/signup", "/api/auth/logout", "/api/auth/me"}


def _parse_users(raw):
    users = {}
    for item in (raw or "").split(","):
        name, sep, pw = item.strip().partition(":")
        name = name.strip().lower()
        if sep and name and pw:
            users[name] = pw
    return users


USERS = _parse_users(os.environ.get("TRADER_USERS", ""))
INVITE_CODE = os.environ.get("TRADER_INVITE_CODE", "").strip()
SIGNUP_ENABLED = bool(INVITE_CODE)
ON_HOST = bool(os.environ.get("RENDER"))  # Render sets RENDER=true on every service
ENABLED = bool(USERS) or SIGNUP_ENABLED
LOCKED = ON_HOST and not ENABLED          # hosted but no accounts or invite code configured
SECRET = (os.environ.get("TRADER_SECRET_KEY") or secrets.token_hex(32)).encode()

_fails = {}
_lock = threading.Lock()


# ---------------------------------------------------------------- accounts

def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS).hex()


def _db_user(name):
    return db.q("SELECT username, pw_hash, salt FROM users WHERE username=?", (name,), one=True)


def user_exists(name):
    return name in USERS or _db_user(name) is not None


def create_user(username, password, invite):
    """Create a self-registered account. Returns (username, None) or (None, error message)."""
    if not SIGNUP_ENABLED:
        return None, "Creating accounts is turned off. Ask the owner for a login."
    if not hmac.compare_digest((invite or "").strip().encode(), INVITE_CODE.encode()):
        return None, "That invite code isn't right."
    name = (username or "").strip().lower()
    if not USERNAME_RE.match(name):
        return None, "Username: 3–20 characters, using letters, numbers, dots, dashes or underscores."
    if len(password or "") < MIN_PASSWORD:
        return None, "Password must be at least %d characters." % MIN_PASSWORD
    if user_exists(name):
        return None, "That username is taken. Try another one."
    salt = secrets.token_bytes(16)
    db.x("INSERT INTO users (username, pw_hash, salt, created_at) VALUES (?,?,?,?)",
         (name, _hash(password, salt), salt.hex(), time.time()))
    return name, None


def check_password(user, password):
    user = (user or "").strip().lower()
    password = password or ""
    if user in USERS:
        return user if hmac.compare_digest(password.encode(), USERS[user].encode()) else None
    row = _db_user(user)
    if row:
        return user if hmac.compare_digest(_hash(password, bytes.fromhex(row["salt"])), row["pw_hash"]) else None
    _hash(password, b"0" * 16)  # same work for unknown names so timing does not reveal valid ones
    return None


# ---------------------------------------------------------------- sessions

def _sign(payload):
    return hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()


def make_token(user, now=None):
    exp = int((now or time.time()) + SESSION_DAYS * 86400)
    payload = "%s|%d" % (user, exp)
    raw = "%s|%s" % (payload, _sign(payload))
    return base64.urlsafe_b64encode(raw.encode()).decode()


def read_token(token, now=None):
    """Return the user name for a valid, unexpired token, else None."""
    if not token:
        return None
    try:
        user, exp, sig = base64.urlsafe_b64decode(token.encode()).decode().rsplit("|", 2)
        if not hmac.compare_digest(sig, _sign("%s|%s" % (user, exp))):
            return None
        if int(exp) < (now or time.time()) or not user_exists(user):
            return None  # expired, or the account no longer exists
        return user
    except Exception:  # noqa: BLE001 - any malformed cookie is simply "not logged in"
        return None


def set_session(resp, request, user):
    resp.set_cookie(COOKIE, make_token(user), max_age=SESSION_DAYS * 86400,
                    httponly=True, samesite="lax", secure=is_https(request), path="/")


def current_user(request):
    return read_token(request.cookies.get(COOKIE, ""))


# ---------------------------------------------------------------- brute-force guard

def client_ip(request):
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


def too_many_fails(ip, now=None):
    now = now or time.time()
    with _lock:
        recent = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
        _fails[ip] = recent
        return len(recent) >= MAX_FAILS


def record_fail(ip, now=None):
    with _lock:
        _fails.setdefault(ip, []).append(now or time.time())


def clear_fails(ip):
    with _lock:
        _fails.pop(ip, None)


def is_https(request):
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
