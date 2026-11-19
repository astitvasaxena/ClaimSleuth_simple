# Main image: dashboard + Google ADK agents + MCP server (requirements/main.txt).
# The dashboard launcher expects the interpreter at <project root>/.venv/bin/python, so the venv lives there.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN useradd --uid 10001 --create-home claimsleuth
WORKDIR /app
COPY requirements/main.txt requirements/main.txt
RUN python -m venv /app/.venv && /app/.venv/bin/pip install -r requirements/main.txt
COPY pyproject.toml README.md ./
COPY src src
COPY data data
RUN /app/.venv/bin/pip install -e . \
 && mkdir -p /app/runtime && chown -R claimsleuth:claimsleuth /app/runtime
USER claimsleuth
ENV PATH=/app/.venv/bin:$PATH CLAIMSLEUTH_HOST=0.0.0.0 CLAIMSLEUTH_RUNTIME_DIR=/app/runtime
EXPOSE 8110
# Offline unless EXECUTION_MODE=live is passed in (see docker-compose.yml / deploy/.env).
CMD ["python", "-m", "claimsleuth.dashboard", "--port", "8110"]
