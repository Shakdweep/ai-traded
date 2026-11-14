"""Sends the sign-up and password-reset codes by email.

Configure ONE of these with environment variables (in Render -> Environment, never in code):

  Resend (needs a verified sending domain):
      RESEND_API_KEY=re_...            MAIL_FROM="AI Traded <no-reply@your-domain>"
  Any SMTP server, e.g. a Gmail account made just for AI Traded with an App Password:
      SMTP_HOST=smtp.gmail.com  SMTP_PORT=465  SMTP_USER=you@gmail.com  SMTP_PASS=<app password>
      MAIL_FROM="AI Traded <you@gmail.com>"   (optional; defaults to SMTP_USER)

On your own PC with nothing configured, codes are printed in the server window instead
(handy for testing). On the hosted site with nothing configured, email features are off.
"""

import json
import os
import smtplib
import ssl
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465") or 465)
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASS = os.environ.get("SMTP_PASS", "").replace(" ", "")  # Gmail shows app passwords in groups of 4
MAIL_FROM = os.environ.get("MAIL_FROM", "").strip() or (formataddr(("AI Traded", SMTP_USER)) if SMTP_USER else "")
ON_HOST = bool(os.environ.get("RENDER"))

PROVIDER = "resend" if (RESEND_API_KEY and MAIL_FROM) else "smtp" if (SMTP_HOST and SMTP_USER and SMTP_PASS) else \
    ("console" if not ON_HOST else None)


class MailError(Exception):
    pass


def configured():
    return PROVIDER is not None


def _send_resend(to, subject, text, html):
    req = urllib.request.Request(
        "https://api.resend.com/emails", method="POST",
        data=json.dumps({"from": MAIL_FROM, "to": [to], "subject": subject, "text": text, "html": html}).encode(),
        headers={"Authorization": "Bearer " + RESEND_API_KEY, "Content-Type": "application/json",
                 "User-Agent": "ai-traded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            if r.status >= 300:
                raise MailError("Resend returned HTTP %d" % r.status)
    except urllib.error.HTTPError as e:
        raise MailError("Resend returned HTTP %d" % e.code) from None


def _send_smtp(to, subject, text, html):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = MAIL_FROM, to, subject
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    ctx = ssl.create_default_context()
    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=20) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.starttls(context=ctx)
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)


def send(to, subject, text, html):
    if PROVIDER == "console":
        print("\n[mail] (not sent: no email service configured on this PC)\n  To: %s\n  Subject: %s\n  %s\n"
              % (to, subject, text.replace("\n", "\n  ")), flush=True)
        return
    if PROVIDER is None:
        raise MailError("Email isn't set up on this site yet.")
    try:
        (_send_resend if PROVIDER == "resend" else _send_smtp)(to, subject, text, html)
    except MailError:
        raise
    except Exception as e:  # noqa: BLE001 - network/auth problems become one clear error
        raise MailError("Could not send the email (%s)." % type(e).__name__) from None


def code_email(code, purpose, name=""):
    """(subject, text, html) for a 6-digit code."""
    what = "create your AI Traded account" if purpose == "signup" else "reset your AI Traded password"
    hello = "Hi %s," % name if name else "Hi,"
    subject = "%s is your AI Traded code" % code
    text = ("%s\n\nUse this code to %s:\n\n    %s\n\nIt expires in 10 minutes. If you didn't ask for this, "
            "you can ignore this email.\n\nAI Traded (private beta, paper trading only)" % (hello, what, code))
    safe_hello = hello.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = """<div style="font-family:Arial,Helvetica,sans-serif;max-width:480px;margin:0 auto;padding:24px;color:#0f1720">
  <div style="font-weight:800;font-size:18px;margin-bottom:20px">AI Traded</div>
  <p style="margin:0 0 12px">%s</p>
  <p style="margin:0 0 20px">Use this code to %s:</p>
  <div style="font-size:32px;font-weight:800;letter-spacing:8px;padding:16px 20px;background:#eef1f5;
       border-radius:10px;text-align:center">%s</div>
  <p style="margin:20px 0 0;color:#566578;font-size:14px">It expires in 10 minutes. If you didn't ask for this,
     you can ignore this email.</p>
  <p style="margin:28px 0 0;color:#8796a8;font-size:12px">AI Traded · private beta · paper trading only</p>
</div>""" % (safe_hello, what, code)
    return subject, text, html


def describe():
    """Safe-to-show status (never the keys)."""
    sender = parseaddr(MAIL_FROM)[1] if MAIL_FROM else None
    return {"provider": PROVIDER or "not configured", "from": sender}
