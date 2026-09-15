# The browser app.
#
# Built from the REPOSITORY ROOT, not from app/, because settle-app depends on
# settle-engine by path and both trees have to be in the context:
#
#   docker build -f app/Dockerfile -t settle-app .
#
# docker-compose.yml sets context: .. for the same reason.
FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /src

# The engine first, then the app that depends on it. Dependencies are read from
# the two pyproject.toml files rather than repeated here, so adding one cannot
# produce an image that is missing it.
COPY pyproject.toml README.md LICENSE COMMERCIAL.md ./
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/venv && \
    VIRTUAL_ENV=/opt/venv uv pip install --no-cache ".[api]"

COPY app/pyproject.toml app/README.md ./app/
COPY app/src/ ./app/src/
RUN --mount=type=cache,target=/root/.cache/uv \
    VIRTUAL_ENV=/opt/venv uv pip install --no-cache --no-deps ./app && \
    VIRTUAL_ENV=/opt/venv uv pip install --no-cache \
      "jinja2>=3.1" "python-multipart>=0.0.9"


FROM python:3.12-slim AS runtime

# uid 10001 matches the engine image. The data volume must be writable by it;
# docs/DEPLOY.md says so, because a permission error here looks like a database
# error and gets diagnosed as one.
RUN useradd --create-home --uid 10001 settle

COPY --from=build /opt/venv /opt/venv
COPY --from=build /src/LICENSE /src/COMMERCIAL.md /usr/share/doc/settle/

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SETTLE_WEB_DATA_DIR=/data

RUN mkdir -p /data && chown settle:settle /data

USER settle
WORKDIR /data

EXPOSE 8000

# python:3.12-slim ships neither curl nor wget, so a `CMD curl` healthcheck
# would report unhealthy forever and take the container down with it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).status == 200 else 1)"]

ENTRYPOINT ["settle-app"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
