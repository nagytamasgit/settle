# The engine image: `docker build -t settle . && docker run --rm settle version`
#
# This is the expert's product — CLI and API, no UI, no database. The browser
# app has its own image at app/Dockerfile.
FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies come from pyproject.toml rather than a hand-written list here.
# The previous version repeated the list, which meant adding an extra silently
# produced an image missing a package — a failure that shows up in production
# and nowhere in CI. The uv cache mount keeps rebuilds fast without the
# correctness risk of a second source of truth.
COPY pyproject.toml README.md LICENSE COMMERCIAL.md ./
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/venv && \
    VIRTUAL_ENV=/opt/venv uv pip install --no-cache ".[api]"


FROM python:3.12-slim AS runtime

# The engine reads two CSVs and writes files. It has no business running as
# root, and nothing it does needs a shell.
RUN useradd --create-home --uid 10001 settle

COPY --from=build /opt/venv /opt/venv

# PolyForm Noncommercial requires the terms to travel with every copy of the
# software, and an image someone pulls is a copy.
COPY --from=build /app/LICENSE /app/COMMERCIAL.md /usr/share/doc/settle/

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER settle
WORKDIR /data

EXPOSE 8000

ENTRYPOINT ["settle"]
CMD ["--help"]
