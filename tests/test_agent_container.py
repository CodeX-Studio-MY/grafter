"""AgentContainer (TODO 1D2c) with a fake `docker`, and the relay script run for real."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from grafter.config import OpenHands
from grafter.engines import agent_container as ac
from grafter.engines.agent_container import (
    RELAY_SCRIPT,
    SESSION_KEY_ENV,
    AgentContainer,
    AgentContainerError,
)

FAKE = r"""
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_DOCKER_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"args": args, "key": os.environ.get("OH_SESSION_API_KEYS_0")}) + "\n")
if args[:2] == ["network", "inspect"]:
    mode = os.environ.get("FAKE_NET", "true")
    if mode == "missing":
        sys.stderr.write("network not found\n"); sys.exit(1)
    print(mode); sys.exit(0)
if args[0] == "run":
    name = args[args.index("--name") + 1]
    if os.environ.get("FAKE_RUN_FAIL", "") and name.startswith(os.environ["FAKE_RUN_FAIL"]):
        sys.stderr.write("boom\n"); sys.exit(125)
    print("cid"); sys.exit(0)
if args[0] == "logs":
    print("agent log line"); sys.exit(0)
sys.exit(0)
"""


@pytest.fixture
def fake_docker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], Path]:
    script = tmp_path / "fake_docker.py"
    script.write_text(FAKE, encoding="utf-8")
    log = tmp_path / "docker.log"
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(log))
    monkeypatch.delenv("FAKE_NET", raising=False)
    monkeypatch.delenv("FAKE_RUN_FAIL", raising=False)
    monkeypatch.delenv(SESSION_KEY_ENV, raising=False)
    return [sys.executable, str(script)], log


def entries(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text("utf-8").splitlines()]


@pytest.fixture
def health_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, int]]:
    """Stands in for relay -> agent server: answers GET /health on the port free_port picks."""
    state = {"status": 200}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: object) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            self.send_response(state["status"] if self.path == "/health" else 404)
            self.end_headers()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(ac, "free_port", lambda: srv.server_address[1])
    yield state
    srv.shutdown()


def settings(**kw: Any) -> OpenHands:
    return OpenHands(health_timeout_s=5, **kw)


def test_start_runs_agent_on_internal_net_behind_relay(
    fake_docker: tuple[list[str], Path], health_server: dict[str, int]
) -> None:
    docker, log = fake_docker
    with AgentContainer(settings(), docker=docker).start() as ep:
        assert ep.url.startswith("http://127.0.0.1:")
        assert len(ep.session_key) >= 32
        port = ep.url.rsplit(":", 1)[1]
    e = entries(log)
    kinds = [x["args"][0] if x["args"][0] != "network" else " ".join(x["args"][:2]) for x in e]
    assert kinds == ["network inspect", "run", "run", "network connect", "rm", "rm"]

    agent = e[1]["args"]
    assert agent[agent.index("--name") + 1] == ep.agent
    assert agent[agent.index("--network") + 1] == "grafter-gw"
    assert "-p" not in agent and "--publish" not in agent  # nothing published from the agent
    for flag in ("--cap-drop ALL", "--security-opt no-new-privileges", "--memory 4g", "--cpus 2"):
        assert flag in " ".join(agent)
    envs = [agent[i + 1] for i, a in enumerate(agent) if a == "-e"]
    assert envs == [SESSION_KEY_ENV, "LITELLM_LOCAL_MODEL_COST_MAP=True"]  # key: name only
    assert ep.session_key not in " ".join(agent)
    assert e[1]["key"] == ep.session_key  # the docker CLI got it through its environment

    relay = e[2]["args"]
    assert relay[relay.index("--network") + 1] == "bridge"
    assert relay[relay.index("-p") + 1] == f"127.0.0.1:{port}:8000"
    for flag in ("--read-only", "--cap-drop ALL", "--user 65534:65534"):
        assert flag in " ".join(relay)
    assert relay[-3:] == [ep.agent, "8000", "8000"]
    assert e[2]["key"] is None and ep.session_key not in " ".join(relay)

    assert e[3]["args"] == ["network", "connect", "grafter-gw", ep.relay]
    assert [x["args"] for x in e[4:]] == [["rm", "-f", ep.relay], ["rm", "-f", ep.agent]]


@pytest.mark.parametrize(("mode", "msg"), [("false", "not internal"), ("missing", "not found")])
def test_refuses_unless_network_is_internal(
    fake_docker: tuple[list[str], Path], monkeypatch: pytest.MonkeyPatch, mode: str, msg: str
) -> None:
    docker, log = fake_docker
    monkeypatch.setenv("FAKE_NET", mode)
    with pytest.raises(AgentContainerError, match=msg):
        with AgentContainer(settings(), docker=docker).start():
            pass
    assert [x["args"][:2] for x in entries(log)] == [["network", "inspect"]]  # nothing started


def test_relay_failure_removes_both(
    fake_docker: tuple[list[str], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    docker, log = fake_docker
    monkeypatch.setenv("FAKE_RUN_FAIL", "grafter-relay-")
    with pytest.raises(AgentContainerError, match="start relay failed: boom"):
        with AgentContainer(settings(), docker=docker).start():
            pass
    rms = [x["args"] for x in entries(log) if x["args"][0] == "rm"]
    assert len(rms) == 2 and rms[0][2].startswith("grafter-relay-")


def test_unhealthy_times_out_with_logs_and_cleans_up(
    fake_docker: tuple[list[str], Path],
    health_server: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker, log = fake_docker
    health_server["status"] = 503
    with pytest.raises(AgentContainerError) as exc:
        with AgentContainer(settings(), docker=docker).start():
            pass
    assert "not healthy after 5s" in str(exc.value) and "agent log line" in str(exc.value)
    assert [x["args"][0] for x in entries(log)][-2:] == ["rm", "rm"]


def test_error_inside_block_still_cleans_up(
    fake_docker: tuple[list[str], Path], health_server: dict[str, int]
) -> None:
    docker, log = fake_docker
    with pytest.raises(RuntimeError, match="agent blew up"):
        with AgentContainer(settings(), docker=docker).start():
            raise RuntimeError("agent blew up")
    assert [x["args"][0] for x in entries(log)][-2:] == ["rm", "rm"]


# --- the relay script itself -----------------------------------------------------------


def _free() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_relay_script_forwards_both_ways() -> None:
    upstream = socket.socket()
    upstream.bind(("127.0.0.1", 0))
    upstream.listen(1)
    up_port = upstream.getsockname()[1]

    def echo() -> None:
        conn, _ = upstream.accept()
        with conn:
            while data := conn.recv(65536):
                conn.sendall(data.upper())

    threading.Thread(target=echo, daemon=True).start()
    listen = _free()
    relay = subprocess.Popen(
        [sys.executable, "-c", RELAY_SCRIPT, "127.0.0.1", str(up_port), str(listen)]
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                client = socket.create_connection(("127.0.0.1", listen), timeout=2)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        with client:
            payload = b"hello relay " * 10_000  # > one read
            client.sendall(payload)
            got = b""
            while len(got) < len(payload):
                got += client.recv(65536)
            assert got == payload.upper()
    finally:
        relay.terminate()
        relay.wait(10)
        upstream.close()


def test_relay_closes_client_when_upstream_is_down() -> None:
    dead = _free()  # nothing listens here
    listen = _free()
    relay = subprocess.Popen(
        [sys.executable, "-c", RELAY_SCRIPT, "127.0.0.1", str(dead), str(listen)]
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                client = socket.create_connection(("127.0.0.1", listen), timeout=2)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.1)
        with client:
            client.settimeout(5)
            assert client.recv(10) == b""  # closed, no hang
    finally:
        relay.terminate()
        relay.wait(10)
