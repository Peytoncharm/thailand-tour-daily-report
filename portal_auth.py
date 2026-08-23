"""Session login for the UTP partner portal (financial data, external
counterparty). All portal/login/logout routes live here; app.py only calls
init_app(app).

Feature flag: PORTAL_AUTH=on enables auth. Default off = the portal serves
exactly as before, so deploying before env vars exist breaks nothing.

Env vars (set in the Render UI by the owner — NEVER written via the Render
API; PUT /env-vars replaces the whole collection and has wiped this
service's config before):
  PORTAL_AUTH   "on" / "off" (default off)
  PORTAL_USERS  "user1:hash1,user2:hash2" (werkzeug password hashes; hashes
                contain colons, so usernames are split on the FIRST colon)
  SECRET_KEY    Flask session signing key

Audit trail: every login success/failure/block and logout is logged to
stdout as one [PORTAL-AUTH] line (ICT timestamp, username, event, IP).
Passwords are never logged. Render captures stdout as the audit log.

Brute force: >=5 failures for a username or IP within 15 minutes blocks
that username/IP until failures age out of the window. In-memory, which is
fine on this single-instance service (a restart clears it — acceptable).
"""

import logging
import os
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from flask import Blueprint, redirect, render_template_string, request, session, url_for
from werkzeug.security import check_password_hash

logger = logging.getLogger(__name__)

ICT = timezone(timedelta(hours=7))
PORTAL_TOKEN = "9h31xbcopddahpjctsklmf8s"
PORTAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "utp_portal.html")
ROBOTS_HEADER = {"X-Robots-Tag": "noindex, nofollow"}

MAX_FAILURES = 5
BLOCK_WINDOW = timedelta(minutes=15)
# Verified against an unknown username so response timing does not reveal
# which usernames exist.
_DUMMY_HASH = ("pbkdf2:sha256:600000$dummydummydummy$"
               "9a3685da2a1073c9e4dd85be1a8607b0a281a1a5d8fdcb51a24777aae32b1c93")

_failures = defaultdict(deque)  # "u:<name>" / "ip:<addr>" -> deque[utc datetime]

portal_bp = Blueprint("utp_portal", __name__)


def init_app(app):
    secret = os.environ.get("SECRET_KEY")
    if not secret:
        # Random fallback keeps the app booting without the env var, but
        # sessions then reset on every restart. Fine while PORTAL_AUTH=off.
        secret = os.urandom(32)
        logger.warning("[PORTAL-AUTH] SECRET_KEY not set - using volatile random key")
    app.secret_key = secret
    app.config.update(
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
        # Flask re-sends the cookie on each request for permanent sessions
        # (SESSION_REFRESH_EACH_REQUEST default True) => sliding 30-min expiry.
    )
    app.register_blueprint(portal_bp)


def _auth_on():
    return os.environ.get("PORTAL_AUTH", "off").strip().lower() == "on"


def _users():
    users = {}
    for entry in os.environ.get("PORTAL_USERS", "").split(","):
        entry = entry.strip()
        if ":" in entry:
            name, pw_hash = entry.split(":", 1)
            users[name.strip()] = pw_hash.strip()
    return users


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _audit(event, user):
    logger.info(
        "[PORTAL-AUTH] ts=%s event=%s user=%s ip=%s",
        datetime.now(ICT).isoformat(timespec="seconds"), event, user or "-", _client_ip(),
    )


def _blocked(keys):
    now = datetime.now(timezone.utc)
    for key in keys:
        dq = _failures[key]
        while dq and now - dq[0] > BLOCK_WINDOW:
            dq.popleft()
        if len(dq) >= MAX_FAILURES:
            return True
    return False


def _register_failure(keys):
    now = datetime.now(timezone.utc)
    for key in keys:
        _failures[key].append(now)


LOGOUT_SNIPPET = (
    '<br><a href="{href}" style="color:#5B6B7F;text-decoration:underline;">'
    "ออกจากระบบ ({user})</a>\n  "
)

LOGIN_PAGE = """<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Partner Revenue Portal — เข้าสู่ระบบ</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Sarabun:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root{--ink:#10233A;--navy:#16304E;--paper:#EDEFF2;--card:#FFFFFF;--amber:#E8A317;
    --amber-deep:#B87C06;--rule:#C9CFD8;--muted:#5B6B7F;}
  *{box-sizing:border-box;}
  html,body{margin:0;padding:0;}
  body{background:var(--paper);font-family:"Sarabun",-apple-system,"Segoe UI",sans-serif;
    color:var(--ink);font-size:15px;line-height:1.55;min-height:100vh;
    display:flex;flex-direction:column;}
  .band{background:var(--ink);padding:11px 18px;border-bottom:2px solid var(--amber);}
  .band span{font-family:"IBM Plex Mono",ui-monospace,monospace;font-size:11.5px;
    letter-spacing:2.2px;color:var(--amber);text-transform:uppercase;}
  main{flex:1;display:flex;align-items:center;justify-content:center;padding:24px 18px;}
  .card{background:var(--card);border:1px solid var(--rule);max-width:400px;width:100%;}
  .card-head{background:var(--navy);color:#fff;padding:22px 24px;}
  .card-head .eyebrow{font-family:"IBM Plex Mono",monospace;font-size:10.5px;
    letter-spacing:2.5px;color:var(--amber);text-transform:uppercase;margin-bottom:8px;}
  .card-head h1{margin:0;font-size:20px;font-weight:700;}
  .card-head p{margin:6px 0 0;color:#B9C6D6;font-size:13.5px;}
  form{padding:24px;}
  label{display:block;font-family:"IBM Plex Mono",monospace;font-size:10.5px;
    letter-spacing:1.5px;text-transform:uppercase;color:var(--muted);margin:0 0 6px;}
  input{width:100%;padding:11px 12px;margin-bottom:16px;border:1px solid var(--rule);
    font-family:"Sarabun",sans-serif;font-size:15px;background:#FBFCFD;}
  input:focus{outline:2px solid var(--amber);outline-offset:-1px;border-color:var(--amber);}
  button{width:100%;padding:12px;background:var(--ink);color:var(--amber);border:none;
    font-family:"Sarabun",sans-serif;font-size:15.5px;font-weight:700;cursor:pointer;}
  button:hover{background:var(--navy);}
  .err{background:#FBEFEA;border:1px solid #E3B7A6;border-left:4px solid #B4553A;
    color:#6E3322;padding:11px 13px;margin-bottom:16px;font-size:14px;}
  footer{padding:16px 18px;text-align:center;font-family:"IBM Plex Mono",monospace;
    font-size:10.5px;letter-spacing:1px;color:var(--muted);text-transform:uppercase;}
</style>
</head>
<body>
  <div class="band"><span>U-TAPAO INTL · PARTNER REVENUE PORTAL</span></div>
  <main>
    <div class="card">
      <div class="card-head">
        <div class="eyebrow">Restricted access</div>
        <h1>เข้าสู่ระบบ</h1>
        <p>พอร์ทัลรายได้พาร์ทเนอร์ · เฉพาะผู้ได้รับสิทธิ์เท่านั้น</p>
      </div>
      <form method="post" action="{{ action }}" autocomplete="off">
        {% if error %}<div class="err">{{ error }}</div>{% endif %}
        <label for="username">ชื่อผู้ใช้</label>
        <input type="text" id="username" name="username" autocapitalize="none" autofocus required>
        <label for="password">รหัสผ่าน</label>
        <input type="password" id="password" name="password" required>
        <button type="submit">เข้าสู่ระบบ</button>
      </form>
    </div>
  </main>
  <footer>Peyton &amp; Charmed Group · Transfer business unit</footer>
</body>
</html>"""


@portal_bp.route(f"/utp-portal/{PORTAL_TOKEN}", methods=["GET"])
def portal():
    """Static demo page for the U-Tapao Airport Authority.
    Hardcoded sample figures only — no CRM or data connection yet.
    Before wiring real data: see UTP_Portal_Build_Notes.md."""
    if _auth_on() and not session.get("portal_user"):
        return redirect(url_for("utp_portal.login"))
    with open(PORTAL_FILE, encoding="utf-8") as f:
        html = f.read()
    if _auth_on():
        snippet = LOGOUT_SNIPPET.format(
            href=url_for("utp_portal.logout"), user=session["portal_user"]
        )
        html = html.replace("</footer>", snippet + "</footer>")
    return html, 200, {**ROBOTS_HEADER, "Content-Type": "text/html; charset=utf-8"}


@portal_bp.route(f"/utp-portal/{PORTAL_TOKEN}/login", methods=["GET", "POST"])
def login():
    if not _auth_on():
        return redirect(url_for("utp_portal.portal"))
    if session.get("portal_user"):
        return redirect(url_for("utp_portal.portal"))

    error = None
    status = 200
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        keys = (f"u:{username}", f"ip:{_client_ip()}")
        if _blocked(keys):
            _audit("login_blocked", username)
            error = ("มีการพยายามเข้าสู่ระบบผิดพลาดหลายครั้ง "
                     "กรุณาลองใหม่อีกครั้งใน 15 นาที")
            status = 429
        else:
            pw_hash = _users().get(username)
            if pw_hash is None:
                check_password_hash(_DUMMY_HASH, password)  # constant-time-ish
                ok = False
            else:
                ok = check_password_hash(pw_hash, password)
            if ok:
                for key in keys:
                    _failures.pop(key, None)
                session.clear()
                session["portal_user"] = username
                session.permanent = True
                _audit("login_success", username)
                return redirect(url_for("utp_portal.portal"))
            _register_failure(keys)
            _audit("login_failed", username)
            error = "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"
            status = 401

    body = render_template_string(LOGIN_PAGE, error=error, action=url_for("utp_portal.login"))
    return body, status, {**ROBOTS_HEADER, "Content-Type": "text/html; charset=utf-8"}


@portal_bp.route(f"/utp-portal/{PORTAL_TOKEN}/logout", methods=["GET"])
def logout():
    user = session.pop("portal_user", None)
    session.clear()
    if user:
        _audit("logout", user)
    target = "utp_portal.login" if _auth_on() else "utp_portal.portal"
    return redirect(url_for(target))
