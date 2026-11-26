"""Kleine Verwaltungsoberfläche für den Mailserver.

Benutzer + Passwörter kommen aus LDAP (nur lesend).
Domains und Aliase liegen in einer SQLite-Datenbank, die Postfix direkt abfragt.
"""
import base64
import json
import os
import re
import urllib.request
import secrets
import sqlite3
import threading
import time
from functools import wraps

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   session, url_for)
from ldap3 import Connection, Server, SUBTREE
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

import sieve

DB_PATH = os.environ.get("DB_PATH", "/data/config/mail.db")
MAIL_HOSTNAME = os.environ["MAIL_HOSTNAME"]
LDAP_URI = os.environ["LDAP_URI"]
LDAP_BASE_DN = os.environ["LDAP_BASE_DN"]
LDAP_BIND_DN = os.environ["LDAP_BIND_DN"]
LDAP_BIND_PW = os.environ["LDAP_BIND_PW"]
LDAP_USER_FILTER = os.environ["LDAP_USER_FILTER"]
LDAP_MAIL_ATTR = os.environ.get("LDAP_MAIL_ATTR", "mail")
# Optionales Standard-LDAP-Attribut mit dem Speicherlimit, z. B. description: quota=10G
# (hat Vorrang vor der Weboberfläche)
LDAP_QUOTA_ATTR = os.environ.get("LDAP_QUOTA_ATTR", "").strip()
LDAP_QUOTA_SYNC_SECONDS = int(os.environ.get("LDAP_QUOTA_SYNC_SECONDS", "300"))
QUOTA_DEFAULT_MB = int(os.environ.get("QUOTA_DEFAULT_MB", "5120"))
DOVEADM_URL = os.environ.get("DOVEADM_URL", "http://mail:8080/doveadm/v1")
DOVEADM_API_KEY = os.environ.get("DOVEADM_API_KEY", "")
ADMIN_USERS = {u.strip().lower() for u in os.environ.get("ADMIN_USERS", "").split(",") if u.strip()}

QUOTA_RE = re.compile(r"^\s*(?:quota\s*[=:]\s*)?(\d+(?:[.,]\d+)?)\s*([MGT]i?B?)?\s*$", re.I)
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
EMAIL_RE = re.compile(r"^[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,63}$")

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax")


# ------------------------------------------------------------------ Datenbank
def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS domains (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            active INTEGER NOT NULL DEFAULT 1,
            created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS aliases (
            id INTEGER PRIMARY KEY,
            source TEXT NOT NULL,
            destination TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (source, destination)
        );
        -- Zusätzliche Absenderadressen: 'login' darf als 'sender' senden.
        -- sender = 'x@domain.tld' (einzelne Adresse) oder '@domain.tld' (ganze Domain)
        CREATE TABLE IF NOT EXISTS senders (
            id INTEGER PRIMARY KEY,
            sender TEXT NOT NULL,
            login TEXT NOT NULL,
            created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (sender, login)
        );
        -- Abweichendes Speicherlimit pro Postfach (MB, 0 = unbegrenzt).
        -- Kein Eintrag = Standard aus QUOTA_DEFAULT_MB.
        CREATE TABLE IF NOT EXISTS quotas (
            email TEXT PRIMARY KEY,
            quota_mb INTEGER NOT NULL
        );
        -- Abwesenheit + Filterregeln je Postfach (JSON), daraus wird das Sieve-Skript erzeugt
        CREATE TABLE IF NOT EXISTS sieve_settings (
            email TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        -- Abgleich aus dem LDAP-Attribut LDAP_QUOTA_ATTR (wird komplett neu geschrieben)
        CREATE TABLE IF NOT EXISTS ldap_quotas (
            email TEXT PRIMARY KEY,
            quota_mb INTEGER NOT NULL
        );
    """)
    if con.execute("SELECT COUNT(*) FROM domains").fetchone()[0] == 0:
        for d in os.environ.get("INITIAL_DOMAINS", "").split(","):
            d = d.strip().lower()
            if DOMAIN_RE.match(d):
                con.execute("INSERT OR IGNORE INTO domains (name) VALUES (?)", (d,))
    con.commit()
    con.close()
    # Postfix (läuft im anderen Container als Benutzer 'postfix') muss lesen dürfen
    os.chmod(DB_PATH, 0o644)


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    con = g.pop("db", None)
    if con is not None:
        con.close()


def active_domains():
    return {r["name"] for r in db().execute("SELECT name FROM domains WHERE active = 1")}


# ------------------------------------------------------------------ LDAP
def ldap_reader():
    return Connection(Server(LDAP_URI, connect_timeout=5), LDAP_BIND_DN, LDAP_BIND_PW,
                      auto_bind=True, receive_timeout=10)


def ldap_filter(value):
    return LDAP_USER_FILTER.replace("%s", value)


def ldap_authenticate(email, password):
    """True, wenn E-Mail + Passwort gegen LDAP gültig sind."""
    if not password:
        return False
    try:
        conn = ldap_reader()
        conn.search(LDAP_BASE_DN, ldap_filter(escape_filter_chars(email)), SUBTREE,
                    attributes=[LDAP_MAIL_ATTR])
        if len(conn.entries) != 1:
            return False
        user_dn = conn.entries[0].entry_dn
        conn.unbind()
        user = Connection(Server(LDAP_URI, connect_timeout=5), user_dn, password)
        ok = user.bind()
        user.unbind()
        return ok
    except LDAPException:
        return False


def ldap_user_exists(email):
    conn = ldap_reader()
    conn.search(LDAP_BASE_DN, ldap_filter(escape_filter_chars(email)), SUBTREE,
                attributes=[LDAP_MAIL_ATTR])
    found = len(conn.entries) > 0
    conn.unbind()
    return found


def parse_quota(values):
    """Erster gültiger Quota-Wert aus einem (ggf. mehrwertigen) Attribut, in MB.

    Akzeptiert z. B. 'quota=10G', 'quota: 500M', '10GB', '2048' (= MB), 'quota=0' (unbegrenzt).
    Alles andere (normaler Beschreibungstext) wird ignoriert.
    """
    if isinstance(values, (str, int)):
        values = [values]
    factor = {"M": 1, "G": 1024, "T": 1024 * 1024}
    for v in values:
        m = QUOTA_RE.match(str(v))
        if m:
            number = float(m.group(1).replace(",", "."))
            unit = (m.group(2) or "M")[0].upper()
            return round(number * factor[unit])
    return None


def ldap_mailboxes():
    """Alle LDAP-Benutzer, deren Mailadresse zu einer aktiven Domain gehört."""
    conn = ldap_reader()
    conn.search(LDAP_BASE_DN, ldap_filter("*"), SUBTREE,
                attributes=[LDAP_MAIL_ATTR, "cn", "displayName"] + ([LDAP_QUOTA_ATTR] if LDAP_QUOTA_ATTR else []))
    domains = active_domains()
    result = []
    for e in conn.entries:
        attrs = e.entry_attributes_as_dict
        name = (attrs.get("displayName") or attrs.get("cn") or [""])[0]
        ldap_mb = parse_quota(attrs.get(LDAP_QUOTA_ATTR, [])) if LDAP_QUOTA_ATTR else None
        for addr in attrs.get(LDAP_MAIL_ATTR, []):
            addr = addr.lower()
            if "@" in addr and addr.split("@", 1)[1] in domains:
                result.append({"email": addr, "name": name, "ldap_mb": ldap_mb})
    conn.unbind()
    return sorted(result, key=lambda m: (m["email"].split("@")[1], m["email"]))


def dovecot_quota(emails):
    """Belegung + Limit je Postfach über die doveadm-HTTP-API (ein Request für alle).

    Rückgabe: {email: {"used": Bytes, "limit": Bytes oder None}}
    """
    if not emails:
        return {}
    cmds = [["quotaGet", {"user": e}, str(i)] for i, e in enumerate(emails)]
    req = urllib.request.Request(
        DOVEADM_URL, data=json.dumps(cmds).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "X-Dovecot-API " + base64.b64encode(DOVEADM_API_KEY.encode()).decode()})
    with urllib.request.urlopen(req, timeout=20) as resp:
        answer = json.load(resp)

    result = {}
    for kind, data, tag in answer:
        if kind != "doveadmResponse":
            continue
        for row in data:
            if row.get("type") == "STORAGE":
                limit = row.get("limit", "-")
                result[emails[int(tag)]] = {
                    "used": int(row.get("value") or 0) * 1024,          # KiB -> Bytes
                    "limit": int(limit) * 1024 if limit not in ("-", "", None) else None,
                }
    return result


def store_ldap_quotas(boxes):
    """Schreibt die LDAP-Limits nach SQLite, wo Dovecot sie liest."""
    con = sqlite3.connect(DB_PATH, timeout=10)
    with con:
        con.execute("DELETE FROM ldap_quotas")
        con.executemany("INSERT OR REPLACE INTO ldap_quotas (email, quota_mb) VALUES (?, ?)",
                        [(m["email"], m["ldap_mb"]) for m in boxes if m["ldap_mb"] is not None])
    con.close()


def ldap_quota_sync_loop():
    while True:
        try:
            store_ldap_quotas(ldap_mailboxes_standalone())
        except Exception as exc:  # LDAP kurz weg: alte Werte bleiben stehen
            app.logger.warning("LDAP-Quota-Abgleich fehlgeschlagen: %s", exc)
        time.sleep(LDAP_QUOTA_SYNC_SECONDS)


def ldap_mailboxes_standalone():
    """ldap_mailboxes() außerhalb eines Requests (eigene DB-Verbindung)."""
    with app.app_context():
        try:
            return ldap_mailboxes()
        finally:
            close_db(None)


@app.template_filter("filesize")
def filesize(n):
    if n is None:
        return "–"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


# ------------------------------------------------------------------ Login / CSRF
def login_required(view):
    """Jeder angemeldete Postfach-Benutzer."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapper


def admin_required(view):
    """Nur Benutzer aus ADMIN_USERS."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login", next=request.path))
        if not session.get("admin"):
            abort(403)
        return view(*args, **kwargs)
    return wrapper


@app.before_request
def csrf_protect():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    if request.method == "POST" and request.form.get("csrf") != session["csrf"]:
        abort(400, "Ungültiges Formular-Token")


@app.context_processor
def inject_globals():
    return {"csrf": session.get("csrf"), "user": session.get("user"),
            "is_admin": session.get("admin", False), "mail_hostname": MAIL_HOSTNAME}


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        is_admin = email in ADMIN_USERS
        domain = email.split("@", 1)[1] if "@" in email else ""
        # Admins immer, sonst jeder LDAP-Benutzer mit Postfach in einer aktiven Domain
        if (is_admin or domain in active_domains()) and ldap_authenticate(email, password):
            session.clear()
            session["user"] = email
            session["admin"] = is_admin
            session["csrf"] = secrets.token_urlsafe(32)
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("index"))
        flash("Anmeldung fehlgeschlagen.", "error")
    return render_template("login.html")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ------------------------------------------------------------------ Start
@app.get("/")
@login_required
def index():
    return redirect(url_for("domains") if session.get("admin") else url_for("vacation"))


# ------------------------------------------------------------------ Domains
@app.get("/domains")
@admin_required
def domains():
    rows = db().execute("""
        SELECT d.*, (SELECT COUNT(*) FROM aliases a
                     WHERE a.source LIKE '%@' || d.name) AS alias_count
        FROM domains d ORDER BY d.name""").fetchall()
    return render_template("domains.html", domains=rows)


@app.post("/domains/add")
@admin_required
def domain_add():
    name = request.form.get("name", "").strip().lower()
    if not DOMAIN_RE.match(name):
        flash(f"Ungültiger Domainname: {name}", "error")
    else:
        try:
            db().execute("INSERT INTO domains (name) VALUES (?)", (name,))
            db().commit()
            flash(f"Domain {name} angelegt.", "ok")
        except sqlite3.IntegrityError:
            flash(f"Domain {name} existiert bereits.", "error")
    return redirect(url_for("domains"))


@app.post("/domains/<int:did>/toggle")
@admin_required
def domain_toggle(did):
    db().execute("UPDATE domains SET active = 1 - active WHERE id = ?", (did,))
    db().commit()
    return redirect(url_for("domains"))


@app.post("/domains/<int:did>/delete")
@admin_required
def domain_delete(did):
    row = db().execute("SELECT name FROM domains WHERE id = ?", (did,)).fetchone()
    if row:
        db().execute("DELETE FROM aliases WHERE source LIKE ?", ("%@" + row["name"],))
        db().execute("DELETE FROM senders WHERE sender LIKE ?", ("%@" + row["name"],))
        db().execute("DELETE FROM domains WHERE id = ?", (did,))
        db().commit()
        flash(f"Domain {row['name']} und ihre Aliase gelöscht. "
              "Postfächer auf der Platte bleiben erhalten.", "ok")
    return redirect(url_for("domains"))


@app.get("/domains/<int:did>/dns")
@admin_required
def domain_dns(did):
    row = db().execute("SELECT * FROM domains WHERE id = ?", (did,)).fetchone()
    if not row:
        abort(404)
    return render_template("dns.html", domain=row)


# ------------------------------------------------------------------ Aliase
@app.get("/aliases")
@admin_required
def aliases():
    rows = db().execute("SELECT * FROM aliases ORDER BY "
                        "substr(source, instr(source, '@') + 1), source, destination").fetchall()
    doms = db().execute("SELECT name FROM domains ORDER BY name").fetchall()
    return render_template("aliases.html", aliases=rows, domains=doms)


@app.post("/aliases/add")
@admin_required
def alias_add():
    local = request.form.get("local", "").strip().lower()
    domain = request.form.get("domain", "").strip().lower()
    dests = [d.strip().lower() for d in re.split(r"[,\s]+", request.form.get("destination", "")) if d.strip()]
    source = f"{local}@{domain}" if local else f"@{domain}"   # leer = Catch-All

    if domain not in {r["name"] for r in db().execute("SELECT name FROM domains")}:
        flash("Unbekannte Domain.", "error")
    elif local and not EMAIL_RE.match(source):
        flash(f"Ungültige Adresse: {source}", "error")
    elif not dests or not all(EMAIL_RE.match(d) for d in dests):
        flash("Bitte gültige Zieladresse(n) angeben.", "error")
    else:
        for d in dests:
            db().execute("INSERT OR IGNORE INTO aliases (source, destination) VALUES (?, ?)", (source, d))
        db().commit()
        flash(f"Alias {source} → {', '.join(dests)} angelegt.", "ok")
    return redirect(url_for("aliases"))


@app.post("/aliases/<int:aid>/toggle")
@admin_required
def alias_toggle(aid):
    db().execute("UPDATE aliases SET active = 1 - active WHERE id = ?", (aid,))
    db().commit()
    return redirect(url_for("aliases"))


@app.post("/aliases/<int:aid>/delete")
@admin_required
def alias_delete(aid):
    db().execute("DELETE FROM aliases WHERE id = ?", (aid,))
    db().commit()
    return redirect(url_for("aliases"))


# ------------------------------------------------------------------ Absender
@app.get("/senders")
@admin_required
def senders():
    rows = db().execute("SELECT * FROM senders ORDER BY login, "
                        "substr(sender, instr(sender, '@') + 1), sender").fetchall()
    doms = db().execute("SELECT name FROM domains ORDER BY name").fetchall()
    return render_template("senders.html", senders=rows, domains=doms)


@app.post("/senders/add")
@admin_required
def sender_add():
    login = request.form.get("login", "").strip().lower()
    local = request.form.get("local", "").strip().lower()
    domain = request.form.get("domain", "").strip().lower()
    sender = f"{local}@{domain}" if local else f"@{domain}"   # leer = ganze Domain

    # Nur eigene Domains – fremde Adressen würden an SPF/DMARC scheitern
    if domain not in {r["name"] for r in db().execute("SELECT name FROM domains")}:
        flash("Unbekannte Domain.", "error")
    elif local and not EMAIL_RE.match(sender):
        flash(f"Ungültige Absenderadresse: {sender}", "error")
    elif not EMAIL_RE.match(login):
        flash("Bitte die Mailadresse (Login) des Benutzers angeben.", "error")
    else:
        try:
            exists = ldap_user_exists(login)
        except LDAPException:
            exists = True   # LDAP gerade nicht erreichbar – nicht blockieren
        if not exists:
            flash(f"Kein LDAP-Benutzer mit Login {login} gefunden.", "error")
        else:
            db().execute("INSERT OR IGNORE INTO senders (sender, login) VALUES (?, ?)",
                         (sender, login))
            db().commit()
            what = f"jeder Adresse @{domain}" if not local else sender
            flash(f"{login} darf jetzt mit {what} senden.", "ok")
    return redirect(url_for("senders"))


@app.post("/senders/<int:sid>/delete")
@admin_required
def sender_delete(sid):
    db().execute("DELETE FROM senders WHERE id = ?", (sid,))
    db().commit()
    return redirect(url_for("senders"))


# ------------------------------------------------------------------ Abwesenheit & Filter
def target_user():
    """Eigenes Postfach – Admins dürfen per ?user= auch fremde bearbeiten."""
    other = request.values.get("user", "").strip().lower()
    if other and other != session["user"]:
        if not session.get("admin") or not EMAIL_RE.match(other):
            abort(403)
        return other
    return session["user"]


def load_settings(email):
    row = db().execute("SELECT data FROM sieve_settings WHERE email = ?", (email,)).fetchone()
    settings = json.loads(json.dumps(sieve.DEFAULT_SETTINGS))
    if row:
        settings.update(json.loads(row["data"]))
    return settings


def own_addresses(email):
    """Adressen, auf die die Abwesenheitsnotiz antworten soll."""
    addrs = {email}
    addrs |= {r["source"] for r in db().execute(
        "SELECT source FROM aliases WHERE destination = ? AND active = 1 AND source NOT LIKE '@%'", (email,))}
    addrs |= {r["sender"] for r in db().execute(
        "SELECT sender FROM senders WHERE login = ? AND sender NOT LIKE '@%'", (email,))}
    return sorted(addrs)


def save_settings(email, settings):
    db().execute("INSERT INTO sieve_settings (email, data) VALUES (?, ?) "
                 "ON CONFLICT(email) DO UPDATE SET data = excluded.data, updated = CURRENT_TIMESTAMP",
                 (email, json.dumps(settings)))
    db().commit()
    sieve.write_script(email, sieve.build_script(settings, own_addresses(email)))


def back(endpoint, email):
    return redirect(url_for(endpoint, user=email) if email != session["user"] else url_for(endpoint))


@app.route("/me/vacation", methods=["GET", "POST"])
@login_required
def vacation():
    email = target_user()
    settings = load_settings(email)
    if request.method == "POST":
        v = {
            "enabled": request.form.get("enabled") == "1",
            "start": request.form.get("start", "").strip(),
            "end": request.form.get("end", "").strip(),
            "subject": request.form.get("subject", "").strip()[:200],
            "body": request.form.get("body", "").strip()[:5000],
            "days": max(1, min(30, int(request.form.get("days") or 1) if (request.form.get("days") or "1").isdigit() else 1)),
        }
        date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
        if any(d and not date_re.match(d) for d in (v["start"], v["end"])):
            flash("Ungültiges Datum.", "error")
        elif v["start"] and v["end"] and v["end"] < v["start"]:
            flash("Das Ende liegt vor dem Beginn.", "error")
        elif v["enabled"] and not v["body"]:
            flash("Bitte einen Text für die Abwesenheitsnotiz eingeben.", "error")
        else:
            settings["vacation"] = v
            save_settings(email, settings)
            flash("Abwesenheitsnotiz " + ("aktiviert." if v["enabled"] else "gespeichert (aus)."), "ok")
            return back("vacation", email)
    return render_template("vacation.html", email=email, v=settings["vacation"],
                           addresses=own_addresses(email), active=sieve.active_script(email))


@app.route("/me/filters", methods=["GET", "POST"])
@login_required
def filters():
    email = target_user()
    settings = load_settings(email)
    if request.method == "POST":
        r = {k: request.form.get(k, "").strip() for k in ("field", "op", "value", "action", "target")}
        if r["field"] not in sieve.FIELDS or r["op"] not in sieve.OPS or r["action"] not in sieve.ACTIONS:
            flash("Ungültige Regel.", "error")
        elif not r["value"]:
            flash("Bitte einen Suchbegriff angeben.", "error")
        elif r["action"] == "move" and not r["target"]:
            flash("Bitte einen Zielordner angeben.", "error")
        elif r["action"] in ("forward", "forward_copy") and not EMAIL_RE.match(r["target"].lower()):
            flash("Bitte eine gültige Weiterleitungsadresse angeben.", "error")
        else:
            if r["action"] in ("forward", "forward_copy"):
                r["target"] = r["target"].lower()
            elif r["action"] != "move":
                r["target"] = ""
            settings["rules"].append(r)
            save_settings(email, settings)
            flash("Regel hinzugefügt.", "ok")
            return back("filters", email)
    return render_template("filters.html", email=email, rules=settings["rules"],
                           fields=sieve.FIELDS, ops=sieve.OPS, actions=sieve.ACTIONS,
                           active=sieve.active_script(email))


@app.post("/me/filters/<int:idx>/<op>")
@login_required
def filter_change(idx, op):
    email = target_user()
    settings = load_settings(email)
    rules = settings["rules"]
    if 0 <= idx < len(rules):
        if op == "delete":
            rules.pop(idx)
        elif op == "up" and idx > 0:
            rules[idx - 1], rules[idx] = rules[idx], rules[idx - 1]
        elif op == "down" and idx < len(rules) - 1:
            rules[idx + 1], rules[idx] = rules[idx], rules[idx + 1]
        save_settings(email, settings)
    return back("filters", email)


# ------------------------------------------------------------------ Postfächer
@app.get("/mailboxes")
@admin_required
def mailboxes():
    boxes, error, quota_error = [], None, None
    try:
        boxes = ldap_mailboxes()
        if LDAP_QUOTA_ATTR:
            store_ldap_quotas(boxes)   # Änderungen in LDAP sofort übernehmen
    except LDAPException as exc:
        error = str(exc)

    custom = {r["email"]: r["quota_mb"] for r in db().execute("SELECT * FROM quotas")}
    try:
        usage = dovecot_quota([m["email"] for m in boxes])
    except Exception as exc:  # Mail-Container nicht erreichbar o. Ä.
        usage, quota_error = {}, str(exc)

    for m in boxes:
        u = usage.get(m["email"], {})
        m["used"] = u.get("used")
        m["limit"] = u.get("limit")
        m["percent"] = (min(100, round(100 * m["used"] / m["limit"]))
                        if m["used"] is not None and m["limit"] else None)
        mb = custom.get(m["email"])
        m["custom_gb"] = "" if mb is None else f"{round(mb / 1024, 2):g}"
        if m["ldap_mb"] is not None:
            m["source"] = "LDAP"
            m["ldap_gb"] = "unbegrenzt" if m["ldap_mb"] == 0 else f"{round(m['ldap_mb'] / 1024, 2):g} GB"
        else:
            m["source"] = "Weboberfläche" if mb is not None else "Standard"
    total = sum(m["used"] or 0 for m in boxes)
    return render_template("mailboxes.html", mailboxes=boxes, error=error,
                           quota_error=quota_error, total=total,
                           default_mb=QUOTA_DEFAULT_MB, ldap_quota_attr=LDAP_QUOTA_ATTR)


@app.post("/mailboxes/quota")
@admin_required
def mailbox_quota():
    email = request.form.get("email", "").strip().lower()
    value = request.form.get("quota_gb", "").strip().replace(",", ".")
    if not EMAIL_RE.match(email):
        abort(400)
    if value == "":
        db().execute("DELETE FROM quotas WHERE email = ?", (email,))
        flash(f"{email}: Standardlimit gesetzt.", "ok")
    else:
        try:
            mb = round(float(value) * 1024)
            if mb < 0:
                raise ValueError
        except ValueError:
            flash("Bitte eine Zahl in GB angeben (0 = unbegrenzt).", "error")
            return redirect(url_for("mailboxes"))
        db().execute("INSERT INTO quotas (email, quota_mb) VALUES (?, ?) "
                     "ON CONFLICT(email) DO UPDATE SET quota_mb = excluded.quota_mb", (email, mb))
        flash(f"{email}: Limit {'unbegrenzt' if mb == 0 else value + ' GB'}.", "ok")
    db().commit()
    return redirect(url_for("mailboxes"))


init_db()

if LDAP_QUOTA_ATTR:
    threading.Thread(target=ldap_quota_sync_loop, daemon=True).start()
