# Bewusst bookworm: Debian 13 (trixie) bringt Dovecot 2.4 mit komplett
# neuer Konfigurationssyntax – die Configs hier sind für Dovecot 2.3.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        postfix postfix-ldap postfix-sqlite \
        dovecot-core dovecot-imapd dovecot-lmtpd dovecot-ldap dovecot-sqlite \
        dovecot-sieve dovecot-managesieved \
        supervisor gettext-base openssl ca-certificates sqlite3 tzdata \
    && rm -rf /var/lib/apt/lists/*

# Virtueller Mail-Benutzer, dem alle Postfächer gehören
RUN groupadd -g 5000 vmail \
    && useradd -u 5000 -g vmail -d /var/mail -s /usr/sbin/nologin vmail

COPY postfix/  /templates/postfix/
COPY dovecot/  /templates/dovecot/
COPY supervisord.conf /etc/supervisor/conf.d/mail.conf
COPY entrypoint.sh cert-watch.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/cert-watch.sh

EXPOSE 25 465 587 993 4190
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
