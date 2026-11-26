#!/bin/bash
set -euo pipefail

log() { echo "[entrypoint] $*"; }

for v in MAIL_HOSTNAME DOVEADM_API_KEY LDAP_URI LDAP_BASE_DN LDAP_BIND_DN LDAP_BIND_PW LDAP_USER_FILTER; do
    if [ -z "${!v:-}" ]; then echo "FEHLER: Variable $v ist nicht gesetzt" >&2; exit 1; fi
done
export LDAP_MAIL_ATTR="${LDAP_MAIL_ATTR:-mail}"
export MESSAGE_SIZE_LIMIT="${MESSAGE_SIZE_LIMIT:-52428800}"
export TLS_MODE="${TLS_MODE:-caddy}"
export QUOTA_DEFAULT_MB="${QUOTA_DEFAULT_MB:-5120}"

# Filter je Dienst: Postfix nutzt %s, Dovecot %u
export LDAP_FILTER_POSTFIX="$LDAP_USER_FILTER"
export LDAP_FILTER_DOVECOT="${LDAP_USER_FILTER//%s/%u}"

# --------------------------------------------------------------- Zertifikate
mkdir -p /certs
if [ "$TLS_MODE" = "selfsigned" ]; then
    if [ ! -f /certs/fullchain.pem ]; then
        log "Erzeuge selbstsigniertes Zertifikat für $MAIL_HOSTNAME"
        openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
            -subj "/CN=$MAIL_HOSTNAME" \
            -keyout /certs/privkey.pem -out /certs/fullchain.pem 2>/dev/null
    fi
else
    log "Warte auf Let's-Encrypt-Zertifikat von Caddy für $MAIL_HOSTNAME ..."
    for i in $(seq 1 120); do
        /usr/local/bin/cert-watch.sh --once && break
        sleep 5
    done
    [ -f /certs/fullchain.pem ] || { echo "FEHLER: Kein Zertifikat gefunden. Läuft Caddy und ist DNS/Port 80 korrekt?" >&2; exit 1; }
fi
chmod 600 /certs/privkey.pem

# --------------------------------------------------------------- Templates
VARS='${MAIL_HOSTNAME} ${LDAP_URI} ${LDAP_BASE_DN} ${LDAP_BIND_DN} ${LDAP_BIND_PW} ${LDAP_MAIL_ATTR} ${LDAP_FILTER_POSTFIX} ${LDAP_FILTER_DOVECOT} ${MESSAGE_SIZE_LIMIT} ${QUOTA_DEFAULT_MB} ${DOVEADM_API_KEY}'

render() { envsubst "$VARS" < "$1" > "$2"; }

mkdir -p /etc/postfix/maps
render /templates/postfix/main.cf               /etc/postfix/main.cf
cp     /templates/postfix/master.cf             /etc/postfix/master.cf
render /templates/postfix/ldap-mailboxes.cf     /etc/postfix/maps/ldap-mailboxes.cf
cp     /templates/postfix/sqlite-domains.cf     /etc/postfix/maps/sqlite-domains.cf
cp     /templates/postfix/sqlite-aliases.cf     /etc/postfix/maps/sqlite-aliases.cf
cp     /templates/postfix/sqlite-senders.cf     /etc/postfix/maps/sqlite-senders.cf
chown root:postfix /etc/postfix/maps/*.cf
chmod 640 /etc/postfix/maps/*.cf

render /templates/dovecot/dovecot.conf          /etc/dovecot/dovecot.conf
render /templates/dovecot/dovecot-ldap.conf.ext /etc/dovecot/dovecot-ldap.conf.ext
render /templates/dovecot/dovecot-sql.conf.ext  /etc/dovecot/dovecot-sql.conf.ext
chmod 600 /etc/dovecot/dovecot-*.conf.ext

# --------------------------------------------------------------- Rechte / DB
mkdir -p /var/mail
chown vmail:vmail /var/mail
postfix set-permissions >/dev/null 2>&1 || true

log "Warte auf Konfigurationsdatenbank (wird von der Admin-Oberfläche angelegt) ..."
for i in $(seq 1 60); do
    [ -f /data/config/mail.db ] && break
    sleep 2
done
[ -f /data/config/mail.db ] || { echo "FEHLER: /data/config/mail.db fehlt" >&2; exit 1; }

postfix check || true
log "Start."
exec "$@"
