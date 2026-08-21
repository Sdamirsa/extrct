# Container build for the app's compose component (deploy/components/<app-verb>/).
#
# Buildable once `extrct` is on PyPI (the repo's next milestone): drop the
# [tool.uv.sources] path override from pyproject.toml, `uv lock`, commit the lock.
# Until then the path source points outside this build context — run locally with
# `uv run python app.py` instead.
#
# APP_HOST=0.0.0.0 applies INSIDE the container only; the compose file publishes
# the port on 127.0.0.1 on the host, which is where the repo's localhost-only rule
# is enforced.

FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .
ENV PATH="/app/.venv/bin:$PATH" APP_HOST=0.0.0.0 APP_PORT=8080

EXPOSE 8080
CMD ["python", "app.py"]
