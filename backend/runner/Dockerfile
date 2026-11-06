# Remote runner: `cst serve` inside the network that holds the databases.
# Put it behind a TLS proxy. Secrets come from the environment or a secrets manager, never a keychain.
FROM python:3.12-slim
WORKDIR /app
COPY core /app/core
RUN pip install --no-cache-dir -e "/app/core[s3,postgres]"
ENV CST_HOME=/data CST_ALLOW_REMOTE=1
VOLUME /data
EXPOSE 8765
# CST_SERVE_TOKEN must be set per deployment. Each request is logged with its SQL by the proxy.
CMD ["sh", "-c", "test -n \"$CST_SERVE_TOKEN\" && cst serve --host 0.0.0.0 --port 8765 --token \"$CST_SERVE_TOKEN\""]
