# Deminic app image (used by `docker compose --profile full up -d --build`).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
RUN pip install uv && uv pip install --system -r pyproject.toml

COPY app ./app
COPY static ./static
COPY templates ./templates
COPY scripts ./scripts
COPY stress ./stress
COPY run.py ./

RUN useradd --create-home --uid 10001 deminic \
 && mkdir -p /app/data \
 && chown -R deminic:deminic /app
USER deminic

EXPOSE 8700 8743
HEALTHCHECK --interval=20s --timeout=5s --start-period=40s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8700/healthz', timeout=4).status == 200 else 1)"
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "run.py"]
