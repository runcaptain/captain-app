FROM python:3.12-slim
WORKDIR /app
COPY backend/core /app/backend/core
RUN pip install --no-cache-dir '/app/backend/core[team-server]'
RUN useradd --create-home captain && mkdir /artifacts && chown captain /artifacts
USER captain
EXPOSE 8000
CMD ["cst", "team-server", "serve", "--host", "0.0.0.0", "--port", "8000"]
