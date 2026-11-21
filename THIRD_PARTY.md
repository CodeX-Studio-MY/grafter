# Third-party notices

Code copied or adapted from other projects is listed here with its licence.

| Project | Licence | What we use | Where |
|---|---|---|---|
| — | — | Nothing copied yet | — |

Runtime dependencies (installed, not copied): PyJWT (MIT) with cryptography (Apache-2.0 or BSD-3-Clause), pydantic (MIT), PyYAML (MIT), Typer (MIT), OpenHands Software Agent SDK (optional `openhands` extra, pinned 1.60.1: `openhands-sdk`, `openhands-tools`, `openhands-workspace`, `openhands-agent-server`; MIT per the upstream `LICENSE` at tag `v1.60.1`, wheels carry no licence metadata; no `enterprise/` code). Its tree (about 200 packages, audited with `pip-licenses`, 2026-11-20) is permissive except: `func_timeout` (LGPL: classifier says v2, bundled licence file is v3; pulled in by `openhands-tools`, installed unmodified, never copied), and `certifi`, `orjson`, `tqdm` (MPL-2.0 parts, installed unmodified). `litellm` is the open-source package only (no `litellm-enterprise`).

Services run as unmodified container images (not copied, not linked): LiteLLM proxy `ghcr.io/berriai/litellm` (MIT; Grafter uses only open-source features, never the `enterprise/` directory) and PostgreSQL `postgres` (PostgreSQL Licence). OpenHands agent server `ghcr.io/openhands/agent-server` (MIT, from the same SDK repo), pinned by digest to the `1.60-python` build of 1.60.1.

Scanners run as unmodified container images, pinned by digest (TODO 1E2, D20): Trivy `aquasec/trivy` 0.76.2 (Apache-2.0) and OSV-Scanner `ghcr.io/google/osv-scanner` v2.7.0 (Apache-2.0). Their vulnerability databases are downloaded at run time into a local Docker volume and are not redistributed: the Trivy DB (`mirror.gcr.io/aquasec/trivy-db`) and the OSV databases (`osv-vulnerabilities.storage.googleapis.com`). Semgrep is not used (D20).
