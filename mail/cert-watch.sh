#!/bin/bash
# Übernimmt das von Caddy verwaltete Zertifikat und lädt Postfix/Dovecot
# neu, sobald Caddy es erneuert hat.
#   --once : nur einmal kopieren (Exit 1, wenn noch kein Zertifikat da ist)

[ "${TLS_MODE:-caddy}" = "selfsigned" ] && { [ "${1:-}" = "--once" ] && exit 0; exec sleep infinity; }

sync_cert() {
    local crt key
    crt=$(find /caddy-data/caddy/certificates -type f -name "${MAIL_HOSTNAME}.crt" 2>/dev/null | head -n1)
    [ -n "$crt" ] || return 1
    key="${crt%.crt}.key"
    [ -f "$key" ] || return 1

    if ! cmp -s "$crt" /certs/fullchain.pem 2>/dev/null; then
        install -m 644 "$crt" /certs/fullchain.pem
        install -m 600 "$key" /certs/privkey.pem
        echo "[cert-watch] Zertifikat aktualisiert"
        return 2
    fi
    return 0
}

if [ "${1:-}" = "--once" ]; then
    sync_cert; rc=$?
    [ $rc -eq 1 ] && exit 1 || exit 0
fi

while true; do
    sleep 21600   # alle 6 Stunden
    sync_cert
    if [ $? -eq 2 ]; then
        postfix reload || true
        doveadm reload || true
    fi
done
