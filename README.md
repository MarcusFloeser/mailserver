# Einfacher Mailserver (Docker Compose)

IMAP · SMTP · SSL/TLS · CalDAV/CardDAV · LDAP-Login · mehrere Domains · kleine Web-Verwaltung

## Aufbau

| Container  | Aufgabe                                                        | Ports            |
|------------|----------------------------------------------------------------|------------------|
| `caddy`    | HTTPS + Let's-Encrypt-Zertifikat (wird von Mail mitgenutzt)    | 80, 443          |
| `mail`     | Postfix (SMTP) + Dovecot (IMAP, LMTP, SASL, Sieve)             | 25, 465, 587, 993, 4190|
| `radicale` | CalDAV/CardDAV unter `https://MAIL_HOSTNAME/dav/`              | intern           |
| `admin`    | Weboberfläche unter `https://MAIL_HOSTNAME/` (Admins + Benutzer)| intern          |

**Wer ist wofür zuständig?**

- **LDAP** = Benutzer & Passwörter (nur lesend). Jeder LDAP-Benutzer, dessen `mail`-Attribut
  zu einer aktiven Domain gehört, hat automatisch ein Postfach. Login überall mit der vollen Mailadresse.
- **SQLite** (`/data/config/mail.db`) = Domains und Aliase, gepflegt über die Weboberfläche.
  Postfix fragt die DB direkt ab – Änderungen wirken sofort, kein Reload nötig.

Alle Domains laufen über einen gemeinsamen Hostnamen (`MAIL_HOSTNAME`), dadurch reicht ein Zertifikat.

## Voraussetzungen

1. **DNS:** A/AAAA-Record für `MAIL_HOSTNAME`, PTR (Reverse-DNS) beim Hoster auf denselben Namen.
   Pro Domain MX/SPF/DMARC – die Weboberfläche zeigt unter „DNS" die passenden Einträge.
2. **Ports 25, 80, 443, 465, 587, 993, 4190** frei. ⚠️ Läuft auf demselben Host noch mailcow,
   kollidieren die Ports – dann einen anderen Host nehmen oder mailcow vorher stoppen.
   Viele Heim-/Cloud-Anschlüsse sperren ausgehend Port 25.
3. **LDAP-Lesebenutzer:** Bei osixia/openldap `LDAP_READONLY_USER=true` setzen
   (ergibt `cn=readonly,dc=…`) oder einen eigenen Bind-User mit Leserechten anlegen.
4. **Docker-Netz des LDAP-Stacks** herausfinden: `docker network ls`
   und den Containernamen des OpenLDAP (`docker ps`).

## Installation

```bash
cp .env.example .env
nano .env                       # Hostname, Domains, LDAP-Daten, ADMIN_USERS, SECRET_KEY
openssl rand -hex 32            # -> SECRET_KEY
docker compose up -d --build
docker compose logs -f mail     # wartet auf das Zertifikat von Caddy, dann Start
```

Danach `https://MAIL_HOSTNAME/` öffnen und mit einem Konto aus `ADMIN_USERS` anmelden.

**Lokal testen ohne öffentliche Domain:** `TLS_MODE=selfsigned` und `CADDY_TLS=internal` setzen
(Weboberfläche bekommt ein Zertifikat der Caddy-eigenen CA, Mail ein selbstsigniertes).

## Client-Einstellungen

| Dienst        | Server          | Port | Verschlüsselung |
|---------------|-----------------|------|-----------------|
| IMAP          | MAIL_HOSTNAME   | 993  | SSL/TLS         |
| SMTP          | MAIL_HOSTNAME   | 465  | SSL/TLS         |
| SMTP (alt.)   | MAIL_HOSTNAME   | 587  | STARTTLS        |
| CalDAV/CardDAV| `https://MAIL_HOSTNAME/dav/` | 443 | HTTPS |
| ManageSieve   | MAIL_HOSTNAME   | 4190 | STARTTLS        |

Benutzername = vollständige Mailadresse, Passwort = LDAP-Passwort.

## Kurztests

```bash
# LDAP-Abfrage von Postfix (muss die Adresse zurückgeben)
docker compose exec mail postmap -q user@example.com ldap:/etc/postfix/maps/ldap-mailboxes.cf

# Domain aktiv?
docker compose exec mail postmap -q example.com sqlite:/etc/postfix/maps/sqlite-domains.cf

# Login gegen LDAP über Dovecot
docker compose exec mail doveadm auth test user@example.com

# TLS von außen
openssl s_client -connect MAIL_HOSTNAME:993 -quiet
```

## Funktionsumfang der Weboberfläche

- Domains anlegen, (de)aktivieren, löschen – inkl. DNS-Vorlagen je Domain
- Aliase/Weiterleitungen, auch mehrere Ziele und Catch-All (`@domain`)
- Zusätzliche Absenderadressen je Benutzer (einzeln oder ganze Domain)
- Postfachübersicht aus LDAP mit Belegung und Speicherlimit je Postfach
- Admin-Bereich nur für LDAP-Benutzer in `ADMIN_USERS`
- Selbstbedienung für alle Benutzer: Abwesenheitsnotiz und Filterregeln

Benutzer selbst legst du weiterhin in LDAP (phpLDAPadmin) an – einfach mit `mail`-Attribut
in einer der Domains.

## Absenderadressen

Beim Versand prüft Postfix, ob der angemeldete Benutzer die Absenderadresse verwenden darf
(`reject_sender_login_mismatch`). Erlaubt sind:

1. die eigene Adresse aus LDAP,
2. alle Aliase, bei denen der Benutzer als Ziel eingetragen ist,
3. zusätzliche Adressen aus **Weboberfläche → Absender**, entweder einzeln
   (`vertrieb@example.com`) oder für eine ganze Domain (`@example.com` = jede Adresse dort).

Freigegeben werden können nur Adressen aus eigenen Domains, weil fremde Absender an SPF/DMARC
scheitern würden. Die Änderungen wirken sofort.

Test, wer als eine Adresse senden darf:

```bash
docker compose exec mail postmap -q vertrieb@example.com sqlite:/etc/postfix/maps/sqlite-senders.cf
```

## Filter und Abwesenheit (Sieve)

Jeder Benutzer meldet sich mit Mailadresse und LDAP-Passwort an derselben Weboberfläche an
und sieht dort nur **Abwesenheit** und **Filter** für sein eigenes Postfach. Admins sehen
zusätzlich die Verwaltung und können über „Postfächer" die Einstellungen anderer Benutzer
bearbeiten, etwa die Abwesenheitsnotiz bei Krankheit.

**Abwesenheit:** an/aus, optional Zeitraum (Von/Bis, inklusive), Betreff, Text, Antwortabstand
pro Absender. Geantwortet wird auf die eigene Adresse, die Aliase des Benutzers und seine
zusätzlichen Absenderadressen, jeweils mit der angeschriebenen Adresse als Absender.
Mailinglisten und automatische Mails bekommen keine Antwort.

**Filter:** Regeln der Form „Wenn Absender/Empfänger/Betreff enthält/ist genau X, dann …"
- in Ordner verschieben (wird bei Bedarf angelegt, Unterordner mit `/`)
- weiterleiten (ohne Kopie) bzw. weiterleiten und behalten
- als gelesen markieren
- löschen

Die Reihenfolge ist mit ↑/↓ änderbar. Änderungen gelten sofort für neu eingehende Mails.

**Technik:** Die Weboberfläche speichert die Einstellungen in SQLite und erzeugt daraus
`~/sieve/webui.sieve` im Postfach (aktiv über `~/.dovecot.sieve`). Dovecot/Pigeonhole führt
das Skript bei der Zustellung aus.

**ManageSieve (Port 4190):** Wer lieber eigene Skripte schreibt (z. B. Thunderbird-Add-on
„Sieve"), kann das. Es ist aber immer nur **ein** Skript aktiv. Aktiviert jemand im Client ein
eigenes Skript, zeigt die Weboberfläche einen Hinweis, und Speichern in der Weboberfläche
aktiviert wieder deren Skript.

**Weiterleitungen** nach extern laufen ohne SRS. Damit SPF beim Ziel passt, wird die Adresse
des Empfängers als Envelope-Absender verwendet. Für Weiterleitungen an Gmail & Co. ist DKIM
(siehe unten) trotzdem empfehlenswert.

```bash
# Skript eines Benutzers prüfen / testweise kompilieren
docker compose exec mail sh -c 'cat /var/mail/example.com/anna/sieve/webui.sieve'
docker compose exec -u vmail mail sievec /var/mail/example.com/anna/sieve/webui.sieve
```

## Speicherlimits (Quota)

- Standardlimit pro Postfach: `QUOTA_DEFAULT_MB` in `.env` (0 = unbegrenzt).
- Abweichende Limits je Postfach: **Weboberfläche → Postfächer**, Feld „Limit (GB)".
  Leer = Standard, 0 = unbegrenzt. Wirkt sofort.
- Volle Postfächer lehnt Postfix schon beim Einliefern ab (`552 Mailbox full`), der Absender
  bekommt eine saubere Fehlermeldung. Der Papierkorb darf 200 MB über das Limit, damit
  Benutzer auch bei vollem Postfach noch löschen können.
- Mailprogramme mit IMAP-QUOTA-Unterstützung (Thunderbird, Apple Mail) zeigen die Belegung an.
- Die Belegung führt Dovecot laufend mit (`count`-Backend). Die Weboberfläche fragt sie über
  die interne Dovecot-HTTP-API ab, ein Request für alle Postfächer, auch bei großen Datenmengen schnell.
  Beim allerersten Abruf eines bestehenden, großen Postfachs muss Dovecot einmalig die Indizes
  aufbauen, das kann einen Moment dauern.

```bash
# Belegung eines Postfachs auf der Kommandozeile
docker compose exec mail doveadm quota get -u anna@example.com
```

### Quota aus LDAP (Standard-Attribut, kein Zusatzschema)

Welcher Wert gilt: **LDAP → Wert aus der Weboberfläche → Standard**.

In `.env` wird ein vorhandenes Attribut gewählt, voreingestellt `LDAP_QUOTA_ATTR=description`
(gehört zu `person`/`inetOrgPerson`, ist also bei jedem Benutzer verfügbar). Beim Benutzer
einfach einen Wert ergänzen:

```
description: quota=10G
```

- Erlaubt: `quota=10G`, `quota=500M`, `quota=1,5G`, `quota=0` (unbegrenzt), auch ohne
  `quota=` (`10G`, `2048` = MB).
- `description` ist mehrwertig: normale Beschreibungen daneben stören nicht, sie werden ignoriert.
- Fehlt ein gültiger Wert, gilt der Wert aus der Weboberfläche bzw. der Standard.

Die Weboberfläche liest die Werte alle `LDAP_QUOTA_SYNC_SECONDS` (Standard 5 Minuten) und bei
jedem Aufruf der Seite „Postfächer" und schreibt sie in die SQLite-Datenbank, aus der Dovecot
liest. Ein ungültiger Text in LDAP kann so die Zustellung nie stören. Ist LDAP kurz nicht
erreichbar, bleiben die zuletzt gelesenen Werte gültig.

Statt `description` geht jedes andere Attribut, das du sonst nicht nutzt, z. B. `employeeType`.

Prüfen, welches Limit Dovecot für einen Benutzer sieht:
```bash
docker compose exec mail doveadm user anna@example.com    # Zeile quota_rule
```

## Bewusst (noch) nicht enthalten

- **Spamfilter / Virenscanner** (z. B. rspamd) und **DKIM-Signatur** – ohne DKIM landen
  ausgehende Mails bei Gmail/Outlook häufiger im Spam. Nächster sinnvoller Ausbauschritt.
- Webmail, Quota-Warnmails (z. B. bei 90 % Belegung).

## Backup

Wichtig sind die Volumes `mail_data` (Postfächer), `dav_data` (Kalender/Kontakte)
und `config_db` (Domains/Aliase).

## Hinweise

- Das `mail`-Image basiert bewusst auf **Debian bookworm**: Debian trixie bringt Dovecot 2.4
  mit inkompatibler Konfigurationssyntax.
- Ein `%`-Zeichen im LDAP-Bind-Passwort vermeiden (Radicales Config-Parser kann darüber stolpern).
- Zertifikat: Caddy erneuert automatisch, der Mail-Container prüft alle 6 h und lädt Postfix/Dovecot neu.
