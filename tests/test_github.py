import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from grafter.github import AppAuth, GitHubError

APP_ID = 1234
INSTALLATION_ID = 5678
T0 = 1_800_000_000.0
# The fake clock is not wall time; the fake server checks iat/exp against it instead.
NO_TIME_CHECKS = {"verify_exp": False, "verify_iat": False}


@pytest.fixture(scope="module")
def keypair() -> tuple[str, str]:
    """Throwaway RSA key generated per test module; never written to disk."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private, public


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGitHub:
    """Minimal stand-in for the GitHub REST endpoints Grafter uses."""

    def __init__(self, public_key: str, clock: Clock) -> None:
        self.public_key = public_key
        self.clock = clock
        self.calls: list[tuple[str, str, dict[str, list[str]], Any]] = []
        self.headers: list[dict[str, str]] = []
        self.issued: list[str] = []
        self.token_ttl = 3600.0
        self.fail: dict[tuple[str, str], tuple[int, str]] = {}
        self.repos: list[str] = []
        self.labels: dict[str, list[str]] = {}
        self.pulls: dict[str, list[dict[str, Any]]] = {}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def _send(self, code: int, payload: Any) -> None:
                raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self, method: str) -> None:
                url = urlsplit(self.path)
                query = parse_qs(url.query)
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                fake.calls.append((method, url.path, query, body))
                fake.headers.append({k.lower(): v for k, v in self.headers.items()})
                if (method, url.path) in fake.fail:
                    code, text = fake.fail[(method, url.path)]
                    self._send(code, text.encode())
                    return
                auth = self.headers.get("Authorization") or ""
                if not auth.startswith("Bearer "):
                    self._send(401, {"message": "Requires authentication"})
                    return
                code, payload = fake.route(method, url.path, query, body, auth[7:])
                self._send(code, payload)

            def do_GET(self) -> None:  # noqa: N802
                self._handle("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._handle("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    # -- routing ----------------------------------------------------------------

    def route(
        self, method: str, path: str, query: dict[str, list[str]], body: Any, bearer: str
    ) -> tuple[int, Any]:
        if (method, path) == ("POST", f"/app/installations/{INSTALLATION_ID}/access_tokens"):
            return self.issue_token(bearer)
        if bearer not in self.issued:
            return 401, {"message": "Bad credentials"}
        return self.api(method, path, query, body)

    def issue_token(self, bearer: str) -> tuple[int, Any]:
        try:
            claims = jwt.decode(
                bearer, self.public_key, algorithms=["RS256"], options=NO_TIME_CHECKS
            )
        except jwt.PyJWTError as e:
            return 401, {"message": f"bad jwt: {e}"}
        now = self.clock()
        ok = (
            claims["iss"] == str(APP_ID)
            and claims["iat"] <= now <= claims["exp"]
            and claims["exp"] - claims["iat"] <= 600
        )
        if not ok:
            return 401, {"message": f"bad claims: {claims}"}
        token = f"ghs_fake{len(self.issued):04d}abcdefghijklmnop"
        self.issued.append(token)
        return 201, {"token": token, "expires_at": iso(now + self.token_ttl)}

    def api(
        self, method: str, path: str, query: dict[str, list[str]], body: Any
    ) -> tuple[int, Any]:
        return 404, {"message": "Not Found"}

    def paths(self) -> list[tuple[str, str]]:
        return [(c[0], c[1]) for c in self.calls]


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def fake(keypair: tuple[str, str], clock: Clock) -> Iterator[FakeGitHub]:
    f = FakeGitHub(keypair[1], clock)
    yield f
    f.server.shutdown()


@pytest.fixture
def auth(keypair: tuple[str, str], fake: FakeGitHub, clock: Clock) -> AppAuth:
    return AppAuth(APP_ID, INSTALLATION_ID, keypair[0], api_url=fake.url, clock=clock)


# --- AppAuth -------------------------------------------------------------------


def test_jwt_claims(keypair: tuple[str, str], clock: Clock) -> None:
    a = AppAuth(APP_ID, INSTALLATION_ID, keypair[0], clock=clock)
    claims = jwt.decode(a.jwt(), keypair[1], algorithms=["RS256"], options=NO_TIME_CHECKS)
    assert claims == {"iat": int(T0) - 60, "exp": int(T0) + 540, "iss": str(APP_ID)}
    assert jwt.get_unverified_header(a.jwt())["alg"] == "RS256"


def test_token_fetch_sends_jwt_and_headers(auth: AppAuth, fake: FakeGitHub) -> None:
    assert auth.token() == fake.issued[0]
    assert fake.paths() == [("POST", f"/app/installations/{INSTALLATION_ID}/access_tokens")]
    h = fake.headers[0]
    assert h["accept"] == "application/vnd.github+json"
    assert h["x-github-api-version"] == "2022-11-28"
    assert h["user-agent"] == "grafter"


def test_token_cached_until_five_minutes_before_expiry(
    auth: AppAuth, fake: FakeGitHub, clock: Clock
) -> None:
    first = auth.token()
    clock.now = T0 + 3600 - 301
    assert auth.token() == first
    assert len(fake.issued) == 1
    clock.now = T0 + 3600 - 300
    second = auth.token()
    assert second != first
    assert len(fake.issued) == 2
    assert auth.token() == second


def test_token_rejected_by_server(keypair: tuple[str, str], fake: FakeGitHub, clock: Clock) -> None:
    a = AppAuth(APP_ID + 1, INSTALLATION_ID, keypair[0], api_url=fake.url, clock=clock)
    with pytest.raises(GitHubError) as exc:
        a.token()
    assert exc.value.status == 401
    assert "bad claims" in str(exc.value)


@pytest.mark.parametrize(
    "payload",
    [{}, {"token": "ghs_x"}, {"token": "", "expires_at": "2030-01-01T00:00:00Z"}, ["x"]],
)
def test_token_bad_response(
    auth: AppAuth, fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, payload: Any
) -> None:
    monkeypatch.setattr(fake, "issue_token", lambda bearer: (201, payload))
    with pytest.raises(GitHubError, match="token response"):
        auth.token()


def test_token_bad_expiry(auth: AppAuth, fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        fake, "issue_token", lambda bearer: (201, {"token": "ghs_x", "expires_at": "soon"})
    )
    with pytest.raises(GitHubError, match="token response"):
        auth.token()


def test_bad_private_key(clock: Clock) -> None:
    a = AppAuth(APP_ID, INSTALLATION_ID, "not a key", clock=clock)
    with pytest.raises(GitHubError, match="private key"):
        a.jwt()


def test_repr_hides_key_and_token(auth: AppAuth, keypair: tuple[str, str]) -> None:
    tok = auth.token()
    r = repr(auth)
    assert "PRIVATE KEY" not in r and keypair[0] not in r
    assert tok not in r
    assert str(APP_ID) in r and str(INSTALLATION_ID) in r


def test_rejects_non_http_url(keypair: tuple[str, str]) -> None:
    with pytest.raises(GitHubError, match="http"):
        AppAuth(APP_ID, INSTALLATION_ID, keypair[0], api_url="file:///etc/passwd")


def test_unreachable_api(keypair: tuple[str, str]) -> None:
    a = AppAuth(APP_ID, INSTALLATION_ID, keypair[0], api_url="http://127.0.0.1:1", timeout=2)
    with pytest.raises(GitHubError, match="unreachable") as exc:
        a.token()
    assert exc.value.status is None
