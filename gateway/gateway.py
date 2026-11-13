#!/usr/bin/env python3
"""
RyvenPanel Team-Gateway

* Login: username + password. Accounts are created by the owner and stored only on this server
  (/var/lib/ryvenpanel/gateway.db, passwords as salted PBKDF2-SHA256 hashes).
* Rights: either the LuckPerms rank of a linked Minecraft player (incl. inheritance and temporary groups)
  or a fixed rank chosen by the owner – mapped to panel permissions configured by the owner.
* IP binding: an account is bound to the IP of its first login; other IPs are rejected and queued for the owner.
* Proxy: only permitted Crafty API calls are forwarded (with the owner's Crafty token, never handed out).

Owner administration happens locally via SSH:  python3 gateway.py admin <command> [base64-json | - (stdin)]
"""
import base64, hashlib, hmac, http.server, json, os, re, secrets, socketserver, sqlite3, ssl, subprocess, sys, threading, time
import urllib.error, urllib.request

CONF = "/etc/ryvenpanel/gateway.json"
DB = "/var/lib/ryvenpanel/gateway.db"
VERSION = "2.4"
SCHEMA = 2

PERMS = [
    ("servers.view", "Server & Status sehen"),
    ("console.view", "Konsole lesen"),
    ("console.command", "Konsolenbefehle senden (mit Sperrliste)"),
    ("console.command.unrestricted", "Alle Konsolenbefehle (ohne Sperrliste)"),
    ("server.control", "Server starten / stoppen / neu starten"),
    ("server.kill", "Server hart beenden (Kill)"),
    ("server.backup", "Backups starten"),
    ("files.view", "Server-Dateien ansehen"),
    ("files.edit", "Server-Dateien bearbeiten / hochladen / löschen"),
    ("economy.view", "Wirtschaft ansehen (Geldfluss, Shop, Crates)"),
    ("economy.edit", "Shop-Preise ändern / Items hinzufügen"),
    ("moderation.view", "Moderation ansehen (Reports, Strafen, Verify)"),
    ("moderation.reports", "Reports bearbeiten (übernehmen / erledigen / ablehnen)"),
    ("moderation.punish", "Verwarnen, Kicken, Muten / Entmuten"),
    ("moderation.ban", "Bannen / Entbannen"),
]
ECO_PY = "/opt/ryvenpanel/eco.py"
MOD_PY = "/opt/ryvenpanel/mod.py"
MOD_VIEW = ("overview", "reports", "report", "punishments", "links", "player", "templates")
MOD_ACT = {"warn": "moderation.punish", "kick": "moderation.punish", "mute": "moderation.punish", "unmute": "moderation.punish",
           "ban": "moderation.ban", "ipban": "moderation.ban", "unban": "moderation.ban"}
DIST = "/opt/ryvenpanel/dist"  # panel releases (RyvenPanel.exe + version.json), published by the owner's panel
ECO_CMDS = {"summary": "economy.view", "items": "economy.view", "setprice": "economy.edit", "additem": "economy.edit", "reload": "economy.edit", "settax": "economy.edit"}
PERM_KEYS = [p[0] for p in PERMS]

DEFAULT_BLOCKED = [
    "op", "deop", "stop", "restart", "reload", "rl", "lp", "luckperms", "lpb", "lpv", "perm", "perms", "pex",
    "eco", "economy", "points", "p", "give", "giveall", "i", "item", "whitelist", "ban-ip", "pardon-ip", "sudo",
    "plugman", "execute", "save-off", "kill", "killall", "butcher", "worldedit", "we", "/", "//", "sk", "skript",
    "gamemode", "gm", "gmc", "gms", "fly", "god", "vanish", "v", "money", "pay", "mobcoins", "crate", "key",
]

DEFAULT_ROLES = {
    "owner": PERM_KEYS[:],
    "admin": ["servers.view", "console.view"],
}

UUID_RX = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SID_RX = re.compile(r"^/api/v2/servers/([0-9a-f-]{36})(/.*)?$")
USER_RX = re.compile(r"^[a-z0-9_.\-]{3,32}$")
MC_RX = re.compile(r"^[A-Za-z0-9_]{3,16}$")
ROLE_RX = re.compile(r"^[a-z0-9_\-]{1,36}$")

PW_ITER = 310000
MAX_FAILS = 5          # wrong passwords before the account is locked
LOCK_SECONDS = 900     # lock duration

lock = threading.RLock()
_conf = None
_conf_mtime = 0
login_hits = {}


# ───────────────────────────── config / db ─────────────────────────────
def conf():
    global _conf, _conf_mtime
    with lock:
        try:
            m = os.path.getmtime(CONF)
        except OSError:
            m = 0
        if _conf is None or m != _conf_mtime:
            try:
                with open(CONF, encoding="utf-8") as f:
                    _conf = json.load(f)
            except (OSError, ValueError):
                _conf = {}
            _conf_mtime = m
            _conf.setdefault("port", 8444)
            _conf.setdefault("roles", DEFAULT_ROLES)
            _conf.setdefault("blocked_commands", DEFAULT_BLOCKED)
            _conf.setdefault("session_hours", 12)
            _conf.setdefault("crafty_url", "https://127.0.0.1:8443")
            _conf.pop("ms_client_id", None)
        return _conf


def save_conf(c):
    global _conf
    with lock:
        c = dict(c)
        c.pop("ms_client_id", None)
        tmp = CONF + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(c, f, indent=2, ensure_ascii=False)
        os.replace(tmp, CONF)
        _conf = None


def db():
    con = sqlite3.connect(DB, timeout=15)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    with db() as con:
        ver = con.execute("pragma user_version").fetchone()[0]
        if ver < 2:
            # v1 used Microsoft/Minecraft logins keyed by UUID – those accounts and sessions are obsolete.
            con.executescript("drop table if exists accounts; drop table if exists sessions; drop table if exists ip_requests;")
        con.executescript("""
        create table if not exists users (username text primary key, pw text, mc_name text default '', mc_uuid text default '',
            role text default '', ips text default '[]', frozen int default 0, must_change int default 1, fails int default 0,
            locked_until real default 0, created real, last_login real default 0, last_ip text default '', rank text default '',
            note text default '');
        create table if not exists ip_requests (id integer primary key autoincrement, username text, ip text, time real,
            status text default 'pending');
        create table if not exists sessions (token_hash text primary key, username text, rank text, ip text, perms text,
            created real, expires real, last_seen real);
        create table if not exists audit (id integer primary key autoincrement, time real, uuid text, name text, ip text,
            action text, detail text, result text);
        """)
        con.execute("pragma user_version=%d" % SCHEMA)
    try:
        os.chmod(DB, 0o600)
    except OSError:
        pass


def audit(user, ip, action, detail="", result="ok", con=None):
    """Writes an audit entry. Pass the open connection when called inside a write transaction
    (a second connection would wait for that transaction's lock)."""
    def write(c):
        c.execute("insert into audit (time, uuid, name, ip, action, detail, result) values (?,?,?,?,?,?,?)",
                  (time.time(), user or "", user or "", ip, action, (detail or "")[:500], result))
        c.execute("delete from audit where id < (select max(id) - 20000 from audit)")
    try:
        if con is not None:
            write(con)
        else:
            with db() as c:
                write(c)
    except sqlite3.Error:
        pass


def thash(token):
    return hashlib.sha256(token.encode()).hexdigest()


# ───────────────────────────── passwords ─────────────────────────────
def hash_pw(pw):
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, PW_ITER)
    return "pbkdf2_sha256$%d$%s$%s" % (PW_ITER, salt.hex(), h.hex())


def check_pw(pw, stored):
    try:
        algo, it, salt, h = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), int(it))
        return hmac.compare_digest(calc.hex(), h)
    except (ValueError, TypeError):
        return False


_DUMMY = None


def dummy_hash():
    """Hash used for unknown usernames so the response time does not reveal whether an account exists."""
    global _DUMMY
    if _DUMMY is None:
        _DUMMY = hash_pw(secrets.token_hex(8))
    return _DUMMY


def pw_problem(pw):
    if not isinstance(pw, str) or len(pw) < 8:
        return "Das Passwort muss mindestens 8 Zeichen lang sein."
    if len(pw) > 200:
        return "Das Passwort ist zu lang."
    if pw.lower() == pw or pw.upper() == pw or not re.search(r"\d", pw):
        if len(pw) < 14:
            return "Das Passwort braucht Groß- und Kleinbuchstaben und eine Zahl (oder mindestens 14 Zeichen)."
    return None


# ───────────────────────────── LuckPerms ─────────────────────────────
def lp_settings():
    path = conf().get("luckperms_config")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"^data:[^\n]*\n((?:[ \t#][^\n]*\n|[ \t]*\n)*)", text, re.M)
    sec = m.group(1) if m else ""

    def v(key):
        mm = re.search(r"^[ \t]+" + re.escape(key) + r":[ \t]*['\"]?([^'\"\n#]*)['\"]?", sec, re.M)
        return mm.group(1).strip() if mm else ""

    host = v("address").split(":")[0]
    names = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=20).stdout.split()
    container = host if host in names else next((n for n in names if re.search("mariadb|mysql", n, re.I)), None)
    if not container:
        raise RuntimeError("Datenbank-Container nicht gefunden")
    return container, v("username"), v("password"), v("database"), (v("table-prefix") or "luckperms_")


def lp_query(sql):
    container, user, pw, database, prefix = lp_settings()
    sql = sql.replace("{p}", prefix)
    r = subprocess.run(["docker", "exec", "-i", "-e", "MYSQL_PWD=" + pw, container, "sh", "-c",
                        'exec $(command -v mariadb || command -v mysql) -u"$0" -D "$1" -N -B', user, database],
                       input=sql, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError("LuckPerms-Abfrage fehlgeschlagen: " + r.stderr.strip()[-200:])
    return [line.split("\t") for line in r.stdout.splitlines() if line]


def lp_groups():
    rows = lp_query("select 'R', name, '' from {p}groups;\n"
                    "select 'W', name, substring(permission,8) from {p}group_permissions where permission like 'weight.%';")
    groups = {}
    for r in rows:
        kind, name, val = (r + ["", "", ""])[:3]
        groups.setdefault(name, 0)
        if kind == "W":
            try:
                groups[name] = int(val)
            except ValueError:
                pass
    return groups


def lp_player(name):
    """Minecraft name -> (uuid, name) from the LuckPerms player table (player must have joined once)."""
    if not MC_RX.match(name or ""):
        return None
    rows = lp_query("select uuid, username from {p}players where username='%s' limit 1;" % name.lower())
    if not rows or not UUID_RX.match(rows[0][0]):
        return None
    return rows[0][0], (rows[0][1] if len(rows[0]) > 1 else name)


def lp_rank(uuid):
    """Returns (rank, perms) – rank = highest-weight group of the player that has a panel role."""
    if not UUID_RX.match(uuid):
        return None, []
    now = int(time.time())
    rows = lp_query(
        "select 'P', primary_group, 0 from {p}players where uuid='%s';\n"
        "select 'G', substring(permission,7), expiry from {p}user_permissions where uuid='%s' and permission like 'group.%%' "
        "and value=1 and server in ('global','') and world in ('global','');\n"
        "select 'W', name, substring(permission,8) from {p}group_permissions where permission like 'weight.%%';\n"
        "select 'I', name, substring(permission,7) from {p}group_permissions where permission like 'group.%%' and value=1;"
        % (uuid, uuid))
    mine, weights, parents = {"default"}, {}, {}
    for r in rows:
        r = (r + ["", "", ""])[:3]
        if r[0] == "P" and r[1]:
            mine.add(r[1])
        elif r[0] == "G":
            try:
                exp = int(r[2] or 0)
            except ValueError:
                exp = 0
            if exp == 0 or exp > now:
                mine.add(r[1])
        elif r[0] == "W":
            try:
                weights[r[1]] = int(r[2])
            except ValueError:
                pass
        elif r[0] == "I":
            parents.setdefault(r[1], set()).add(r[2])
    todo, seen = list(mine), set()
    while todo:
        g = todo.pop()
        if g in seen:
            continue
        seen.add(g)
        todo.extend(parents.get(g, ()))
    roles = conf().get("roles", {})
    best = None
    for g in seen:
        if roles.get(g) and (best is None or weights.get(g, 0) > weights.get(best, 0)):
            best = g
    if best is None:
        return None, []
    return best, [p for p in roles[best] if p in PERM_KEYS]


def resolve_rank(u):
    """Rank + perms of an account: LuckPerms rank of the linked player, otherwise the fixed rank."""
    if u["mc_uuid"]:
        return lp_rank(u["mc_uuid"])
    role = u["role"] or ""
    ps = conf().get("roles", {}).get(role)
    if not ps:
        return None, []
    return role, [p for p in ps if p in PERM_KEYS]


def refresh_session(token_hash, username, old_rank, old_perms):
    """Re-reads rank + rights of a running session (rights edited by the owner, LuckPerms rank changed) and stores them.
    Falls back to the stored values if LuckPerms cannot be read right now."""
    try:
        with db() as con:
            u = con.execute("select * from users where username=?", (username,)).fetchone()
        if u is None:
            return old_rank, old_perms
        rank, perms = resolve_rank(u)
    except Exception:
        return old_rank, old_perms
    rank = rank or ""
    if rank != old_rank or perms != old_perms:
        with db() as con:
            con.execute("update sessions set rank=?, perms=? where token_hash=?", (rank, json.dumps(perms), token_hash))
            if rank:
                con.execute("update users set rank=? where username=?", (rank, username))
    return rank, perms


# ───────────────────────────── permission mapping ─────────────────────────────
def required_perm(method, path):
    base = path.split("?", 1)[0]
    if base == "/api/v2/servers":
        return "servers.view" if method == "GET" else None
    m = SID_RX.match(base)
    if not m:
        return None
    rest = m.group(2) or ""
    if rest in ("", "/stats", "/history") and method == "GET":
        return "servers.view"
    if rest == "/logs" and method == "GET":
        return "console.view"
    if rest == "/stdin" and method == "POST":
        return "console.command"
    if rest.startswith("/action/") and method == "POST":
        act = rest.split("/")[2]
        if act in ("start_server", "stop_server", "restart_server"):
            return "server.control"
        if act == "kill_server":
            return "server.kill"
        if act == "backup_server":
            return "server.backup"
        return None
    if rest == "/backups" and method == "GET":
        return "server.backup"
    if rest == "/files":
        return "files.view" if method == "POST" else ("files.edit" if method in ("PATCH", "DELETE", "PUT") else None)
    if rest.startswith("/files/") and method in ("PUT", "PATCH", "POST"):
        return "files.edit"
    return None


def command_allowed(cmd, perms):
    if "\n" in cmd or "\r" in cmd:
        return False
    if "console.command.unrestricted" in perms:
        return True
    first = cmd.strip().lstrip("/").split(" ", 1)[0].lower()
    if ":" in first:
        first = first.split(":", 1)[1]
    return first not in [b.lower() for b in conf().get("blocked_commands", [])]


# ───────────────────────────── HTTP ─────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "RyvenPanelGateway/" + VERSION
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    @property
    def ip(self):
        ip = self.client_address[0]
        return ip[7:] if ip.startswith("::ffff:") else ip

    def body(self, limit=300 * 1024 * 1024):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("Anfrage zu groß")
        self._body_read = True
        return self.rfile.read(n) if n > 0 else b""

    def drain(self):
        """Reads an unread (small) request body – closing with unread data makes the client see a reset instead of our error."""
        if getattr(self, "_body_read", False):
            return
        try:
            if int(self.headers.get("Content-Length") or 0) <= 4 * 1024 * 1024:
                self.body()
        except Exception:
            pass

    def send_json(self, code, obj, extra=None):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, fp):
        try:
            with open(fp, "rb") as f:
                data = f.read()
        except OSError:
            return self.err(404, "NOT_FOUND", "Datei nicht gefunden")
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def err(self, code, error, msg):
        self.drain()
        self.send_json(code, {"status": "error", "error": error, "error_data": msg})

    def do_GET(self): self.route("GET")
    def do_POST(self): self.route("POST")
    def do_PUT(self): self.route("PUT")
    def do_PATCH(self): self.route("PATCH")
    def do_DELETE(self): self.route("DELETE")

    def rate_limited(self, kind="login"):
        now = time.time()
        key = kind + "|" + self.ip
        with lock:
            hits = [t for t in login_hits.get(key, []) if t > now - 300]
            hits.append(now)
            login_hits[key] = hits
            if len(login_hits) > 5000:  # forget idle entries
                for k in [k for k, v in login_hits.items() if not v or v[-1] < now - 300]:
                    del login_hits[k]
        return len(hits) > 10

    def session(self):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        h = thash(auth[7:].strip())
        now = time.time()
        with db() as con:
            s = con.execute("select s.*, u.frozen, u.must_change from sessions s join users u on u.username = s.username "
                            "where s.token_hash=?", (h,)).fetchone()
            if not s or s["expires"] < now:
                return None
            if s["ip"] != self.ip:
                con.execute("delete from sessions where token_hash=?", (h,))
                audit(s["username"], self.ip, "Sitzung von fremder IP benutzt", "gebunden an " + s["ip"], "blockiert", con=con)
                return None
            if s["frozen"]:
                return None
            con.execute("update sessions set last_seen=? where token_hash=?", (now, h))
        return s

    def me(self, s, perms, rank=None):
        return {"username": s["username"], "uuid": s["username"], "rank": rank if rank is not None else s["rank"], "perms": perms,
                "all_perms": PERMS, "must_change": bool(s["must_change"])}

    def route(self, method):
        try:
            path = self.path
            if path == "/panel/config" and method == "GET":
                return self.send_json(200, {"status": "ok", "data": {"auth": "password", "version": VERSION}})
            if path == "/panel/login" and method == "POST":
                return self.login()
            # panel updates (published by the owner into DIST) – needed before login, so no session required
            if path == "/panel/update" and method == "GET":
                try:
                    with open(os.path.join(DIST, "version.json"), encoding="utf-8") as f:
                        info = json.load(f)
                except (OSError, ValueError):
                    return self.err(404, "NO_UPDATE", "Keine Version veröffentlicht.")
                return self.send_json(200, {"status": "ok", "data": info})
            if path == "/panel/update/RyvenPanel.exe" and method == "GET":
                return self.send_file(os.path.join(DIST, "RyvenPanel.exe"))
            s = self.session()
            if s is None:
                return self.err(401, "ACCESS_DENIED", "Sitzung ungültig oder abgelaufen – bitte neu anmelden.")
            perms = json.loads(s["perms"])
            if path == "/panel/logout" and method == "POST":
                with db() as con:
                    con.execute("delete from sessions where token_hash=?", (s["token_hash"],))
                audit(s["username"], self.ip, "Abmeldung")
                return self.send_json(200, {"status": "ok"})
            if path in ("/panel/me", "/api/v2/users/@me") and method == "GET":
                # the panel polls this – always answer with the current rights, not the ones from login time
                rank, perms = refresh_session(s["token_hash"], s["username"], s["rank"], perms)
                return self.send_json(200, {"status": "ok", "data": self.me(s, perms, rank)})
            if path == "/panel/password" and method == "POST":
                return self.change_password(s)
            if s["must_change"]:
                return self.err(403, "PASSWORD_CHANGE", "Bitte zuerst ein eigenes Passwort festlegen.")
            if path == "/panel/eco" and method == "POST":
                return self.eco(s, perms)
            if path == "/panel/mod" and method == "POST":
                return self.mod(s, perms)
            if path.startswith("/api/v2/"):
                return self.proxy(method, s, perms)
            return self.err(404, "NOT_FOUND", "Unbekannter Pfad")
        except Exception as e:  # never leak a stack trace
            try:
                self.err(500, "GATEWAY_ERROR", str(e)[:300])
            except Exception:
                pass

    def login(self):
        ip = self.ip
        now = time.time()
        if self.rate_limited():
            return self.err(429, "RATE_LIMIT", "Zu viele Anmeldeversuche – bitte 5 Minuten warten.")
        try:
            data = json.loads(self.body(65536) or b"{}")
        except ValueError:
            return self.err(400, "BAD_REQUEST", "Ungültige Anfrage.")
        username = str(data.get("username", "")).strip().lower()
        password = str(data.get("password", ""))
        bad = "Benutzername oder Passwort falsch."
        with db() as con:
            u = con.execute("select * from users where username=?", (username,)).fetchone() if USER_RX.match(username) else None
            if u is None:
                check_pw(password, dummy_hash())
                audit(username[:40], ip, "Login fehlgeschlagen", "unbekannter Benutzer", "abgelehnt", con=con)
                return self.err(403, "LOGIN_FAILED", bad)
            if u["locked_until"] and u["locked_until"] > now:
                audit(username, ip, "Login eines vorübergehend gesperrten Accounts", "", "abgelehnt", con=con)
                return self.err(403, "LOCKED", "Zu viele falsche Passwörter – der Account ist für %d Minuten gesperrt."
                                % max(1, int((u["locked_until"] - now) / 60 + 0.99)))
            if not check_pw(password, u["pw"]):
                fails = (u["fails"] or 0) + 1
                until = now + LOCK_SECONDS if fails >= MAX_FAILS else 0
                con.execute("update users set fails=?, locked_until=? where username=?", (0 if until else fails, until, username))
                audit(username, ip, "Falsches Passwort", "Account für 15 Min gesperrt" if until else "Versuch %d/%d" % (fails, MAX_FAILS), "abgelehnt", con=con)
                return self.err(403, "LOGIN_FAILED", bad)
            con.execute("update users set fails=0, locked_until=0 where username=?", (username,))
            if u["frozen"]:
                audit(username, ip, "Login eines gesperrten Accounts", "", "abgelehnt", con=con)
                return self.err(403, "FROZEN", "Dein Panel-Zugang ist gesperrt. Wende dich an den Owner.")
        try:
            rank, perms = resolve_rank(u)
        except Exception as e:
            return self.err(500, "LUCKPERMS", "Rang konnte nicht gelesen werden: " + str(e)[:200])
        if not rank:
            audit(username, ip, "Login ohne Team-Rang", u["mc_name"] or u["role"], "abgelehnt")
            return self.err(403, "NO_RANK", "Dein Rang hat keinen Zugang zum Panel.")
        with db() as con:
            ips = json.loads(u["ips"] or "[]")
            if not ips:
                ips = [ip]
                con.execute("update users set ips=? where username=?", (json.dumps(ips), username))
                audit(username, ip, "Erster Login – IP gebunden", ip, con=con)
            elif ip not in ips:
                pend = con.execute("select id from ip_requests where username=? and ip=? and status='pending'", (username, ip)).fetchone()
                if not pend:
                    con.execute("insert into ip_requests (username, ip, time) values (?,?,?)", (username, ip, now))
                audit(username, ip, "Login von nicht freigegebener IP", "erlaubt: " + ", ".join(ips), "abgelehnt", con=con)
                return self.err(403, "IP_NOT_ALLOWED",
                                "Diese IP-Adresse (%s) ist für deinen Account nicht freigegeben. "
                                "Der Owner hat eine Freigabe-Anfrage erhalten." % ip)
            con.execute("update users set last_login=?, last_ip=?, rank=? where username=?", (now, ip, rank, username))
            # one active session per account
            con.execute("delete from sessions where username=?", (username,))
            token = secrets.token_urlsafe(32)
            exp = now + 3600 * float(conf().get("session_hours", 12))
            con.execute("insert into sessions (token_hash, username, rank, ip, perms, created, expires, last_seen) values (?,?,?,?,?,?,?,?)",
                        (thash(token), username, rank, ip, json.dumps(perms), now, exp, now))
        audit(username, ip, "Login", "Rang " + rank)
        self.send_json(200, {"status": "ok", "data": {"token": token, "username": username, "uuid": username, "rank": rank,
                                                      "perms": perms, "all_perms": PERMS, "expires": exp,
                                                      "must_change": bool(u["must_change"])}})

    def change_password(self, s):
        if self.rate_limited("password"):
            return self.err(429, "RATE_LIMIT", "Zu viele Versuche – bitte 5 Minuten warten.")
        try:
            data = json.loads(self.body(65536) or b"{}")
        except ValueError:
            return self.err(400, "BAD_REQUEST", "Ungültige Anfrage.")
        old, new = str(data.get("old", "")), str(data.get("new", ""))
        with db() as con:
            u = con.execute("select pw from users where username=?", (s["username"],)).fetchone()
            if not u or not check_pw(old, u["pw"]):
                audit(s["username"], self.ip, "Passwortänderung", "altes Passwort falsch", "abgelehnt", con=con)
                return self.err(403, "WRONG_PASSWORD", "Das bisherige Passwort ist falsch.")
            prob = pw_problem(new)
            if prob:
                return self.err(400, "WEAK_PASSWORD", prob)
            if new == old:
                return self.err(400, "WEAK_PASSWORD", "Das neue Passwort muss sich vom alten unterscheiden.")
            con.execute("update users set pw=?, must_change=0 where username=?", (hash_pw(new), s["username"]))
            # other sessions of this account end; the current one stays
            con.execute("delete from sessions where username=? and token_hash<>?", (s["username"], s["token_hash"]))
        audit(s["username"], self.ip, "Passwort geändert")
        return self.send_json(200, {"status": "ok"})

    def eco(self, s, perms):
        """Economy tab: runs eco.py (installed by the owner's panel) with the caller's rights."""
        try:
            req = json.loads(self.body(1024 * 1024).decode() or "{}")
        except ValueError:
            return self.err(400, "BAD_REQUEST", "Ungültige Anfrage")
        cmd = str(req.get("cmd", ""))
        need = ECO_CMDS.get(cmd)
        if need is not None and need not in perms:
            # stored rights may be stale (rank changed in LuckPerms) – check once more before refusing
            perms = refresh_session(s["token_hash"], s["username"], s["rank"], perms)[1]
        if need is None or need not in perms:
            audit(s["username"], self.ip, "Verweigert", "Wirtschaft: " + cmd[:40], "keine Berechtigung")
            return self.err(403, "NO_PERMISSION", "Keine Berechtigung" + (" (" + need + ")" if need else "") + ".")
        if not os.path.exists(ECO_PY):
            return self.err(503, "NOT_INSTALLED", "Der Wirtschafts-Reiter ist noch nicht eingerichtet – der Owner muss ihn einmal im Panel öffnen.")
        args = req.get("args") if isinstance(req.get("args"), dict) else {}
        args["actor"] = s["username"] + " (Team)"
        arg = base64.b64encode(json.dumps(args).encode()).decode()
        try:
            r = subprocess.run(["python3", ECO_PY, cmd, arg], capture_output=True, timeout=120)
            res = json.loads(r.stdout.decode() or "{}")
        except (subprocess.TimeoutExpired, ValueError) as e:
            return self.err(500, "ECO_ERROR", "Auswertung fehlgeschlagen: " + str(e)[:200])
        if need == "economy.edit":
            audit(s["username"], self.ip, "Wirtschaft: " + cmd, json.dumps({k: v for k, v in args.items() if k != "actor"}, ensure_ascii=False)[:300],
                  "ok" if res.get("status") == "ok" else "Fehler")
        if res.get("status") != "ok":
            return self.err(400, "ECO_ERROR", res.get("error") or "Fehler")
        return self.send_json(200, res)

    def mod(self, s, perms):
        """Moderation tab: runs mod.py with the caller's rights; the team member is recorded as the actor."""
        try:
            req = json.loads(self.body(256 * 1024).decode() or "{}")
        except ValueError:
            return self.err(400, "BAD_REQUEST", "Ungültige Anfrage")
        cmd = str(req.get("cmd", ""))
        args = req.get("args") if isinstance(req.get("args"), dict) else {}
        if cmd in MOD_VIEW:
            need = "moderation.view"
        elif cmd == "report_set":
            need = "moderation.reports"
        elif cmd == "act":
            need = MOD_ACT.get(str(args.get("action", "")))
        else:
            need = None
        if need is not None and need not in perms:
            perms = refresh_session(s["token_hash"], s["username"], s["rank"], perms)[1]
        if need is None or need not in perms:
            audit(s["username"], self.ip, "Verweigert", "Moderation: " + cmd[:20] + " " + str(args.get("action", ""))[:12], "keine Berechtigung")
            return self.err(403, "NO_PERMISSION", "Keine Berechtigung" + (" (" + need + ")" if need else "") + ".")
        if not os.path.exists(MOD_PY):
            return self.err(503, "NOT_INSTALLED", "Die Moderation ist noch nicht eingerichtet – der Owner muss das Panel einmal starten.")
        args["actor"] = s["username"] + " (Panel)"
        try:
            r = subprocess.run(["python3", MOD_PY, cmd, base64.b64encode(json.dumps(args).encode()).decode()], capture_output=True, timeout=60)
            res = json.loads(r.stdout.decode() or "{}")
        except (subprocess.TimeoutExpired, ValueError) as e:
            return self.err(500, "MOD_ERROR", "Moderation fehlgeschlagen: " + str(e)[:200])
        if cmd in ("act", "report_set"):
            detail = (str(args.get("action", "")) + " " + str(args.get("player", "")) + " " + str(args.get("duration", "")) + " " + str(args.get("reason", ""))) \
                if cmd == "act" else "Report #%s → %s" % (args.get("id"), args.get("status"))
            audit(s["username"], self.ip, "Moderation", detail.strip()[:300], "ok" if res.get("status") == "ok" else "Fehler")
        if res.get("status") != "ok":
            return self.err(400, "MOD_ERROR", res.get("error") or "Fehler")
        return self.send_json(200, res)

    def proxy(self, method, s, perms):
        need = required_perm(method, self.path)
        if need is None or need not in perms:
            audit(s["username"], self.ip, "Verweigert", method + " " + self.path.split("?")[0], "keine Berechtigung")
            return self.err(403, "NO_PERMISSION", "Keine Berechtigung" + (" (" + need + ")" if need else "") + ".")
        body = self.body()
        if need == "console.command":
            cmd = body.decode("utf-8", "replace")
            if not command_allowed(cmd, perms):
                audit(s["username"], self.ip, "Befehl blockiert", cmd[:200], "gesperrt")
                return self.err(403, "NO_PERMISSION", "Dieser Befehl ist für deinen Rang gesperrt.")
        c = conf()
        req = urllib.request.Request(c["crafty_url"].rstrip("/") + self.path, data=body if method != "GET" else None, method=method)
        for k, v in self.headers.items():
            if k.lower() not in ("host", "authorization", "content-length", "connection", "accept-encoding"):
                req.add_header(k, v)
        req.add_header("Authorization", "Bearer " + c.get("crafty_token", ""))
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # Crafty on localhost uses a self-signed certificate
        try:
            with urllib.request.urlopen(req, timeout=600, context=ctx) as r:
                code, data, ctype = r.status, r.read(), r.headers.get("Content-Type", "application/json")
        except urllib.error.HTTPError as e:
            code, data, ctype = e.code, e.read(), e.headers.get("Content-Type", "application/json")
        if method != "GET":
            detail = method + " " + self.path.split("?")[0]
            if need == "console.command":
                detail = body.decode("utf-8", "replace")[:300]
            audit(s["username"], self.ip, need, detail, "ok" if code < 400 else "Fehler %d" % code)
        if code in (401, 403):
            code = 502  # Crafty rejected the gateway token – do not log the panel user out
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    address_family = __import__("socket").AF_INET6

    def server_bind(self):
        import socket
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        super().server_bind()

    def finish_request(self, request, client_address):
        request.settimeout(60)
        try:
            tls = self.ctx.wrap_socket(request, server_side=True)
        except Exception:
            try:
                request.close()
            except Exception:
                pass
            return
        self.RequestHandlerClass(tls, client_address, self)


# ───────────────────────────── owner CLI (via SSH) ─────────────────────────────
def link_player(data):
    """Validates mc_name / role of a create/update request -> (mc_name, mc_uuid, role) or raises ValueError."""
    mc = str(data.get("mc_name", "") or "").strip()
    role = str(data.get("role", "") or "").strip().lower()
    mc_uuid = ""
    if mc:
        if not MC_RX.match(mc):
            raise ValueError("Ungültiger Minecraft-Name.")
        found = lp_player(mc)
        if not found:
            raise ValueError("Spieler „%s“ ist LuckPerms unbekannt – er muss einmal auf dem Server gewesen sein." % mc)
        mc_uuid, mc = found
        role = ""
    elif role:
        if not ROLE_RX.match(role):
            raise ValueError("Ungültiger Rang.")
    else:
        raise ValueError("Entweder einen Minecraft-Spieler verknüpfen oder einen festen Rang wählen.")
    return mc, mc_uuid, role


def admin(cmd, arg):
    data = json.loads(base64.b64decode(arg).decode()) if arg else {}
    c = conf()
    out = {"ok": True}
    with db() as con:
        if cmd == "state":
            try:
                groups = lp_groups()
                gerr = ""
            except Exception as e:
                groups, gerr = {}, str(e)
            now = time.time()
            out = {
                "ok": True, "version": VERSION, "perms": PERMS, "roles": c.get("roles", {}), "groups": groups, "groups_error": gerr,
                "blocked_commands": c.get("blocked_commands", []), "port": c.get("port"),
                "users": [{k: r[k] for k in r.keys() if k != "pw"} for r in con.execute("select * from users order by username")],
                "requests": [dict(r) for r in con.execute("select * from ip_requests where status='pending' order by time desc")],
                "sessions": [dict(r) | {"token_hash": r["token_hash"][:12]} for r in
                             con.execute("select * from sessions where expires > ? order by last_seen desc", (now,))],
                "audit": [dict(r) for r in con.execute("select * from audit order by id desc limit 400")],
            }
        elif cmd == "roles":
            roles = {g: [p for p in ps if p in PERM_KEYS] for g, ps in data.items() if ROLE_RX.match(g)}
            c["roles"] = {g: ps for g, ps in roles.items() if ps}
            save_conf(c)
            audit("Owner", "ssh", "Rechte geändert", json.dumps(c["roles"])[:500], con=con)
            # Running sessions get the new rights immediately. The rank is resolved again: a newly enabled LuckPerms group
            # can now be a member's highest panel rank (before, only the stored rank's rights were copied).
            for s in con.execute("select s.token_hash as th, s.rank as srank, u.mc_uuid as mc_uuid, u.role as role "
                                 "from sessions s join users u on u.username = s.username").fetchall():
                try:
                    rank, ps = resolve_rank(s)
                    rank = rank or ""
                except Exception:
                    rank, ps = s["srank"], [p for p in c["roles"].get(s["srank"], []) if p in PERM_KEYS]
                con.execute("update sessions set rank=?, perms=? where token_hash=?", (rank, json.dumps(ps), s["th"]))
        elif cmd == "user":
            name, action = str(data.get("username", "")).strip().lower(), data.get("action")
            if not USER_RX.match(name):
                return {"ok": False, "error": "Benutzername: 3–32 Zeichen, nur a–z, 0–9, _ . -"}
            u = con.execute("select * from users where username=?", (name,)).fetchone()
            if action != "create" and not u:
                return {"ok": False, "error": "Account unbekannt"}
            if action == "create":
                if u:
                    return {"ok": False, "error": "Diesen Benutzernamen gibt es schon."}
                prob = pw_problem(str(data.get("password", "")))
                if prob:
                    return {"ok": False, "error": prob}
                try:
                    mc, mc_uuid, role = link_player(data)
                except ValueError as e:
                    return {"ok": False, "error": str(e)}
                con.execute("insert into users (username, pw, mc_name, mc_uuid, role, created, must_change, note) values (?,?,?,?,?,?,?,?)",
                            (name, hash_pw(data["password"]), mc, mc_uuid, role, time.time(), 1 if data.get("must_change", True) else 0,
                             str(data.get("note", ""))[:200]))
                audit("Owner", "ssh", "Account angelegt", name + (" → " + mc if mc else " · Rang " + role), con=con)
            elif action == "update":
                try:
                    mc, mc_uuid, role = link_player(data)
                except ValueError as e:
                    return {"ok": False, "error": str(e)}
                con.execute("update users set mc_name=?, mc_uuid=?, role=?, note=? where username=?",
                            (mc, mc_uuid, role, str(data.get("note", u["note"] or ""))[:200], name))
                con.execute("delete from sessions where username=?", (name,))
                audit("Owner", "ssh", "Account geändert", name + (" → " + mc if mc else " · Rang " + role), con=con)
            elif action == "password":
                prob = pw_problem(str(data.get("password", "")))
                if prob:
                    return {"ok": False, "error": prob}
                con.execute("update users set pw=?, must_change=1, fails=0, locked_until=0 where username=?", (hash_pw(data["password"]), name))
                con.execute("delete from sessions where username=?", (name,))
                audit("Owner", "ssh", "Passwort zurückgesetzt", name, con=con)
            elif action == "freeze":
                con.execute("update users set frozen=1 where username=?", (name,))
                con.execute("delete from sessions where username=?", (name,))
                audit("Owner", "ssh", "Account gesperrt", name, con=con)
            elif action == "unfreeze":
                con.execute("update users set frozen=0, fails=0, locked_until=0 where username=?", (name,))
                audit("Owner", "ssh", "Account entsperrt", name, con=con)
            elif action == "kick":
                con.execute("delete from sessions where username=?", (name,))
                audit("Owner", "ssh", "Abmeldung erzwungen", name, con=con)
            elif action == "delete":
                con.execute("delete from sessions where username=?", (name,))
                con.execute("delete from users where username=?", (name,))
                con.execute("delete from ip_requests where username=?", (name,))
                audit("Owner", "ssh", "Account gelöscht", name, con=con)
            else:
                return {"ok": False, "error": "unbekannte Aktion"}
        elif cmd == "ip":
            name, ip, action = str(data.get("username", "")).lower(), data.get("ip", ""), data.get("action")
            u = con.execute("select * from users where username=?", (name,)).fetchone()
            if not u:
                return {"ok": False, "error": "Account unbekannt"}
            ips = json.loads(u["ips"] or "[]")
            if action == "approve":
                if ip not in ips:
                    ips.append(ip)
                con.execute("update ip_requests set status='approved' where username=? and ip=?", (name, ip))
            elif action == "deny":
                con.execute("update ip_requests set status='denied' where username=? and ip=?", (name, ip))
            elif action == "remove":
                ips = [x for x in ips if x != ip]
                con.execute("delete from sessions where username=? and ip=?", (name, ip))
            elif action == "add" and re.match(r"^[0-9a-fA-F:.]{3,45}$", ip):
                if ip not in ips:
                    ips.append(ip)
            con.execute("update users set ips=? where username=?", (json.dumps(ips), name))
            audit(name, "ssh", "IP " + str(action), ip, con=con)
        elif cmd == "config":
            if "blocked_commands" in data:
                c["blocked_commands"] = [str(b).strip().lower() for b in data["blocked_commands"] if str(b).strip()][:300]
            save_conf(c)
            audit("Owner", "ssh", "Einstellungen geändert", con=con)
        else:
            out = {"ok": False, "error": "unbekannter Befehl"}
    return out


def init_from_env():
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    c = dict(conf())
    for env, key in (("RP_CRAFTY_TOKEN", "crafty_token"), ("RP_LP_CONFIG", "luckperms_config")):
        v = os.environ.get(env, "").strip()
        if v:
            c[key] = v
    if os.environ.get("RP_PORT", "").isdigit():
        c["port"] = int(os.environ["RP_PORT"])
    c.setdefault("cert", "/etc/ryvenpanel/gw.crt")
    c.setdefault("key", "/etc/ryvenpanel/gw.key")
    save_conf(c)
    init_db()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--init":
        init_from_env()
        print("ok")
        return
    if len(sys.argv) > 2 and sys.argv[1] == "admin":
        init_db()
        arg = sys.argv[3] if len(sys.argv) > 3 else ""
        if arg == "-":  # payload via stdin – keeps passwords out of the process list
            arg = sys.stdin.read().strip()
        print(json.dumps(admin(sys.argv[2], arg), ensure_ascii=False, default=str))
        return
    init_db()
    c = conf()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(c["cert"], c["key"])
    srv = Server(("::", int(c["port"])), Handler)
    srv.ctx = ctx
    print("RyvenPanel Gateway %s auf Port %s" % (VERSION, c["port"]), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
