FROM python:3.12-slim
LABEL org.opencontainers.image.title="Salesforce Release Impact Check"       org.opencontainers.image.description="Rates the impact of the next Salesforce release on your org (High/Medium/Low) and writes technical and business documentation"       org.opencontainers.image.source="https://github.com/gabrielolv/sf-release-check"

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 RC_DATA_DIR=/data \
    DISABLE_AUTOUPDATER=1 PATH=/home/app/.local/bin:$PATH
# DejaVu fonts give the PDF documents full Unicode coverage; curl is for the Claude Code installer
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Corporate TLS inspection (Zscaler etc.): trust any company root CA placed in certs/*.crt (see certs/README.md).
# The env vars point pip, requests/httpx (Anthropic SDK), curl and Claude Code at the combined system bundle.
COPY certs/ /usr/local/share/ca-certificates/extra/
RUN update-ca-certificates
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    PIP_CERT=/etc/ssl/certs/ca-certificates.crt NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --create-home --uid 10001 app && mkdir -p /data && chown app:app /data /app

# Official Claude Code CLI - used when you connect with a Claude account (claude setup-token).
# Pin a version with: docker compose build --build-arg CLAUDE_CODE_VERSION=<x.y.z>
ARG CLAUDE_CODE_VERSION=stable
USER app
RUN curl -fsSL https://claude.ai/install.sh | bash -s ${CLAUDE_CODE_VERSION} && claude --version

COPY --chown=app:app scripts ./scripts
COPY --chown=app:app config ./config
COPY --chown=app:app webapp ./webapp
COPY --chown=app:app .claude/agents ./.claude/agents
COPY --chown=app:app .claude/skills ./.claude/skills
COPY --chown=app:app --chmod=755 docker/entrypoint.sh /usr/local/bin/entrypoint.sh

VOLUME ["/data"]
EXPOSE 1717
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:1717/static/app.css')" || exit 1
# Extra root CAs can also be mounted at runtime: -v ./certs:/certs:ro (see docker/entrypoint.sh)
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
# 0.0.0.0 inside the container; docker-compose publishes it on the host's 127.0.0.1 only
CMD ["uvicorn", "webapp.app:app", "--host", "0.0.0.0", "--port", "1717", "--proxy-headers"]
