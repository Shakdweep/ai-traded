"""Invite-only accounts (private beta).

How people join: the owner creates a single-use invite code for each friend (Members
page). Every new account waits as "pending" until the owner approves it.

How people sign in:
  * Mobile-number accounts (for now, until email is set up): name + mobile number +
    password + invite code (+ optional email), no OTP. Forgotten passwords are reset by
    the owner on the Members page, or by email code if they added an email.
  * Email accounts. Sign-up: name + email + password + invite code, then a
    6-digit code is emailed to prove the address. "Forgot password" emails a code too.
    Passwords are stored only as salted PBKDF2 hashes; codes only as keyed hashes.
  * Owner/fixed logins by username: TRADER_USERS env var, or hashed entries in
    backend/access.json (hosted site only). Older username accounts still sign in.

Set secrets in Render's Environment tab (never commit them). A signed, HttpOnly session
cookie is issued on login; changing the password signs out older sessions.

- Nothing configured on your own PC -> login is off (local use is unchanged).
- Nothing configured on Render      -> the site stays LOCKED (fail-safe).
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from . import db, mailer

COOKIE = "aitraded_session"
SESSION_DAYS = 14
MAX_FAILS = 8            # failed logins / sign-ups allowed per IP ...
FAIL_WINDOW = 15 * 60    # ... in this many seconds
PBKDF2_ROUNDS = 200_000
USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,20}$")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[a-z]{2,}$")
MIN_PASSWORD = 8
CODE_TTL = 10 * 60          # an emailed code works for 10 minutes ...
CODE_MAX_TRIES = 5          # ... and for at most 5 guesses
CODE_COOLDOWN = 60          # one new code per email per minute
CODE_MAX_PER_HOUR = 5       # and at most 5 per email per hour

# Paths anyone can open without logging in ("/" shows the home page when logged out).
PUBLIC_PATHS = {"/", "/login", "/signup", "/forgot", "/robots.txt", "/favicon.ico",
                "/api/auth/login", "/api/auth/logout", "/api/auth/me",
                "/api/auth/signup/start", "/api/auth/signup/verify", "/api/auth/signup/phone",
                "/api/auth/forgot/start", "/api/auth/forgot/verify"}


def _parse_users(raw):
    users = {}
    for item in (raw or "").split(","):
        name, sep, pw = item.strip().partition(":")
        name = name.strip().lower()
        if sep and name and pw:
            users[name] = pw
    return users


def _load_access_file(path):
    """backend/access.json holds only salted PBKDF2 hashes ("salt_hex$hash_hex"), never plain
    passwords, so it is safe to commit. Used only on the hosted site; env vars take priority."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {k.lower(): v for k, v in (data.get("users") or {}).items()}, data.get("invite") or ""
    except (OSError, ValueError):
        return {}, ""


ON_HOST = bool(os.environ.get("RENDER"))  # Render sets RENDER=true on every service
USERS = _parse_users(os.environ.get("TRADER_USERS", ""))
INVITE_CODE = os.environ.get("TRADER_INVITE_CODE", "").strip()
FILE_USERS, INVITE_HASH = _load_access_file(Path(__file__).with_name("access.json")) if ON_HOST else ({}, "")
SHARED_INVITE = bool(INVITE_CODE or INVITE_HASH)  # optional shared code, besides single-use codes
ENABLED = bool(USERS or FILE_USERS) or SHARED_INVITE
INVITE_REQUIRED = ENABLED                  # sign-ups are open (with an invite) whenever login is on
EMAIL_ENABLED = mailer.configured()        # codes can be delivered (or printed, on your own PC)
SIGNUP_ENABLED = INVITE_REQUIRED and EMAIL_ENABLED  # email sign-up (with a code)
PHONE_SIGNUP_ENABLED = INVITE_REQUIRED             # mobile sign-up (no code)
LOCKED = ON_HOST and not ENABLED          # hosted but no accounts or invite code configured
SECRET = (os.environ.get("TRADER_SECRET_KEY") or secrets.token_hex(32)).encode()

_fails = {}
_lock = threading.Lock()


# ---------------------------------------------------------------- accounts

def _hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ROUNDS).hex()


def make_hash(secret):
    """'salt_hex$hash_hex' for backend/access.json."""
    salt = secrets.token_bytes(16)
    return "%s$%s" % (salt.hex(), _hash(secret, salt))


def _matches_hash(secret, stored):
    try:
        salt_hex, digest = stored.split("$", 1)
        return hmac.compare_digest(_hash(secret, bytes.fromhex(salt_hex)), digest)
    except ValueError:
        return False


def _db_user(name):
    return db.q("SELECT username, pw_hash, salt FROM users WHERE username=?", (name,), one=True)


def get_account(login_id):
    """accounts.email holds the login id: an email address, or a mobile number like +919876543210."""
    return db.q("SELECT * FROM accounts WHERE email=?", ((login_id or "").strip().lower(),), one=True)


def is_account_id(user):
    return "@" in user or user.startswith("+")


def clean_phone(raw):
    """Indian 10-digit numbers (with or without +91 / 0) -> +91XXXXXXXXXX; other countries need +code."""
    s = re.sub(r"[\s\-().]", "", raw or "")
    if s.startswith("+"):
        rest = s[1:]
        if not rest.isdigit() or not 8 <= len(rest) <= 15:
            return None
        if rest.startswith("91") and (len(rest) != 12 or rest[2] not in "6789"):
            return None
        return "+" + rest
    if not s.isdigit():
        return None
    if len(s) == 10 and s[0] in "6789":
        return "+91" + s
    if len(s) == 11 and s[0] == "0" and s[1] in "6789":
        return "+91" + s[1:]
    if len(s) == 12 and s.startswith("91") and s[2] in "6789":
        return "+" + s
    return None


def login_id(raw):
    """What someone typed in the sign-in box -> an account id, or the plain text (owner username)."""
    raw = (raw or "").strip()
    if "@" in raw:
        return raw.lower()
    if re.fullmatch(r"[+\d\s\-().]{8,}", raw):
        return clean_phone(raw) or raw
    return raw.lower()


def user_exists(name):
    if is_account_id(name):
        return get_account(name) is not None
    return name in USERS or name in FILE_USERS or _db_user(name) is not None


def is_owner(user):
    return bool(user) and (user in USERS or user in FILE_USERS)


def display_name(user):
    if user and is_account_id(user):
        acct = get_account(user)
        return acct["name"] if acct else user
    return user


def _invite_hash(code):
    return hashlib.sha256((code or "").strip().lower().encode()).hexdigest()


def find_invite(code):
    """'shared' for the optional shared code, an invite id for an unused single-use code, else None."""
    code = (code or "").strip()
    if not code:
        return None
    if INVITE_CODE and hmac.compare_digest(code.encode(), INVITE_CODE.encode()):
        return "shared"
    if INVITE_HASH and _matches_hash(code, INVITE_HASH):
        return "shared"
    row = db.q("SELECT id FROM invites WHERE code_hash=? AND used_by IS NULL AND revoked=0",
               (_invite_hash(code),), one=True)
    return row["id"] if row else None


def _invite_ok(invite):
    return find_invite(invite) is not None


def _invite_still_free(inv):
    if inv == "shared":
        return True
    return db.q("SELECT id FROM invites WHERE id=? AND used_by IS NULL AND revoked=0", (inv,), one=True) is not None


def _consume_invite(inv, account_id, now):
    """Mark a single-use code as used by this account. False if someone else used it first."""
    if inv == "shared":
        return True
    db.x("UPDATE invites SET used_by=?, used_at=? WHERE id=? AND used_by IS NULL AND revoked=0", (account_id, now, inv))
    row = db.q("SELECT used_by FROM invites WHERE id=?", (inv,), one=True)
    return bool(row) and row["used_by"] == account_id


BAD_INVITE = "That invite code isn't right, or it has already been used. Ask the person who invited you."


def _create_pending(login, name, pw_hash, salt_hex, inv, contact_email, now):
    """Insert a pending account and use up its invite. Returns an error message or None."""
    try:
        db.x("INSERT INTO accounts(email, name, pw_hash, salt, created_at, pw_changed_at, status, contact_email, "
             "invite_id) VALUES(?,?,?,?,?,?,?,?,?)",
             (login, name, pw_hash, salt_hex, now, 0, "pending", contact_email,
              None if inv == "shared" else inv))
    except db.IntegrityError:
        return "An account with this %s already exists. Sign in instead." % ("email" if "@" in login else
                                                                             "mobile number")
    if not _consume_invite(inv, login, now):
        db.x("DELETE FROM accounts WHERE email=?", (login,))
        return BAD_INVITE
    return None


def account_status(user):
    """'active' for owners and approved accounts, 'pending' while waiting for approval, None if unknown."""
    if not user:
        return None
    if not is_account_id(user):
        return "active" if user_exists(user) else None
    acct = get_account(user)
    return (acct.get("status") or "active") if acct else None


def clean_email(email):
    email = (email or "").strip().lower()
    return email if EMAIL_RE.match(email) else None


def check_password(user, password):
    """user is an email or mobile number (accounts) or a username (owner / older accounts)."""
    user = login_id(user)
    password = password or ""
    if is_account_id(user):
        candidates = [a for a in [get_account(user)] if a]
        if not candidates and "@" in user:
            # a mobile-number account can also sign in with the optional email it added
            candidates = db.q("SELECT * FROM accounts WHERE contact_email=? ORDER BY created_at", (user,))
        for acct in candidates:
            if hmac.compare_digest(_hash(password, bytes.fromhex(acct["salt"])), acct["pw_hash"]):
                if (acct.get("status") or "active") == "active":
                    db.x("UPDATE accounts SET last_login=? WHERE email=?", (time.time(), acct["email"]))
                return acct["email"]  # the account's login id
        if not candidates:
            _hash(password, b"0" * 16)  # same work for unknown emails so timing does not reveal them
        return None
    if user in USERS:
        return user if hmac.compare_digest(password.encode(), USERS[user].encode()) else None
    if user in FILE_USERS:
        return user if _matches_hash(password, FILE_USERS[user]) else None
    row = _db_user(user)
    if row:
        return user if hmac.compare_digest(_hash(password, bytes.fromhex(row["salt"])), row["pw_hash"]) else None
    _hash(password, b"0" * 16)
    return None


# ---------------------------------------------------------------- emailed codes

def _code_hash(email, purpose, code):
    return hmac.new(SECRET, ("%s|%s|%s" % (email, purpose, code)).encode(), hashlib.sha256).hexdigest()


def _can_send(email, purpose, now):
    rows = db.q("SELECT created_at FROM email_codes WHERE email=? AND purpose=? AND created_at > ?",
                (email, purpose, now - 3600))
    if rows and now - max(r["created_at"] for r in rows) < CODE_COOLDOWN:
        return "Please wait a minute before asking for another code."
    if len(rows) >= CODE_MAX_PER_HOUR:
        return "Too many codes requested for this email. Please try again in an hour."
    return None


def _issue_code(email, purpose, name="", payload=None, now=None):
    """Store a fresh code (older unused ones stop working) and email it."""
    now = now or time.time()
    code = "%06d" % secrets.randbelow(1_000_000)
    db.x("UPDATE email_codes SET used=1 WHERE email=? AND purpose=? AND used=0", (email, purpose))
    db.x("INSERT INTO email_codes(email, purpose, code_hash, payload, created_at, expires_at) VALUES(?,?,?,?,?,?)",
         (email, purpose, _code_hash(email, purpose, code), json.dumps(payload) if payload else None,
          now, now + CODE_TTL))
    subject, text, html = mailer.code_email(code, purpose, name)
    mailer.send(email, subject, text, html)
    return code


def _check_code(email, purpose, code, now=None):
    """Returns (row, None) when the code is right, else (None, error)."""
    now = now or time.time()
    row = db.q("SELECT * FROM email_codes WHERE email=? AND purpose=? AND used=0 ORDER BY created_at DESC LIMIT 1",
               (email, purpose), one=True)
    if not row or row["expires_at"] < now:
        return None, "That code has expired. Ask for a new one."
    if row["attempts"] >= CODE_MAX_TRIES:
        return None, "Too many wrong tries. Ask for a new code."
    code = re.sub(r"\D", "", code or "")
    if not hmac.compare_digest(_code_hash(email, purpose, code), row["code_hash"]):
        db.x("UPDATE email_codes SET attempts=attempts+1 WHERE id=?", (row["id"],))
        left = CODE_MAX_TRIES - row["attempts"] - 1
        return None, ("That code isn't right. %d tries left." % left) if left > 0 else \
            "Too many wrong tries. Ask for a new code."
    db.x("UPDATE email_codes SET used=1 WHERE id=?", (row["id"],))
    return row, None


def signup_start(name, email, password, invite, now=None):
    """Check the details and email a code. Returns (masked email, None) or (None, error)."""
    if not INVITE_REQUIRED:
        return None, "Creating accounts is turned off. Ask the owner for a login."
    if not EMAIL_ENABLED:
        return None, "Email isn't set up on this site yet, so new accounts can't be created. Ask the owner."
    inv = find_invite(invite)
    if inv is None:
        return None, BAD_INVITE
    name = " ".join((name or "").split())[:40]
    if not name:
        return None, "Please enter your name."
    email = clean_email(email)
    if not email:
        return None, "Please enter a valid email address."
    if len(password or "") < MIN_PASSWORD:
        return None, "Password must be at least %d characters." % MIN_PASSWORD
    if get_account(email):
        return None, "An account with this email already exists. Sign in, or use “Forgot password”."
    now = now or time.time()
    wait = _can_send(email, "signup", now)
    if wait:
        return None, wait
    salt = secrets.token_bytes(16)
    try:
        _issue_code(email, "signup", name, {"name": name, "pw_hash": _hash(password, salt), "salt": salt.hex(),
                                            "invite": inv}, now)
    except mailer.MailError as e:
        return None, str(e)
    return mask(email), None


def signup_verify(email, code, now=None):
    """Create the account if the code is right. Returns (email, None) or (None, error)."""
    email = clean_email(email)
    if not email:
        return None, "Please enter a valid email address."
    row, err = _check_code(email, "signup", code, now)
    if err:
        return None, err
    if get_account(email):
        return email, None  # already created (e.g. double click)
    p = json.loads(row["payload"])
    inv = p.get("invite", "shared")
    if not _invite_still_free(inv):
        return None, BAD_INVITE
    err = _create_pending(email, p["name"], p["pw_hash"], p["salt"], inv, None, now or time.time())
    if err and not get_account(email):
        return None, err
    return email, None


def forgot_start(email, now=None):
    """Email a reset code if the account exists. Looks the same from outside either way."""
    if not EMAIL_ENABLED:
        return None, "Email isn't set up on this site yet. Ask the owner to reset your password."
    email = clean_email(email)
    if not email:
        return None, "Please enter a valid email address."
    acct = _account_for_reset(email)
    if acct:
        now = now or time.time()
        wait = _can_send(email, "reset", now)
        if wait:
            return None, wait
        try:
            _issue_code(email, "reset", acct["name"], None, now)
        except mailer.MailError as e:
            return None, str(e)
    return mask(email), None


def forgot_verify(email, code, new_password, now=None):
    email = clean_email(email)
    if not email:
        return None, "Please enter a valid email address."
    if len(new_password or "") < MIN_PASSWORD:
        return None, "Password must be at least %d characters." % MIN_PASSWORD
    acct = _account_for_reset(email)
    if not acct:
        return None, "That code has expired. Ask for a new one."
    _, err = _check_code(email, "reset", code, now)
    if err:
        return None, err
    salt = secrets.token_bytes(16)
    db.x("UPDATE accounts SET pw_hash=?, salt=?, pw_changed_at=? WHERE email=?",
         (_hash(new_password, salt), salt.hex(), (now or time.time()) * 1000, acct["email"]))  # ms
    return acct["email"], None


def _account_for_reset(email):
    """The account that signs in with this email, or a mobile account that added it as its email."""
    return get_account(email) or db.q("SELECT * FROM accounts WHERE contact_email=? ORDER BY created_at LIMIT 1",
                                      (email,), one=True)


def phone_signup(name, phone, password, invite, now=None, contact_email=None):
    """Create a mobile-number account (no OTP); it waits for the owner's approval.
    Returns (id, None) or (None, error)."""
    if not INVITE_REQUIRED:
        return None, "Creating accounts is turned off. Ask the owner for a login."
    inv = find_invite(invite)
    if inv is None:
        return None, BAD_INVITE
    name = " ".join((name or "").split())[:40]
    if not name:
        return None, "Please enter your name."
    phone = clean_phone(phone)
    if not phone:
        return None, "Please enter a valid mobile number (10 digits, or with a + country code)."
    if len(password or "") < MIN_PASSWORD:
        return None, "Password must be at least %d characters." % MIN_PASSWORD
    contact = None
    if (contact_email or "").strip():
        contact = clean_email(contact_email)
        if not contact:
            return None, "That email address doesn't look right. Fix it, or leave it empty."
    if get_account(phone):
        return None, "An account with this mobile number already exists. Sign in instead."
    salt = secrets.token_bytes(16)
    err = _create_pending(phone, name, _hash(password, salt), salt.hex(), inv, contact, now or time.time())
    return (None, err) if err else (phone, None)


# ---------------------------------------------------------------- owner tools (Members page)

def list_accounts():
    return db.q("SELECT a.email AS id, a.name, a.status, a.contact_email, a.created_at, a.last_login, "
                "a.approved_at, i.note AS invite_note FROM accounts a LEFT JOIN invites i ON i.id = a.invite_id "
                "ORDER BY CASE WHEN a.status='pending' THEN 0 ELSE 1 END, a.created_at DESC")


def approve(account_id, now=None):
    acct = get_account(account_id)
    if not acct:
        return False
    db.x("UPDATE accounts SET status='active', approved_at=? WHERE email=?", (now or time.time(), acct["email"]))
    return True


INVITE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no look-alikes (0/o, 1/l/i)


def create_invite(note, now=None):
    """A new single-use invite code. Only its hash is stored, so it is shown once."""
    grp = lambda: "".join(secrets.choice(INVITE_ALPHABET) for _ in range(4))  # noqa: E731
    code = "ait-%s-%s" % (grp(), grp())
    db.x("INSERT INTO invites(code_hash, hint, note, created_at) VALUES(?,?,?,?)",
         (_invite_hash(code), code[-4:], " ".join((note or "").split())[:60] or None, now or time.time()))
    return code


def list_invites():
    return db.q("SELECT id, hint, note, created_at, used_by, used_at, revoked FROM invites ORDER BY created_at DESC")


def revoke_invite(invite_id):
    row = db.q("SELECT id FROM invites WHERE id=? AND used_by IS NULL", (invite_id,), one=True)
    if not row:
        return False
    db.x("UPDATE invites SET revoked=1 WHERE id=?", (invite_id,))
    return True


def owner_reset_password(account_id, now=None):
    """Give a member a new temporary password (signs out their old sessions). Returns it, or None."""
    if not get_account(account_id):
        return None
    A = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
    temp = "".join(secrets.choice(A) for _ in range(10))
    salt = secrets.token_bytes(16)
    db.x("UPDATE accounts SET pw_hash=?, salt=?, pw_changed_at=? WHERE email=?",
         (_hash(temp, salt), salt.hex(), (now or time.time()) * 1000, account_id))
    return temp


def owner_remove(account_id):
    if not get_account(account_id):
        return False
    db.x("DELETE FROM accounts WHERE email=?", (account_id,))
    return True


def mask(email):
    local, _, domain = email.partition("@")
    return "%s%s@%s" % (local[:2], "•" * max(1, len(local) - 2), domain)


# ---------------------------------------------------------------- sessions

def _sign(payload):
    return hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()


SESSION_MS = SESSION_DAYS * 86400 * 1000


def make_token(user, now=None):
    exp_ms = int((now or time.time()) * 1000) + SESSION_MS  # milliseconds, so "signed in before the reset" is exact
    payload = "%s|%d" % (user, exp_ms)
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
        exp_ms = int(exp) if int(exp) > 10 ** 11 else int(exp) * 1000  # older cookies stored seconds
        if exp_ms < (now or time.time()) * 1000 or not user_exists(user):
            return None  # expired, or the account no longer exists
        if is_account_id(user):
            acct = get_account(user)
            if (acct.get("status") or "active") != "active":
                return None  # waiting for the owner's approval
            if exp_ms - SESSION_MS < (acct["pw_changed_at"] or 0):
                return None  # password changed since this session started
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
