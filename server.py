#!/usr/bin/env python3
"""
rc-launcher v2 — secret-URL, mobile-first dashboard for TWO Claude accounts.

Per account (config-driven via accounts.json):
  - auth badge (claude auth status --json under that CLAUDE_CONFIG_DIR)
  - live rc-<id>-* remote-control sessions (Open in Claude / Close)
  - Launch new session
  - in-page login: tap link -> approve in browser -> paste code -> we send it
    to the CLI. Only the real Google account can complete this = the true lock.

Runs as an unprivileged user, bound to loopback only; your reverse proxy (nginx,
Caddy, ...) forwards the fixed /<prefix>/ path here. The long <secret> is
validated in-process (rotatable without root) and is the sole access credential.

All machine-specific knobs live in config.json (see config.example.json).
"""
import html
import json
import os
import re
import shutil
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))

_cfg = {}
_cfg_path = os.path.join(HERE, "config.json")
if os.path.exists(_cfg_path):
    _cfg = json.load(open(_cfg_path))

CLAUDE = _cfg.get("claude_bin") or shutil.which("claude") or os.path.expanduser(
    "~/.local/bin/claude")
TMUX = _cfg.get("tmux_bin") or shutil.which("tmux") or "/usr/bin/tmux"
WORKDIR = _cfg.get("workdir") or os.path.expanduser("~")
PORT = int(_cfg.get("port") or 9137)

SESSION_RE = re.compile(r"https://claude\.ai/code/session_[A-Za-z0-9]+")
# CLI prints claude.com/cai/oauth/... these days (was claude.ai/oauth/...)
OAUTH_RE = re.compile(r"https://claude\.(?:ai|com)/[^\s\"'<>]*oauth[^\s\"'<>]+")
NAME_RE = re.compile(r"^rc-[A-Za-z0-9]+-[0-9]{8}-[0-9]{6}$|^rc-[0-9]{8}-[0-9]{6}$")
ACCT_RE = re.compile(r"^[a-z0-9]+$")

LOG = os.path.join(HERE, "launches.log")
STATE = os.path.join(HERE, "sessions.json")


def _read(name):
    with open(os.path.join(HERE, name)) as f:
        return f.read().strip()


PREFIX = _read("prefix.txt")
SECRET = _read("secret.txt")
BASE = f"/{PREFIX}/{SECRET}"

ACCOUNTS = json.load(open(os.path.join(HERE, "accounts.json")))["accounts"]
ACCT_BY_ID = {a["id"]: a for a in ACCOUNTS}

# ---- auth status cache (claude auth status is ~1-2s; don't run it every load) ----
_auth_cache = {}  # id -> (epoch, dict)
AUTH_TTL = 15


def log(msg, client):
    with open(LOG, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{client}\t{msg}\n")


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save_state(s):
    tmp = STATE + ".tmp"
    json.dump(s, open(tmp, "w"), indent=2)
    os.replace(tmp, STATE)


def acct_env(acct):
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = acct["config_dir"]
    return env


def auth_status(acct, force=False):
    now = time.time()
    cached = _auth_cache.get(acct["id"])
    if cached and not force and now - cached[0] < AUTH_TTL:
        return cached[1]
    try:
        out = subprocess.run(
            [CLAUDE, "auth", "status", "--json"],
            capture_output=True, text=True, timeout=30, env=acct_env(acct),
        ).stdout
        data = json.loads(out)
    except Exception:
        data = {"loggedIn": False, "authMethod": "error"}
    _auth_cache[acct["id"]] = (now, data)
    return data


# ---------------------------- tmux helpers ----------------------------
def tmux_sessions():
    out = subprocess.run(
        [TMUX, "list-sessions", "-F", "#{session_name}\t#{session_created}"],
        capture_output=True, text=True,
    ).stdout
    res = {}
    for line in out.splitlines():
        if "\t" not in line:
            continue
        name, created = line.split("\t", 1)
        if name.startswith("rc-"):
            res[name] = int(created)
    return res


def session_acct_id(name):
    """rc-<id>-<stamp> -> id; legacy rc-<stamp> -> primary account."""
    m = re.match(r"^rc-([A-Za-z0-9]+)-[0-9]{8}-[0-9]{6}$", name)
    if m and m.group(1) in ACCT_BY_ID:
        return m.group(1)
    return ACCOUNTS[0]["id"]


def capture(name, lines=200):
    try:
        return subprocess.run(
            # -J joins wrapped lines — long URLs span multiple pane rows otherwise
            [TMUX, "capture-pane", "-t", name, "-p", "-J", "-S", f"-{lines}"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except Exception:
        return ""


def pane_url(name):
    m = SESSION_RE.search(capture(name))
    return m.group(0) if m else None


def tmux_alive(name):
    return subprocess.run([TMUX, "has-session", "-t", name]).returncode == 0


# ---------------------------- sessions ----------------------------
def launch_session(acct):
    name = f"rc-{acct['id']}-" + time.strftime("%Y%m%d-%H%M%S")
    # Claude needs a real PTY (never pipe its stdout, or it flips to --print and dies).
    cmd = (f"cd {WORKDIR} && export CLAUDE_CONFIG_DIR={acct['config_dir']} && "
           f"exec {CLAUDE} --remote-control {name} "
           f"--name {name} --permission-mode acceptEdits")
    subprocess.run([TMUX, "new", "-d", "-s", name, "bash", "-lc", cmd], check=True)
    url = None
    for _ in range(30):
        time.sleep(1)
        url = pane_url(name)
        if url or not tmux_alive(name):
            break
    st = load_state()
    st[name] = {"created": int(time.time()), "url": url, "acct": acct["id"]}
    save_state(st)
    return name


def kill_session(name):
    if not NAME_RE.match(name):
        return False
    subprocess.run([TMUX, "kill-session", "-t", name])
    st = load_state()
    st.pop(name, None)
    save_state(st)
    return True


def sessions_by_acct():
    live = tmux_sessions()
    st = load_state()
    by = {a["id"]: [] for a in ACCOUNTS}
    for name, created in live.items():
        if name.startswith("login-"):
            continue
        aid = session_acct_id(name)
        url = (st.get(name) or {}).get("url") or pane_url(name)
        if url and st.get(name, {}).get("url") != url:
            st.setdefault(name, {})["url"] = url
            save_state(st)
        by.setdefault(aid, []).append({"name": name, "created": created, "url": url})
    for aid in by:
        by[aid].sort(key=lambda r: r["created"], reverse=True)
    return by


# ---------------------------- login flow ----------------------------
def login_pane(acct):
    return f"login-{acct['id']}"


def login_start(acct):
    name = login_pane(acct)
    subprocess.run([TMUX, "kill-session", "-t", name])
    cmd = (f"export CLAUDE_CONFIG_DIR={acct['config_dir']} && "
           f"exec {CLAUDE} auth login --claudeai --email {acct['email']}")
    subprocess.run([TMUX, "new", "-d", "-s", name, "bash", "-lc", cmd], check=True)


def login_url(acct):
    if not tmux_alive(login_pane(acct)):
        return None
    m = OAUTH_RE.search(capture(login_pane(acct), 400))
    return m.group(0) if m else None


def login_send_code(acct, code):
    name = login_pane(acct)
    if not tmux_alive(name):
        return False
    code = code.strip()
    # -l sends literally (code contains '#'); then Enter.
    subprocess.run([TMUX, "send-keys", "-t", name, "-l", code])
    subprocess.run([TMUX, "send-keys", "-t", name, "Enter"])
    return True


def login_cancel(acct):
    subprocess.run([TMUX, "kill-session", "-t", login_pane(acct)])


def logout(acct):
    subprocess.run([CLAUDE, "auth", "logout"], capture_output=True, text=True,
                   timeout=30, env=acct_env(acct))
    _auth_cache.pop(acct["id"], None)


def wrong_account(acct, st):
    """Logged in, but not as the email this card is for (e.g. accidental login)."""
    return bool(st.get("loggedIn")) and (st.get("email") or "") != acct["email"]


# ---------------------------- rendering ----------------------------
FAVICON = ("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' "
           "viewBox='0 0 100 100'><text y='.9em' font-size='90'>\U0001F39B️</text></svg>")

CSS = """
*{box-sizing:border-box}
body{font:16px/1.5 system-ui,-apple-system,sans-serif;max-width:640px;margin:0 auto;
padding:18px 14px 60px;background:#0f1115;color:#e7e9ee}
h1{font-size:20px;margin:0 0 2px}.sub{color:#8b93a2;font-size:13px;margin:0 0 20px}
a{color:#c96442}
.acct{background:#141821;border:1px solid #232a36;border-radius:16px;padding:16px;margin:14px 0}
.ahead{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.email{font-weight:700;font-size:16px}
.badge{font-size:12px;font-weight:700;padding:3px 9px;border-radius:20px}
.on{background:#173a26;color:#5fd08a}.off{background:#3a1a1a;color:#ff9a8a}
.plan{color:#8b93a2;font-size:12px}
.sess{background:#0f131a;border:1px solid #232a36;border-radius:11px;padding:11px 13px;
margin:8px 0;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.sess .name{font-family:ui-monospace,monospace;font-size:13px}
.sess .age{color:#8b93a2;font-size:12px}
.spacer{flex:1;min-width:8px}
a.btn,button.btn{font:inherit;font-weight:700;border:0;border-radius:11px;
padding:12px 16px;min-height:46px;cursor:pointer;text-decoration:none;
display:inline-flex;align-items:center;justify-content:center;gap:7px}
.open{background:#c96442;color:#fff}.kill{background:#242a36;color:#ff9a8a}
.launch{background:#2f8a55;color:#fff;flex:1}
.login{background:#3a4050;color:#dfe4ee;flex:1}
.reauth{background:transparent;color:#8b93a2;font-size:12px;min-height:auto;padding:4px 8px}
.row{display:flex;gap:10px;margin-top:12px;align-items:center}
.empty{color:#6b7280;font-size:13px;padding:6px 2px}
.nourl{color:#8b93a2;font-size:12px}
form{margin:0;display:contents}
.big{display:block;width:100%;margin-top:12px}
.field{width:100%;font:15px ui-monospace,monospace;padding:14px;border-radius:11px;
border:1px solid #2a3140;background:#0f131a;color:#e7e9ee;margin:12px 0}
.hint{color:#8b93a2;font-size:13px;margin:8px 0}
.urlbox{word-break:break-all;background:#0f131a;border:1px solid #232a36;
border-radius:11px;padding:12px;font-size:13px;margin:12px 0}
.stepbtn{background:#2f8a55;color:#fff;width:100%}
"""


def _age(created):
    a = int(time.time()) - created
    return f"{a//3600}h {a%3600//60}m" if a >= 3600 else f"{a//60}m {a%60}s"


def render_dashboard():
    by = sessions_by_acct()
    cards = []
    for a in ACCOUNTS:
        st = auth_status(a)
        wrong = wrong_account(a, st)
        on = bool(st.get("loggedIn")) and not wrong
        email = html.escape(a["email"])
        plan = html.escape(st.get("subscriptionType") or "")
        if wrong:
            badge = (f'<span class="badge off">⚠ logged in as '
                     f'{html.escape(st.get("email") or "?")}</span>')
        elif on:
            badge = '<span class="badge on">● logged in</span>'
        else:
            badge = '<span class="badge off">● logged out</span>'
        planhtml = f'<span class="plan">{plan}</span>' if (on and plan) else ""

        sess_html = []
        for r in by.get(a["id"], []):
            n = html.escape(r["name"])
            if r["url"]:
                openb = (f'<a class="btn open" href="{html.escape(r["url"])}" '
                         f'target="_blank">▶ Open</a>')
            else:
                openb = '<span class="nourl">registering… refresh</span>'
            sess_html.append(f'''<div class="sess">
  <span class="name">{n}</span><span class="age">⏱ {_age(r["created"])}</span>
  <span class="spacer"></span>{openb}
  <form method="get" onsubmit="return confirm('Close {n}?')">
    <input type="hidden" name="action" value="kill"><input type="hidden" name="name" value="{n}">
    <button class="btn kill" type="submit">✕</button>
  </form>
</div>''')
        if not sess_html:
            sess_html.append('<div class="empty">No open sessions.</div>')

        if on:
            login_ctl = (f'<a class="reauth" href="?action=login&amp;acct={a["id"]}">'
                         f're-auth</a>'
                         f'<a class="reauth" href="?action=logout&amp;acct={a["id"]}" '
                         f'onclick="return confirm(\'Log out {email}?\')">log out</a>')
            launch_row = (f'<div class="row">'
                          f'<a class="btn launch" href="?action=launch&amp;acct={a["id"]}">'
                          f'➕ Launch session</a>{login_ctl}</div>')
        elif wrong:
            launch_row = (f'<div class="row">'
                          f'<a class="btn kill" href="?action=logout&amp;acct={a["id"]}">'
                          f'⏏ Log out wrong account</a>'
                          f'<a class="btn login" href="?action=login&amp;acct={a["id"]}">'
                          f'🔑 Log in {html.escape(a["label"])}</a></div>')
        else:
            launch_row = (f'<div class="row">'
                          f'<a class="btn login" href="?action=login&amp;acct={a["id"]}">'
                          f'🔑 Log in {html.escape(a["label"])}</a></div>')

        cards.append(f'''<div class="acct">
  <div class="ahead"><span class="email">{email}</span>{badge}{planhtml}</div>
  {"".join(sess_html)}
  {launch_row}
</div>''')

    return page("Remote-control sessions",
                f'<p class="sub">Claude sessions on <code>tr</code> · '
                f'<a href="?">↻ refresh</a></p>{"".join(cards)}')


def render_login(acct):
    st = auth_status(acct, force=True)
    aid = acct["id"]
    if wrong_account(acct, st):
        return page("Wrong account",
                    f'<div class="acct"><div class="ahead">'
                    f'<span class="badge off">⚠ logged in as '
                    f'{html.escape(st.get("email") or "?")}</span></div>'
                    f'<div class="hint">This slot is for <b>{html.escape(acct["email"])}</b>. '
                    f'Log the wrong account out first, then log in again.</div>'
                    f'<a class="btn kill big" href="?action=logout&amp;acct={aid}">'
                    f'⏏ Log out {html.escape(st.get("email") or "wrong account")}</a>'
                    f'<a class="btn launch big" href="?">← Back to dashboard</a></div>')
    if st.get("loggedIn"):
        return page("Logged in",
                    f'<div class="acct"><div class="ahead">'
                    f'<span class="email">{html.escape(st.get("email") or acct["email"])}</span>'
                    f'<span class="badge on">● logged in</span></div>'
                    f'<div class="hint">Login complete.</div>'
                    f'<a class="btn launch big" href="?">← Back to dashboard</a></div>')
    url = login_url(acct)
    if not url:
        return page("Starting login…",
                    f'<div class="acct"><div class="hint">Starting login for '
                    f'<b>{html.escape(acct["email"])}</b> — fetching sign-in link…</div>'
                    f'<meta http-equiv="refresh" content="2">'
                    f'<a class="btn login big" href="?view=login&amp;acct={aid}">↻ Retry</a>'
                    f'<div class="row"><a class="reauth" '
                    f'href="?action=login_cancel&amp;acct={aid}">cancel</a></div></div>')
    return page("Log in", f'''<div class="acct">
  <div class="hint">Logging in <b>{html.escape(acct["email"])}</b>. Sign in with THIS
  Google account — no other account will work.</div>
  <div class="hint"><b>1.</b> Open the sign-in link &amp; approve:</div>
  <a class="btn open big" href="{html.escape(url)}" target="_blank">🔗 Open sign-in page</a>
  <div class="urlbox">{html.escape(url)}</div>
  <div class="hint"><b>2.</b> Copy the code it shows you and paste it here:</div>
  <form method="post" action="?action=code&amp;acct={aid}">
    <input class="field" name="code" placeholder="paste code (looks like xxxx#yyyy)"
           autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false">
    <button class="btn stepbtn" type="submit">✓ Submit code</button>
  </form>
  <div class="row"><a class="reauth" href="?action=login_cancel&amp;acct={aid}">cancel</a>
  <a class="reauth" href="?view=login&amp;acct={aid}">↻ refresh link</a></div>
</div>''')


def page(title, body):
    return f'''<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<link rel=icon href="{FAVICON}"><title>{html.escape(title)}</title><style>{CSS}</style></head>
<body><h1>\U0001F39B️ {html.escape(title)}</h1>{body}</body></html>'''


REDIRECT = "<html><head><meta http-equiv=refresh content='0;url={u}'></head></html>"


# ---------------------------- HTTP ----------------------------
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(b)

    def _client(self):
        return self.headers.get("X-Forwarded-For", self.client_address[0])

    def _authed_path(self, parsed):
        return parsed.path.rstrip("/") == BASE

    def _acct(self, q):
        aid = (q.get("acct") or [""])[0]
        return ACCT_BY_ID.get(aid) if ACCT_RE.match(aid or "") else None

    def do_GET(self):
        client = self._client()
        parsed = urlparse(self.path)
        if not self._authed_path(parsed):
            log(f"DENY {parsed.path[:80]!r}", client)
            self._send(404, "<h1>404</h1>")
            return
        q = parse_qs(parsed.query)
        action = (q.get("action") or [""])[0]
        view = (q.get("view") or [""])[0]

        if action == "launch":
            acct = self._acct(q)
            if not acct:
                self._send(400, "<h1>bad acct</h1>"); return
            log(f"LAUNCH {acct['id']}", client)
            try:
                launch_session(acct)
            except Exception as e:
                log(f"ERROR {e}", client)
                self._send(500, f"<pre>{html.escape(str(e))}</pre>"); return
            self._send(200, REDIRECT.format(u=html.escape(BASE))); return

        if action == "kill":
            name = (q.get("name") or [""])[0]
            ok = kill_session(name)
            log(f"KILL {name} ok={ok}", client)
            self._send(200, REDIRECT.format(u=html.escape(BASE))); return

        if action == "login":
            acct = self._acct(q)
            if not acct:
                self._send(400, "<h1>bad acct</h1>"); return
            log(f"LOGIN-START {acct['id']}", client)
            try:
                login_start(acct)
            except Exception as e:
                self._send(500, f"<pre>{html.escape(str(e))}</pre>"); return
            self._send(200, REDIRECT.format(
                u=html.escape(f"{BASE}?view=login&acct={acct['id']}"))); return

        if action == "logout":
            acct = self._acct(q)
            if not acct:
                self._send(400, "<h1>bad acct</h1>"); return
            log(f"LOGOUT {acct['id']}", client)
            logout(acct)
            self._send(200, REDIRECT.format(u=html.escape(BASE))); return

        if action == "login_cancel":
            acct = self._acct(q)
            if acct:
                login_cancel(acct)
                log(f"LOGIN-CANCEL {acct['id']}", client)
            self._send(200, REDIRECT.format(u=html.escape(BASE))); return

        if view == "login":
            acct = self._acct(q)
            if not acct:
                self._send(400, "<h1>bad acct</h1>"); return
            self._send(200, render_login(acct)); return

        self._send(200, render_dashboard())

    def do_POST(self):
        client = self._client()
        parsed = urlparse(self.path)
        if not self._authed_path(parsed):
            log(f"DENY POST {parsed.path[:80]!r}", client)
            self._send(404, "<h1>404</h1>"); return
        q = parse_qs(parsed.query)
        action = (q.get("action") or [""])[0]
        if action != "code":
            self._send(400, "<h1>bad action</h1>"); return
        acct = self._acct(q)
        if not acct:
            self._send(400, "<h1>bad acct</h1>"); return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        code = (parse_qs(body).get("code") or [""])[0]
        ok = login_send_code(acct, code)
        log(f"LOGIN-CODE {acct['id']} sent={ok}", client)
        # Give the CLI a moment to consume the code, then bust the auth cache.
        time.sleep(3)
        _auth_cache.pop(acct["id"], None)
        self._send(200, REDIRECT.format(
            u=html.escape(f"{BASE}?view=login&acct={acct['id']}")))

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
