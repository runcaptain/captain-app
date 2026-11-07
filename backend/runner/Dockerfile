# The Captain runner image: `cst` is the entrypoint, so a compute job passes cst arguments only, e.g.
#   docker run IMAGE -w <workspace> backtest run <name>
# with CST_STORAGE and CST_WORKSPACE set: the job attaches to that workspace in its bucket (never creates one).
# Cloud credentials come from the platform (ECS task role, Batch job role, workload identity), never from the image.
#
# Serving the daemon remotely: run it with `serve --host 0.0.0.0 --port 8765 --token "$CST_SERVE_TOKEN"` behind a
# TLS proxy. Build: runner/build.sh (linux/amd64).
FROM python:3.12-slim
WORKDIR /app
COPY core/pyproject.toml /app/core/pyproject.toml
COPY core/captain_search /app/core/captain_search
# tini is process 1: it passes the platform's stop signal (ECS StopTask, Batch terminate, Cloud Run cancel) on to cst.
# Without it cst is process 1, which ignores SIGTERM, so a cancelled job keeps running until the platform kills it.
RUN apt-get update && apt-get install -y --no-install-recommends tini && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "/app/core[s3,gcs,azure,postgres]" \
 && useradd --create-home --uid 10001 cst
USER cst
ENV CST_HOME=/home/cst/.cst PYTHONUNBUFFERED=1 CST_ALLOW_REMOTE=1
EXPOSE 8765
COPY runner/entrypoint.sh /usr/local/bin/cst-entrypoint
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/cst-entrypoint"]
CMD ["--help"]
