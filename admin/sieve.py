"""Erzeugt aus den Einstellungen der Weboberfläche ein Sieve-Skript und
legt es im Postfach ab, wo Dovecot (Pigeonhole) es bei der Zustellung ausführt.

Ablage:  /var/mail/<domain>/<local>/sieve/webui.sieve
Aktiv:   /var/mail/<domain>/<local>/.dovecot.sieve  -> sieve/webui.sieve
"""
import os

MAIL_ROOT = "/var/mail"
VMAIL_UID = VMAIL_GID = 5000
SCRIPT_NAME = "webui"

FIELDS = {
    "from": ("Absender", '"from"'),
    "to": ("Empfänger (An/Cc)", '["to", "cc"]'),
    "subject": ("Betreff", '"subject"'),
}
OPS = {
    "contains": ("enthält", ":contains"),
    "is": ("ist genau", ":is"),
}
ACTIONS = {
    "move": "in Ordner verschieben",
    "forward": "weiterleiten (ohne Kopie)",
    "forward_copy": "weiterleiten und behalten",
    "markread": "als gelesen markieren",
    "delete": "löschen",
}

DEFAULT_SETTINGS = {
    "vacation": {"enabled": False, "start": "", "end": "", "subject": "", "body": "", "days": 1},
    "rules": [],
}


def q(text):
    """Sieve-String in Anführungszeichen, sauber escaped."""
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def multiline(text):
    """Mehrzeiliger Sieve-Text (text: ... .), Zeilen mit '.' am Anfang gedoppelt."""
    lines = str(text).replace("\r\n", "\n").split("\n")
    lines = ["." + line if line.startswith(".") else line for line in lines]
    return "text:\n" + "\n".join(lines) + "\n.\n"


def build_script(settings, addresses):
    vac = settings.get("vacation", {})
    rules = settings.get("rules", [])
    out = [
        'require ["fileinto", "mailbox", "copy", "imap4flags", "vacation", "date", "relational"];',
        "# Automatisch erzeugt von der Mail-Weboberfläche – manuelle Änderungen werden überschrieben.",
        "",
    ]

    if vac.get("enabled") and vac.get("body", "").strip():
        conds = []
        if vac.get("start"):
            conds.append(f'currentdate :value "ge" "date" {q(vac["start"])}')
        if vac.get("end"):
            conds.append(f'currentdate :value "le" "date" {q(vac["end"])}')
        args = f":days {int(vac.get('days') or 1)}"
        if vac.get("subject", "").strip():
            args += f" :subject {q(vac['subject'].strip())}"
        if addresses:
            args += " :addresses [" + ", ".join(q(a) for a in addresses) + "]"
        action = f"vacation {args} {multiline(vac['body'].strip())};"
        out.append("# ---- Abwesenheit")
        if conds:
            cond = conds[0] if len(conds) == 1 else "allof(" + ", ".join(conds) + ")"
            # Nicht einrücken: im text:-Block zählt jedes Zeichen, und die
            # Endzeile muss exakt "." sein.
            out.append(f"if {cond} {{")
            out.append(action)
            out.append("}")
        else:
            out.append(action)
        out.append("")

    if rules:
        out.append("# ---- Filter")
    for r in rules:
        field = FIELDS.get(r.get("field"))
        op = OPS.get(r.get("op"))
        if not field or not op or not r.get("value"):
            continue
        # "ist genau" bei Adressen: nur die reine Adresse vergleichen (ohne Anzeigenamen)
        kind = "address" if r["op"] == "is" and r["field"] in ("from", "to") else "header"
        test = f"{kind} {op[1]} {field[1]} {q(r['value'])}"
        act = r.get("action")
        target = r.get("target", "")
        if act == "move":
            body = f"fileinto :create {q(target)}; stop;"
        elif act == "forward":
            body = f"redirect {q(target)}; stop;"
        elif act == "forward_copy":
            body = f"redirect :copy {q(target)};"
        elif act == "markread":
            body = 'addflag "\\\\Seen";'
        elif act == "delete":
            body = "discard; stop;"
        else:
            continue
        out.append(f"if {test} {{ {body} }}")
    out.append("")
    return "\n".join(out)


def mailbox_home(email):
    local, domain = email.lower().split("@", 1)
    return os.path.join(MAIL_ROOT, domain, local)


def _mkdir_vmail(path):
    if not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
    os.chown(path, VMAIL_UID, VMAIL_GID)


def write_script(email, script):
    """Skript atomar schreiben und aktivieren."""
    home = mailbox_home(email)
    _mkdir_vmail(os.path.join(MAIL_ROOT, email.split("@", 1)[1]))
    _mkdir_vmail(home)
    sieve_dir = os.path.join(home, "sieve")
    _mkdir_vmail(sieve_dir)

    path = os.path.join(sieve_dir, SCRIPT_NAME + ".sieve")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(script)
    os.chown(tmp, VMAIL_UID, VMAIL_GID)
    os.replace(tmp, path)

    link = os.path.join(home, ".dovecot.sieve")
    target = os.path.join("sieve", SCRIPT_NAME + ".sieve")
    if os.path.islink(link) and os.readlink(link) == target:
        return
    if os.path.lexists(link):
        os.remove(link)
    os.symlink(target, link)
    os.lchown(link, VMAIL_UID, VMAIL_GID)


def active_script(email):
    """Name des aktiven Skripts (None = keins), um fremde Skripte zu erkennen."""
    link = os.path.join(mailbox_home(email), ".dovecot.sieve")
    if not os.path.lexists(link):
        return None
    if os.path.islink(link):
        return os.path.basename(os.readlink(link)).removesuffix(".sieve")
    return "(Datei)"
