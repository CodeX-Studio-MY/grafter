import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from grafter.cli import app
from grafter.gateway import GatewayClient, GatewayError, redact

MASTER = "sk-master-0123456789"
RUN_KEY = "sk-run-abcdefghijkl"


class FakeLiteLLM:
    """Minimal stand-in for the LiteLLM proxy endpoints Grafter uses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None, Any]] = []
        self.fail: dict[str, tuple[int, str]] = {}
        self.chat_response: Any = {"choices": [{"message": {"content": "ok"}}]}
        self.generate_response: Any = {"key": RUN_KEY}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                fake.calls.append((method, self.path, self.headers.get("Authorization"), body))
                if self.path in fake.fail:
                    code, text = fake.fail[self.path]
                    self._send(code, text.encode())
                    return
                routes: dict[str, Any] = {
                    "/health/liveliness": "I'm alive!",
                    "/key/generate": fake.generate_response,
                    "/key/delete": {"deleted_keys": (body or {}).get("keys", [])},
                    "/v1/chat/completions": fake.chat_response,
                }
                if self.path not in routes:
                    self._send(404, b'{"error": "not found"}')
                    return
                self._send(200, json.dumps(routes[self.path]).encode())

            def _send(self, code: int, payload: bytes) -> None:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:  # noqa: N802
                self._handle("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._handle("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def paths(self) -> list[str]:
        return [c[1] for c in self.calls]


@pytest.fixture
def fake() -> Iterator[FakeLiteLLM]:
    f = FakeLiteLLM()
    yield f
    f.server.shutdown()


def test_health_ok(fake: FakeLiteLLM) -> None:
    GatewayClient(fake.url, MASTER).health()
    assert fake.calls[0][:3] == ("GET", "/health/liveliness", None)


def test_create_run_key_sends_scoped_budget(fake: FakeLiteLLM) -> None:
    key = GatewayClient(fake.url, MASTER).create_run_key("r1", ["claude-sonnet-5-5"], 5.0)
    assert key == RUN_KEY
    method, path, auth, body = fake.calls[0]
    assert (method, path, auth) == ("POST", "/key/generate", f"Bearer {MASTER}")
    assert body == {
        "models": ["claude-sonnet-5-5"],
        "max_budget": 5.0,
        "budget_duration": "1d",
        "duration": "1d",
        "key_alias": "grafter-r1",
        "metadata": {"grafter_run_id": "r1"},
    }


@pytest.mark.parametrize("response", [{}, {"key": ""}, {"key": 3}, "text"])
def test_create_run_key_rejects_missing_key(fake: FakeLiteLLM, response: Any) -> None:
    fake.generate_response = response
    with pytest.raises(GatewayError, match="no key"):
        GatewayClient(fake.url, MASTER).create_run_key("r1", ["m"], 1.0)


def test_delete_key_uses_master(fake: FakeLiteLLM) -> None:
    GatewayClient(fake.url, MASTER).delete_key(RUN_KEY)
    assert fake.calls[0] == ("POST", "/key/delete", f"Bearer {MASTER}", {"keys": [RUN_KEY]})


def test_chat_uses_run_key_not_master(fake: FakeLiteLLM) -> None:
    reply = GatewayClient(fake.url, MASTER).chat(RUN_KEY, "claude-sonnet-5-5", "hi")
    assert reply == "ok"
    _, path, auth, body = fake.calls[0]
    assert path == "/v1/chat/completions"
    assert auth == f"Bearer {RUN_KEY}"
    assert body["model"] == "claude-sonnet-5-5"
    assert body["messages"] == [{"role": "user", "content": "hi"}]


def test_chat_bad_shape(fake: FakeLiteLLM) -> None:
    fake.chat_response = {"choices": []}
    with pytest.raises(GatewayError, match="unexpected chat response"):
        GatewayClient(fake.url, MASTER).chat(RUN_KEY, "m", "hi")


def test_http_error_is_scrubbed(fake: FakeLiteLLM) -> None:
    fake.fail["/key/generate"] = (401, f'{{"error": "bad key {MASTER}"}}')
    with pytest.raises(GatewayError) as exc:
        GatewayClient(fake.url, MASTER).create_run_key("r1", ["m"], 1.0)
    msg = str(exc.value)
    assert "HTTP 401" in msg
    assert MASTER not in msg
    assert "sk-m***" in msg


def test_unreachable_gateway() -> None:
    with pytest.raises(GatewayError, match="unreachable"):
        GatewayClient("http://127.0.0.1:1", MASTER, timeout=2).health()


def test_rejects_non_http_url() -> None:
    with pytest.raises(GatewayError, match="http"):
        GatewayClient("file:///etc/passwd", MASTER)


def test_repr_and_redact_hide_keys() -> None:
    assert MASTER not in repr(GatewayClient("http://x.example", MASTER))
    assert redact(RUN_KEY) == "sk-r***"
    assert redact("") == ""


def test_non_json_body_returned_as_text(fake: FakeLiteLLM) -> None:
    fake.fail["/health/liveliness"] = (200, "plain text")
    GatewayClient(fake.url, MASTER).health()


# --- CLI ---------------------------------------------------------------------

runner = CliRunner()


def write_config(tmp_path: Path, url: str) -> Path:
    cfg = tmp_path / "grafter.yaml"
    cfg.write_text(
        "repos:\n"
        "  - {name: api, github: acme/api, language: python, test_command: pytest,"
        " test_paths: [tests/**]}\n"
        f"llm_gateway_url: {url}\n",
        encoding="utf-8",
    )
    return cfg


def test_cli_check_happy_path(
    fake: FakeLiteLLM, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITELLM_MASTER_KEY", MASTER)
    result = runner.invoke(
        app, ["gateway", "check", "--config", str(write_config(tmp_path, fake.url))]
    )
    assert result.exit_code == 0, result.output
    assert "ok: claude-sonnet-5-5 replied 'ok'" in result.output
    assert MASTER not in result.output and RUN_KEY not in result.output
    assert fake.paths() == [
        "/health/liveliness",
        "/key/generate",
        "/v1/chat/completions",
        "/key/delete",
    ]
    gen = fake.calls[1][3]
    assert gen["models"] == ["claude-sonnet-5-5"]
    assert gen["max_budget"] == 0.05
    assert gen["duration"] == "1h"


def test_cli_check_deletes_key_when_chat_fails(
    fake: FakeLiteLLM, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LITELLM_MASTER_KEY", MASTER)
    fake.fail["/v1/chat/completions"] = (400, '{"error": "budget exceeded"}')
    result = runner.invoke(
        app, ["gateway", "check", "--config", str(write_config(tmp_path, fake.url))]
    )
    assert result.exit_code == 1
    assert "budget exceeded" in result.output
    assert fake.paths()[-1] == "/key/delete"


def test_cli_check_needs_master_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
    cfg = write_config(tmp_path, "http://127.0.0.1:1")
    result = runner.invoke(app, ["gateway", "check", "--config", str(cfg)])
    assert result.exit_code == 1
    assert "LITELLM_MASTER_KEY is not set" in result.output


def test_cli_check_invalid_config(tmp_path: Path) -> None:
    result = runner.invoke(app, ["gateway", "check", "--config", str(tmp_path / "missing.yaml")])
    assert result.exit_code == 1
    assert "invalid config" in result.output


def test_dropped_connection_is_gateway_error() -> None:
    import socket

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def accept_and_close() -> None:
        conn, _ = srv.accept()
        conn.close()

    threading.Thread(target=accept_and_close, daemon=True).start()
    port = srv.getsockname()[1]
    with pytest.raises(GatewayError, match="unreachable"):
        GatewayClient(f"http://127.0.0.1:{port}", MASTER, timeout=5).health()
    srv.close()
