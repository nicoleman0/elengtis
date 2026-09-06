FROM python:3.12-slim

COPY dist/*.whl /tmp/
RUN pip install --no-cache-dir --no-deps /tmp/*.whl \
    && pip install --no-cache-dir "mcp>=1.26,<2" \
    && rm -f /tmp/*.whl

USER 10001:10001
EXPOSE 3000
ENTRYPOINT ["python", "-m", "elengtis.server", "--collector", "/tmp/collector.jsonl", "--scenario", "support-note", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "3000"]
