#!/bin/sh
set -eu

# Radicale erwartet den Platzhalter {0} statt %s
FILTER=$(printf '%s' "$LDAP_USER_FILTER" | sed 's/%s/{0}/g')

cat > /etc/radicale/config <<EOF
[server]
hosts = 0.0.0.0:5232

[auth]
type = ldap
ldap_uri = ${LDAP_URI}
ldap_base = ${LDAP_BASE_DN}
ldap_reader_dn = ${LDAP_BIND_DN}
ldap_secret = ${LDAP_BIND_PW}
ldap_filter = ${FILTER}
# Login = Mailadresse, immer kleingeschrieben
lc_username = True
realm = CalDAV/CardDAV

[rights]
# Jeder sieht nur seine eigenen Kalender/Adressbücher
type = owner_only

[storage]
filesystem_folder = /data/collections

[web]
type = internal

[logging]
level = info
EOF
chmod 600 /etc/radicale/config

exec python -m radicale --config /etc/radicale/config
