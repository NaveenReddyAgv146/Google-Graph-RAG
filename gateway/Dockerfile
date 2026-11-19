FROM python:3.12-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.8.13 /uv /uvx /bin/

WORKDIR /workspace

# Copy root workspace configuration and lockfile
COPY pyproject.toml README.md uv.lock ./
# Copy workspace members required for gateway
COPY common ./common
COPY gateway ./gateway

# Set working directory to gateway package
WORKDIR /workspace/gateway

# Synchronize workspace dependencies for gateway package
RUN uv sync --frozen --package gateway

ENV PYTHONPATH=/workspace
EXPOSE 8080

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
