#!/usr/bin/env python3
"""
RyvenPanel – Moderation (mod.py)

Reports, Strafen (RyvenBans) und Discord-Verknüpfungen (Verify) aus der RyvenLink-Datenbank lesen und Strafen über
RyvenBans auf dem Proxy ausführen. Aufruf durch das Panel (Owner, per SSH) oder das Team-Gateway (Team-Logins mit den
Rechten moderation.*):

    python3 mod.py <befehl> [base64-json]

Befehle: overview                                  – Zähler für die Übersicht
         reports     {status, q, limit}            – Reports (open / claimed / done / rejected / all)
         report      {id}                          – ein Report inkl. Chatverlauf
         report_set  {id, status, actor}           – Report übernehmen / erledigen / ablehnen (Melder bekommt Nachricht)
         punishments {type, active, q, limit}      – Strafen (ban / mute / kick / warn / all)
         links       {q, limit}                    – Discord-Verknüpfungen
         player      {name}                        – Spieler: Daten, Verknüpfung, Strafen, Reports
         templates                                 – Strafvorlagen aus der RyvenLink-Config
         act         {action, player, duration, reason, silent, actor}
                     action: warn / kick / mute / ban / ipban / unmute / unban
"""
import base64, glob, json, os, re, ssl, subprocess, sys, time, urllib.request

import yaml

VERSION = "1.0"
SERVERS = os.environ.get("RP_MOD_SERVERS", "/opt/crafty/docker/servers")
GATEWAY_CONF = "/etc/ryvenpanel/gateway.json"
NAME_RX = re.compile(r"^\.?[A-Za-z0-9_]{1,17}$")
DUR_RX = re.compile(r"^(\d{1,5})(s|m|min|h|d|w|mo|y)$|^perm$", re.I)
ACTIONS = ("warn", "kick", "mute", "ban", "ipban", "unmute", "unban")
STATUSES = ("open", "claimed", "done", "rejected")


class Fail(Exception):
    pass


def proxy():
    """(crafty id, dir) of the Velocity proxy."""
    for f in glob.glob(SERVERS + "/*/velocity.toml"):
        d = os.path.dirname(f)
        return os.path.basename(d), d
    raise Fail("Velocity-Proxy nicht gefunden.")


def link_config():
    _, d = proxy()
    for f in (d + "/plugins/ryvenlink/config.yml", d + "/plugins/RyvenLink/config.yml"):
        if os.path.isfile(f):
            return yaml.safe_load(open(f, encoding="utf-8")) or {}
    raise Fail("RyvenLink-Config auf dem Proxy nicht gefunden.")


def db_creds():
    d = link_config().get("database") or {}
    if not d.get("password"):
        raise Fail("Keine Datenbank-Zugangsdaten in der RyvenLink-Config.")
    return d


def q(v):
    """SQL string literal."""
    if v is None:
        return "NULL"
    return "'" + str(v).replace("\\", "\\\\").replace("'", "''").replace("\x00", "") + "'"


def rows(query):
    """Runs a query whose single column is JSON_OBJECT(...); returns a list of dicts."""
    d = db_creds()
    host = str(d.get("host", "mariadb")).split(":")[0]
    r = subprocess.run(["docker", "exec", "-i", "-e", "MYSQL_PWD=" + str(d.get("password", "")), host, "sh", "-c",
                        'exec $(command -v mariadb || command -v mysql) -u"$0" -D "$1" -N -B --raw', str(d.get("user") or d.get("username") or "ryvendiscord"),
                        str(d.get("name") or d.get("database") or "ryvendiscord")], input=query.encode(), capture_output=True, timeout=60)
    if r.returncode != 0:
        raise Fail("Datenbank: " + r.stderr.decode(errors="replace").strip()[:300])
    out = []
    for ln in r.stdout.decode(errors="replace").splitlines():
        ln = ln.strip()
        if ln.startswith("{"):
            out.append(json.loads(ln))
    return out


def execute(query):
    d = db_creds()
    host = str(d.get("host", "mariadb")).split(":")[0]
    r = subprocess.run(["docker", "exec", "-i", "-e", "MYSQL_PWD=" + str(d.get("password", "")), host, "sh", "-c",
                        'exec $(command -v mariadb || command -v mysql) -u"$0" -D "$1" -N -B', str(d.get("user") or d.get("username") or "ryvendiscord"),
                        str(d.get("name") or d.get("database") or "ryvendiscord")], input=query.encode(), capture_output=True, timeout=60)
    if r.returncode != 0:
        raise Fail("Datenbank: " + r.stderr.decode(errors="replace").strip()[:300])


def limit(a, default=300, top=2000):
    try:
        return max(1, min(top, int(a.get("limit") or default)))
    except (TypeError, ValueError):
        return default


def like(s):
    return q("%" + str(s).replace("%", "").replace("_", "\\_") + "%")


PUN_COLS = ("JSON_OBJECT('id', id, 'type', type, 'uuid', uuid, 'name', name, 'ip_ban', ip_ban, 'reason', reason, 'template', template, "
            "'actor', actor_name, 'server', server, 'silent', silent, 'created', created_at, 'expires', expires_at, 'active', active, "
            "'removed_by', removed_by, 'removed_at', removed_at, 'removed_reason', removed_reason)")
REP_COLS = ("JSON_OBJECT('id', id, 'reporter', reporter_name, 'reporter_uuid', reporter_uuid, 'target', target_name, 'reason', reason, "
            "'server', server, 'created', created_at, 'status', status, 'handler', handler, 'has_chatlog', chatlog IS NOT NULL AND chatlog <> '')")


# ───────────────────────────── commands ─────────────────────────────
def overview(a):
    r = rows("SELECT JSON_OBJECT("
             "'reports_open', (SELECT COUNT(*) FROM rl_reports WHERE status='open'), "
             "'reports_claimed', (SELECT COUNT(*) FROM rl_reports WHERE status='claimed'), "
             "'bans', (SELECT COUNT(*) FROM rl_punishments WHERE type='ban' AND active=1), "
             "'mutes', (SELECT COUNT(*) FROM rl_punishments WHERE type='mute' AND active=1), "
             "'today', (SELECT COUNT(*) FROM rl_punishments WHERE created_at > NOW() - INTERVAL 1 DAY), "
             "'links', (SELECT COUNT(*) FROM rl_links), "
             "'links_today', (SELECT COUNT(*) FROM rl_links WHERE linked_at > NOW() - INTERVAL 1 DAY))")
    return r[0] if r else {}


def reports(a):
    st = str(a.get("status") or "open")
    where = []
    if st == "active":
        where.append("status IN ('open','claimed')")
    elif st in STATUSES:
        where.append("status=" + q(st))
    if a.get("q"):
        s = like(a["q"])
        where.append("(target_name LIKE %s OR reporter_name LIKE %s OR reason LIKE %s)" % (s, s, s))
    return {"reports": rows("SELECT %s FROM rl_reports %s ORDER BY id DESC LIMIT %d" % (REP_COLS, ("WHERE " + " AND ".join(where)) if where else "", limit(a)))}


def report(a):
    rid = int(a.get("id") or 0)
    r = rows("SELECT JSON_MERGE_PATCH(%s, JSON_OBJECT('chatlog', chatlog)) FROM rl_reports WHERE id=%d" % (REP_COLS, rid))
    if not r:
        raise Fail("Report #%d nicht gefunden." % rid)
    rep = r[0]
    rep["target_punishments"] = rows("SELECT %s FROM rl_punishments WHERE name=%s ORDER BY id DESC LIMIT 20" % (PUN_COLS, q(rep["target"])))
    rep["target_reports"] = rows("SELECT JSON_OBJECT('n', COUNT(*)) FROM rl_reports WHERE target_name=%s" % q(rep["target"]))[0]["n"]
    return rep


def report_set(a):
    rid = int(a.get("id") or 0)
    st = str(a.get("status") or "")
    if st not in ("claimed", "done", "rejected", "open"):
        raise Fail("Ungültiger Status.")
    actor = str(a.get("actor") or "Panel")[:64]
    cur = rows("SELECT %s FROM rl_reports WHERE id=%d" % (REP_COLS, rid))
    if not cur:
        raise Fail("Report #%d nicht gefunden." % rid)
    execute("UPDATE rl_reports SET status=%s, handler=%s WHERE id=%d" % (q(st), q(actor), rid))
    r = cur[0]
    if st in ("done", "rejected") and r["status"] not in ("done", "rejected"):
        txt = "wurde vom Team bearbeitet. Danke für deine Meldung! 💜" if st == "done" else "wurde geprüft und abgelehnt."
        msg = "<gray>Dein Report <white>#%d</white> gegen <white>%s</white> %s</gray>" % (rid, r["target"], txt)
        execute("INSERT INTO rl_notify (uuid, message) VALUES (%s, %s)" % (q(r["reporter_uuid"]), q(msg)))
    return {"id": rid, "status": st}


def punishments(a):
    t = str(a.get("type") or "all")
    where = []
    if t in ("ban", "mute", "kick", "warn"):
        where.append("type=" + q(t))
    if a.get("active"):
        where.append("active=1 AND type IN ('ban','mute')")
    if a.get("q"):
        s = like(a["q"])
        where.append("(name LIKE %s OR reason LIKE %s OR actor_name LIKE %s OR uuid=%s)" % (s, s, s, q(a["q"])))
    return {"punishments": rows("SELECT %s FROM rl_punishments %s ORDER BY id DESC LIMIT %d" % (PUN_COLS, ("WHERE " + " AND ".join(where)) if where else "", limit(a)))}


def links(a):
    where = ""
    if a.get("q"):
        s = like(a["q"])
        where = "WHERE l.mc_name LIKE %s OR CAST(l.discord_id AS CHAR) LIKE %s OR l.uuid=%s" % (s, s, q(a["q"]))
    r = rows("SELECT JSON_OBJECT('uuid', l.uuid, 'name', l.mc_name, 'discord_id', CAST(l.discord_id AS CHAR), 'linked', l.linked_at, "
             "'groups', l.groups_csv, 'synced', l.synced_csv, 'last_seen', p.last_seen, 'online', p.online, 'playtime', p.playtime_sec) "
             "FROM rl_links l LEFT JOIN rl_players p ON p.uuid = l.uuid %s ORDER BY l.linked_at DESC LIMIT %d" % (where, limit(a)))
    pend = rows("SELECT JSON_OBJECT('n', COUNT(*)) FROM rl_codes WHERE expires_at > NOW()")
    return {"links": r, "pending_codes": pend[0]["n"] if pend else 0}


def player(a):
    name = str(a.get("name") or "")
    if not NAME_RX.match(name):
        raise Fail("Ungültiger Spielername.")
    p = rows("SELECT JSON_OBJECT('uuid', uuid, 'name', name, 'first_join', first_join, 'last_seen', last_seen, 'playtime', playtime_sec, "
             "'online', online, 'server', server) FROM rl_players WHERE name=%s ORDER BY last_seen DESC LIMIT 1" % q(name))
    info = p[0] if p else {"name": name}
    cond = "uuid=%s" % q(info["uuid"]) if info.get("uuid") else "name=%s" % q(name)
    info["punishments"] = rows("SELECT %s FROM rl_punishments WHERE %s ORDER BY id DESC LIMIT 100" % (PUN_COLS, cond))
    lk = rows("SELECT JSON_OBJECT('discord_id', CAST(discord_id AS CHAR), 'linked', linked_at, 'groups', groups_csv) FROM rl_links WHERE %s" % cond)
    info["link"] = lk[0] if lk else None
    info["reports_against"] = rows("SELECT %s FROM rl_reports WHERE target_name=%s ORDER BY id DESC LIMIT 50" % (REP_COLS, q(info.get("name", name))))
    info["reports_by"] = rows("SELECT JSON_OBJECT('n', COUNT(*)) FROM rl_reports WHERE reporter_name=%s" % q(info.get("name", name)))[0]["n"]
    return info


def default_templates():
    """Templates from the config.yml inside RyvenLink.jar (used when the live config has none)."""
    import zipfile
    _, d = proxy()
    for jar in glob.glob(d + "/plugins/*.jar"):
        try:
            z = zipfile.ZipFile(jar)
            if "config.yml" in z.namelist() and "velocity-plugin.json" in z.namelist() and b"punish-templates" in z.read("config.yml"):
                return (yaml.safe_load(z.read("config.yml").decode("utf-8")) or {}).get("punish-templates") or {}
        except (OSError, zipfile.BadZipFile, yaml.YAMLError):
            pass
    return {}


def templates(a):
    out = []
    for k, v in sorted((link_config().get("punish-templates") or default_templates()).items()):
        v = v or {}
        out.append({"key": str(k), "type": str(v.get("type", "")), "reason": str(v.get("reason", "")), "ladder": [str(x) for x in (v.get("ladder") or [])]})
    return {"templates": out}


def crafty_stdin(server_id, command):
    tok = json.load(open(GATEWAY_CONF, encoding="utf-8")).get("crafty_token")
    if not tok:
        raise Fail("Kein Crafty-Token gefunden.")
    req = urllib.request.Request("https://127.0.0.1:8443/api/v2/servers/%s/stdin" % server_id, data=command.encode(), method="POST",
                                 headers={"Authorization": "Bearer " + tok, "Content-Type": "text/plain"})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
        r.read()


def act(a):
    action = str(a.get("action") or "")
    name = str(a.get("player") or "").strip()
    if action not in ACTIONS:
        raise Fail("Unbekannte Aktion.")
    if not NAME_RX.match(name):
        raise Fail("Ungültiger Spielername.")
    dur = str(a.get("duration") or "").strip().lower()
    if dur and not DUR_RX.match(dur):
        raise Fail("Ungültige Dauer – z. B. 30m, 6h, 7d, 30d oder perm.")
    reason = re.sub(r"[\r\n\t]+", " ", str(a.get("reason") or "")).strip()[:200]
    if action in ("warn", "kick", "mute", "ban", "ipban") and not reason:
        raise Fail("Bitte einen Grund oder eine Vorlage angeben.")
    actor = re.sub(r"[\r\n\t]+", " ", str(a.get("actor") or "Panel"))[:60]
    payload = {"action": action, "player": name, "actor": actor, "reason": reason}
    if dur and dur != "perm" and action in ("mute", "ban", "ipban"):
        payload["duration"] = dur
    if a.get("silent"):
        payload["silent"] = True
    sid, _ = proxy()
    before = rows("SELECT JSON_OBJECT('m', COALESCE(MAX(id), 0)) FROM rl_punishments")[0]["m"]
    crafty_stdin(sid, "ryvenbans exec " + base64.b64encode(json.dumps(payload).encode()).decode())
    # wait for the proxy to write the result
    for _ in range(16):
        time.sleep(0.5)
        if action in ("unban", "unmute"):
            t = action[2:]
            left = rows("SELECT JSON_OBJECT('n', COUNT(*)) FROM rl_punishments WHERE name=%s AND type=%s AND active=1" % (q(name), q(t)))[0]["n"]
            if left == 0:
                return {"done": True, "message": "%s wurde %s." % (name, "entbannt" if t == "ban" else "entmutet")}
        else:
            new = rows("SELECT %s FROM rl_punishments WHERE id > %d AND name=%s ORDER BY id DESC LIMIT 1" % (PUN_COLS, int(before), q(name)))
            if new:
                return {"done": True, "punishment": new[0]}
    if action == "kick":
        return {"done": False, "message": "Kein Eintrag – ist %s gerade online?" % name}
    return {"done": False, "message": "Befehl gesendet, aber keine Bestätigung – Spieler unbekannt, geschützt oder nicht bestraft. Siehe Proxy-Konsole."}


CMDS = {"overview": overview, "reports": reports, "report": report, "report_set": report_set, "punishments": punishments,
        "links": links, "player": player, "templates": templates, "act": act}


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        print(json.dumps({"status": "error", "error": "Befehle: " + ", ".join(CMDS)}))
        return
    try:
        args = json.loads(base64.b64decode(sys.argv[2]).decode()) if len(sys.argv) > 2 and sys.argv[2] else {}
        print(json.dumps({"status": "ok", "data": CMDS[sys.argv[1]](args)}, ensure_ascii=False, default=str))
    except Fail as e:
        print(json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"status": "error", "error": "%s: %s" % (type(e).__name__, str(e)[:300])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
