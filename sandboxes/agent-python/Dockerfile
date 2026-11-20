# Agent sandbox for Python repos (TODO 1D2c). The OpenHands agent server, pinned by digest
# (SDK 1.60.1, spike 1C2), plus the test tools the agent needs. Its network is internal
# (gateway only), so the agent cannot pip install at run time: everything it needs to run
# the repo's tests must be in this image. No secrets are baked in.
FROM ghcr.io/openhands/agent-server@sha256:b43ee580c2177280c81cb807c9a559d7a14aa13b48dba5040132a6a637bb6d55

USER root
RUN python3 -m pip install --no-cache-dir "pytest>=8,<9" "pytest-cov>=5,<6"
USER openhands
