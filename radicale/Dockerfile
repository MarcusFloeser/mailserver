FROM python:3.12-alpine

RUN pip install --no-cache-dir "radicale>=3.3,<4" ldap3 \
    && adduser -D -h /data -u 5232 radicale \
    && mkdir -p /etc/radicale /data && chown radicale /data /etc/radicale

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER radicale
VOLUME /data
EXPOSE 5232
ENTRYPOINT ["/entrypoint.sh"]
