FROM ghcr.io/astral-sh/uv:0.10.12 AS uv
FROM python:3.12.9-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /deps
COPY pyproject.toml uv.lock ./
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
RUN uv sync --frozen --no-install-project
ENV PATH=/opt/venv/bin:$PATH
ENV PYTHONDONTWRITEBYTECODE=1
