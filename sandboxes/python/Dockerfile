# Build-and-test sandbox for Python repos (TODO 1C1). One container per task.
# No secrets are baked in or mounted. The only network a sandbox gets is the
# grafter-gw Docker network to the LiteLLM gateway (deploy/litellm).
FROM python:3.12.15-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && pip install "pytest>=8,<9" "pytest-cov>=5,<6" \
 && useradd --create-home --uid 10001 --shell /usr/sbin/nologin sandbox \
 && mkdir /workspace && chown sandbox:sandbox /workspace

USER sandbox
WORKDIR /workspace
CMD ["python", "-m", "pytest", "-q"]
