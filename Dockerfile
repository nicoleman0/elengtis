FROM docker:27-cli AS docker-client

FROM python:3.12-slim
WORKDIR /app

# Install dependencies from the locked manifests before copying source so
# ordinary source changes retain the dependency layer.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "uv>=0.11,<0.12"
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-dev --no-install-project
COPY src/ ./src/
RUN uv sync --locked --no-dev

# Isolated-container campaigns invoke the Docker CLI. Operators must still
# mount a daemon socket and grant its group explicitly when using that target.
COPY --from=docker-client /usr/local/bin/docker /usr/local/bin/docker

RUN useradd --create-home --uid 10001 elengtis && chown -R elengtis:elengtis /app
ENV PATH="/app/.venv/bin:$PATH" HOME=/home/elengtis
USER elengtis

ENTRYPOINT ["elengtis"]
CMD ["--help"]
