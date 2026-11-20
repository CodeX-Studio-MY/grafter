"""Agent-server container that can reach only the gateway (TODO 1D2c, spike 1C2 finding 2).

    host --127.0.0.1:P--> relay (bridge + grafter-gw) --> agent server (grafter-gw only)
                                                          --> litellm --> provider

`grafter-gw` is an `internal` Docker network: no route out. The agent server lives only
there, so agent-written code reaches LiteLLM and nothing else. An internal network cannot
publish ports, so a tiny TCP relay (our code only, hardened, read-only) sits on both the
default bridge and `grafter-gw` and publishes the agent server on 127.0.0.1. The agent
server requires a random per-container session key; only Grafter holds it.

Fails closed: if the network is missing or not internal, nothing starts.
"""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from grafter.config import OpenHands

AGENT_PORT = 8000
SESSION_KEY_ENV = "OH_SESSION_API_KEYS_0"
NOBODY = "65534:65534"
DOCKER_TIMEOUT_S = 180

# TCP relay: listen on argv[3], forward each connection to argv[1]:argv[2]. Plain bytes both
# ways, so HTTP and WebSocket both pass. Standard library only.
RELAY_SCRIPT = """
import asyncio, sys
host, port, listen = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
async def pipe(r, w):
    try:
        while True:
            data = await r.read(65536)
            if not data:
                break
            w.write(data)
            await w.drain()
    except Exception:
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass
async def handle(cr, cw):
    try:
        ur, uw = await asyncio.open_connection(host, port)
    except Exception:
        cw.close()
        return
    await asyncio.gather(pipe(cr, uw), pipe(ur, cw))
async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", listen)
    async with server:
        await server.serve_forever()
asyncio.run(main())
""".strip()


class AgentContainerError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentEndpoint:
    url: str  # http://127.0.0.1:<port>, via the relay
    session_key: str  # X-Session-API-Key for the agent server
    agent: str  # container names, for logs
    relay: str


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class AgentContainer:
    def __init__(self, settings: OpenHands, docker: Sequence[str] | None = None) -> None:
        self.s = settings
        self.docker = list(docker) if docker else [shutil.which("docker") or "docker"]

    def _docker(
        self, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*self.docker, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DOCKER_TIMEOUT_S,
            env=env,
            shell=False,
            check=False,
        )

    def _must(self, what: str, *args: str, env: dict[str, str] | None = None) -> str:
        proc = self._docker(*args, env=env)
        if proc.returncode != 0:
            raise AgentContainerError(f"{what} failed: {proc.stderr.strip()[-500:]}")
        return proc.stdout.strip()

    def check_network(self) -> None:
        proc = self._docker("network", "inspect", self.s.network, "--format", "{{.Internal}}")
        if proc.returncode != 0:
            raise AgentContainerError(
                f"network {self.s.network} not found; start the gateway (deploy/litellm)"
            )
        if proc.stdout.strip() != "true":
            raise AgentContainerError(
                f"network {self.s.network} is not internal: the agent could reach the "
                "internet (1D2c); recreate it: docker compose down && docker compose up -d"
            )

    def _wait_healthy(self, url: str) -> None:
        deadline = time.monotonic() + self.s.health_timeout_s
        last = ""
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"{url}/health", timeout=5) as resp:  # noqa: S310
                    if resp.status == 200:
                        return
            except (OSError, urllib.error.URLError) as e:
                last = type(e).__name__
            time.sleep(1)
        raise AgentContainerError(
            f"agent server not healthy after {self.s.health_timeout_s}s ({last})"
        )

    def _logs(self, *names: str) -> str:
        out = []
        for name in names:
            proc = self._docker("logs", "--tail", "15", name)
            tail = (proc.stdout + proc.stderr).strip()[-1500:]
            out.append(f"\n--- {name} logs ---\n{tail}")
        return "".join(out)

    @contextmanager
    def start(self) -> Iterator[AgentEndpoint]:
        self.check_network()
        tag = uuid.uuid4().hex[:12]
        agent, relay = f"grafter-agent-{tag}", f"grafter-relay-{tag}"
        key = secrets.token_urlsafe(32)
        port = free_port()
        try:
            # The key goes in through the environment of the docker CLI, never on argv.
            self._must(
                "start agent server",
                "run",
                "-d",
                "--name",
                agent,
                "--network",
                self.s.network,
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "1024",
                "--memory",
                self.s.memory,
                "--cpus",
                self.s.cpus,
                "--ulimit",
                "nofile=65536:65536",
                "-e",
                SESSION_KEY_ENV,
                self.s.image,
                "--host",
                "0.0.0.0",
                "--port",
                str(AGENT_PORT),
                env={**os.environ, SESSION_KEY_ENV: key},
            )
            self._must(
                "start relay",
                "run",
                "-d",
                "--name",
                relay,
                "--network",
                "bridge",
                "-p",
                f"127.0.0.1:{port}:{AGENT_PORT}",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--read-only",
                "--user",
                NOBODY,
                "--pids-limit",
                "64",
                "--memory",
                "128m",
                self.s.relay_image,
                "python",
                "-c",
                RELAY_SCRIPT,
                agent,
                str(AGENT_PORT),
                str(AGENT_PORT),
            )
            self._must("connect relay", "network", "connect", self.s.network, relay)
            url = f"http://127.0.0.1:{port}"
            try:
                self._wait_healthy(url)
            except AgentContainerError as e:
                raise AgentContainerError(f"{e}{self._logs(agent, relay)}") from None
            yield AgentEndpoint(url, key, agent, relay)
        finally:
            for name in (relay, agent):
                self._docker("rm", "-f", name)
