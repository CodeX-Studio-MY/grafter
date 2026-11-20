"""TODO 1C2 spike: OpenHands SDK agent edits a file inside a Docker container.

Not part of the grafter package. Run from the repo root with the OpenHands venv:

    .venv-oh/Scripts/python spikes/openhands_hello.py      # Windows
    .venv-oh/bin/python spikes/openhands_hello.py          # Linux

Needs: Docker running, the local gateway up (`docker compose up -d` in deploy/litellm).
The container only ever gets a per-run gateway key (models [coder], capped, 1 h), never the
Anthropic key or the master key. The key is deleted at the end.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

from openhands.sdk import LLM, Conversation  # noqa: E402
from openhands.tools.preset.default import get_default_agent  # noqa: E402
from openhands.workspace import DockerWorkspace  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from grafter.gateway import GatewayClient, redact  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = ROOT / "examples" / "sandbox-repo"
TARGET = "tests/test_grading.py"
# 1.60-python index digest; the image inside is SDK 1.60.1 (built 2026-11-05T16:57Z).
IMAGE = (
    "ghcr.io/openhands/agent-server@sha256:"
    "b43ee580c2177280c81cb807c9a559d7a14aa13b48dba5040132a6a637bb6d55"
)
MODEL = "claude-sonnet-5-5"  # models.coder (D15)
BUDGET_USD = 0.50
GATEWAY_HOST = "http://127.0.0.1:4000"  # from this host
GATEWAY_IN_NET = "http://litellm:4000"  # from containers on grafter-gw
NETWORK = "grafter-gw"
WORKDIR = "/workspace/project"

PROMPT = f"""You are working in a Python repo at {WORKDIR}.
Add exactly ONE new pytest test function to {TARGET} that covers behaviour of
src/acme_harvest/grading.py not covered yet. Change no other file.
Then run: cd {WORKDIR} && python -m pytest -q -p no:cacheprovider
and make sure it passes. Finish when the suite is green."""


def master_key() -> str:
    env = ROOT / "deploy" / "litellm" / ".env"
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("LITELLM_MASTER_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("LITELLM_MASTER_KEY missing in deploy/litellm/.env")


def repo_tar() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(SANDBOX.rglob("*")):
            rel = p.relative_to(SANDBOX).as_posix()
            skip = {"__pycache__", ".pytest_cache", ".coverage"}
            if skip & set(rel.split("/")):
                continue
            tar.add(p, arcname=rel, recursive=False)
    return buf.getvalue()


def container_env(name_prefix: str = "agent-server-") -> list[str]:
    out = subprocess.run(
        ["docker", "ps", "--filter", f"name={name_prefix}", "--format", "{{.ID}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    if not out:
        return []
    raw = subprocess.run(
        ["docker", "inspect", out[0], "--format", "{{json .Config.Env}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [e.split("=", 1)[0] for e in json.loads(raw)]


def main() -> int:
    gw = GatewayClient(GATEWAY_HOST, master_key())
    gw.health()
    run_id = f"spike-{uuid.uuid4().hex[:8]}"
    key = gw.create_run_key(run_id, [MODEL], BUDGET_USD, "1h")
    print(f"run key {redact(key)}: models [{MODEL}], ${BUDGET_USD}, 1h")
    started = time.monotonic()
    try:
        llm = LLM(
            usage_id="coder",
            model=f"litellm_proxy/{MODEL}",
            base_url=GATEWAY_IN_NET,
            api_key=SecretStr(key),
            num_retries=2,
        )
        with DockerWorkspace(server_image=IMAGE, network=NETWORK, detach_logs=False) as ws:
            print("container env var names:", container_env())
            with tempfile.TemporaryDirectory() as tmp:
                tar_path = Path(tmp) / "repo.tgz"
                tar_path.write_bytes(repo_tar())
                up = ws.file_upload(tar_path, "/tmp/repo.tgz")
                print("upload:", up.success)
            r = ws.execute_command(
                f"mkdir -p {WORKDIR} && tar -xzf /tmp/repo.tgz -C {WORKDIR} && "
                f"cd {WORKDIR} && git init -q && git add -A && "
                "git -c user.name=spike -c user.email=spike@example.invalid commit -qm base && "
                "python --version && python -m pip --version",
                timeout=120,
            )
            print("prepare:", r.exit_code, r.stdout.strip()[-300:], r.stderr.strip()[-300:])
            r = ws.execute_command(
                f"cd {WORKDIR} && python -m pip install -q pytest pytest-cov 2>&1 | tail -2; "
                "python -m pytest -q -p no:cacheprovider 2>&1 | tail -3",
                timeout=300,
            )
            print("baseline tests:", r.exit_code, r.stdout.strip()[-400:])

            agent = get_default_agent(llm=llm, cli_mode=True)
            conv = Conversation(
                agent=agent,
                workspace=ws,
                max_iteration_per_run=40,
                visualizer=None,
            )
            try:
                conv.send_message(PROMPT)
                conv.run()
                m = conv.conversation_stats.get_combined_metrics()
                usage = m.accumulated_token_usage
                print(
                    f"agent metrics: cost ${m.accumulated_cost:.4f}, prompt "
                    f"{usage.prompt_tokens if usage else '?'}, completion "
                    f"{usage.completion_tokens if usage else '?'}"
                )
            finally:
                conv.close()

            r = ws.execute_command(
                f"cd {WORKDIR} && git status --porcelain && git diff", timeout=60
            )
            print("--- changes inside the container ---")
            print(r.stdout)
            r = ws.execute_command(
                f"cd {WORKDIR} && python -m pytest -q -p no:cacheprovider 2>&1 | tail -3",
                timeout=300,
            )
            print("tests after agent:", r.exit_code, r.stdout.strip()[-400:])
            with tempfile.TemporaryDirectory() as tmp:
                dst = Path(tmp) / "test_grading.py"
                down = ws.file_download(f"{WORKDIR}/{TARGET}", dst)
                changed = down.success and dst.read_text("utf-8") != (SANDBOX / TARGET).read_text(
                    "utf-8"
                )
                print("download:", down.success, "file changed vs host copy:", changed)
    finally:
        try:
            info = gw._request("GET", "/key/info", None, key=key)  # noqa: SLF001
            spend = (info.get("info") or {}).get("spend") if isinstance(info, dict) else None
            print(f"gateway spend for run key: ${spend}")
        except Exception as e:  # spike: report and carry on to delete
            print("key info failed:", type(e).__name__)
        gw.delete_key(key)
        print(f"run key deleted; elapsed {time.monotonic() - started:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
