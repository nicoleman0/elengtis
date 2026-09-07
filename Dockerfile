FROM node:22-alpine AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS base
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ ./src/
COPY --from=frontend /build/src/elengtis/static ./src/elengtis/static
RUN pip install --no-cache-dir uv && uv sync --locked --no-dev
ENV PATH="/app/.venv/bin:$PATH"

FROM base AS web
EXPOSE 8000
CMD ["elengtis", "serve", "--host", "0.0.0.0", "--port", "8000"]

FROM docker:27-cli AS docker-client

FROM base AS runner
COPY --from=docker-client /usr/local/bin/docker /usr/local/bin/docker
CMD ["elengtis", "worker"]
