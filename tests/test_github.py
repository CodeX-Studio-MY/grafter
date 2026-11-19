import json
import re
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

from grafter.github import AppAuth, GitHubClient, GitHubError, scrub
from grafter.prbody import FINGERPRINT_MARKER

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
        threading.Thread(target=self.server.serve_forever, args=(0.02,), daemon=True).start()

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

    @staticmethod
    def page(items: list[Any], query: dict[str, list[str]]) -> list[Any]:
        per_page = int(query.get("per_page", ["30"])[0])
        page = int(query.get("page", ["1"])[0])
        return items[(page - 1) * per_page : page * per_page]

    def api(
        self, method: str, path: str, query: dict[str, list[str]], body: Any
    ) -> tuple[int, Any]:
        if (method, path) == ("GET", "/installation/repositories"):
            repos = [{"full_name": r} for r in self.repos]
            return 200, {"total_count": len(repos), "repositories": self.page(repos, query)}
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)/(labels|pulls|issues/(\d+)/labels)", path)
        if not m:
            return 404, {"message": "Not Found"}
        repo, what, number = m.group(1), m.group(2), m.group(3)
        if (method, what) == ("GET", "labels"):
            labels = [{"name": n} for n in self.labels.get(repo, [])]
            return 200, self.page(labels, query)
        if (method, what) == ("POST", "labels"):
            self.labels.setdefault(repo, []).append(body["name"])
            return 201, body
        if (method, what) == ("POST", "pulls"):
            pulls = self.pulls.setdefault(repo, [])
            pr = {
                "number": len(pulls) + 1,
                "state": "open",
                "title": body["title"],
                "body": body["body"],
                "head": {"ref": body["head"]},
                "base": {"ref": body["base"]},
                "html_url": f"https://github.example/{repo}/pull/{len(pulls) + 1}",
            }
            pulls.append(pr)
            return 201, pr
        if (method, what) == ("GET", "pulls"):
            state = query.get("state", ["open"])[0]
            pulls = [p for p in self.pulls.get(repo, []) if state == "all" or p["state"] == state]
            return 200, self.page(pulls, query)
        if method == "POST" and number:
            pr = self.pulls[repo][int(number) - 1]
            pr["labels"] = [{"name": n} for n in body["labels"]]
            return 200, pr["labels"]
        return 404, {"message": "Not Found"}

    def add_pr(self, repo: str, head: str, body: str | None, state: str = "open") -> None:
        pulls = self.pulls.setdefault(repo, [])
        pulls.append(
            {"number": len(pulls) + 1, "state": state, "body": body, "head": {"ref": head}}
        )

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
    f.server.server_close()


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


# --- GitHubClient --------------------------------------------------------------

REPO = "acme/harvest-api"


class StaticToken:
    def __init__(self, token: str) -> None:
        self._token = token

    def token(self) -> str:
        return self._token


@pytest.fixture
def client(auth: AppAuth, fake: FakeGitHub) -> GitHubClient:
    return GitHubClient(auth, api_url=fake.url)


def test_client_accepts_any_token_provider(fake: FakeGitHub) -> None:
    fake.issued.append("ghp_static0000000000")
    fake.repos = ["acme/one"]
    c = GitHubClient(StaticToken("ghp_static0000000000"), api_url=fake.url)
    assert c.installation_repos() == ["acme/one"]
    assert fake.headers[0]["authorization"] == "Bearer ghp_static0000000000"
    assert fake.headers[0]["user-agent"] == "grafter"


def test_client_reuses_cached_token(client: GitHubClient, fake: FakeGitHub) -> None:
    client.installation_repos()
    client.installation_repos()
    assert len(fake.issued) == 1


def test_installation_repos_paginates(client: GitHubClient, fake: FakeGitHub) -> None:
    fake.repos = [f"acme/r{i:03d}" for i in range(250)]
    assert client.installation_repos() == fake.repos
    pages = [c[2]["page"] for c in fake.calls if c[1] == "/installation/repositories"]
    assert pages == [["1"], ["2"], ["3"]]


def test_installation_repos_exact_page_boundary(client: GitHubClient, fake: FakeGitHub) -> None:
    fake.repos = [f"acme/r{i:03d}" for i in range(100)]
    assert len(client.installation_repos()) == 100
    assert sum(c[1] == "/installation/repositories" for c in fake.calls) == 2


def test_installation_repos_empty(client: GitHubClient, fake: FakeGitHub) -> None:
    assert client.installation_repos() == []


def test_installation_repos_bad_shape(
    client: GitHubClient, fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fake, "api", lambda *a: (200, ["not", "a", "dict"]))
    with pytest.raises(GitHubError, match="unexpected"):
        client.installation_repos()


def test_ensure_labels_creates_only_missing(client: GitHubClient, fake: FakeGitHub) -> None:
    fake.labels[REPO] = ["grafter", "Tier-1"]
    created = client.ensure_labels(REPO, {"grafter": "5319e7", "tier-1": "fbca04", "x": "000000"})
    assert created == ["x"]
    posts = [c for c in fake.calls if c[0] == "POST" and c[1] == f"/repos/{REPO}/labels"]
    assert [c[3] for c in posts] == [{"name": "x", "color": "000000"}]


def test_ensure_labels_paginates(client: GitHubClient, fake: FakeGitHub) -> None:
    fake.labels[REPO] = [f"l{i}" for i in range(150)]
    assert client.ensure_labels(REPO, {"l149": "ffffff"}) == []


def test_create_pr_then_labels(client: GitHubClient, fake: FakeGitHub) -> None:
    pr = client.create_pr(REPO, "ai/t1", "main", "title", "body", ["grafter", "tier-1"])
    assert pr["number"] == 1
    assert fake.paths()[-2:] == [
        ("POST", f"/repos/{REPO}/pulls"),
        ("POST", f"/repos/{REPO}/issues/1/labels"),
    ]
    assert fake.calls[-2][3] == {"title": "title", "head": "ai/t1", "base": "main", "body": "body"}
    assert fake.calls[-1][3] == {"labels": ["grafter", "tier-1"]}
    assert fake.pulls[REPO][0]["labels"] == [{"name": "grafter"}, {"name": "tier-1"}]


def test_create_pr_without_labels_skips_label_call(client: GitHubClient, fake: FakeGitHub) -> None:
    client.create_pr(REPO, "ai/t1", "main", "t", "b", [])
    assert fake.paths()[-1] == ("POST", f"/repos/{REPO}/pulls")


@pytest.mark.parametrize("head", ["main", "feature/x", "ai", "xai/x", "ai/", ""])
def test_create_pr_refuses_non_ai_head(client: GitHubClient, fake: FakeGitHub, head: str) -> None:
    with pytest.raises(GitHubError, match="ai/"):
        client.create_pr(REPO, head, "main", "t", "b", [])
    assert fake.calls == []


@pytest.mark.parametrize("base", ["", "  "])
def test_create_pr_refuses_empty_base(client: GitHubClient, fake: FakeGitHub, base: str) -> None:
    with pytest.raises(GitHubError, match="base"):
        client.create_pr(REPO, "ai/t1", base, "t", "b", [])
    assert fake.calls == []


def test_create_pr_bad_response(
    client: GitHubClient, fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fake, "api", lambda *a: (201, {"no": "number"}))
    with pytest.raises(GitHubError, match="unexpected"):
        client.create_pr(REPO, "ai/t1", "main", "t", "b", ["grafter"])


@pytest.mark.parametrize("repo", ["acme", "acme/x/y", "../etc/passwd", "acme/x?y=1", ""])
def test_rejects_bad_repo_names(client: GitHubClient, fake: FakeGitHub, repo: str) -> None:
    with pytest.raises(GitHubError, match="repo"):
        client.ensure_labels(repo, {"grafter": "ffffff"})
    assert fake.calls == []


def test_http_error_is_scrubbed(client: GitHubClient, fake: FakeGitHub) -> None:
    client.installation_repos()  # obtain a token first
    leak = f"{fake.issued[0]} ghp_abcdefghijklmnop github_pat_11ABCDEF_xyz"
    fake.fail[("POST", f"/repos/{REPO}/pulls")] = (422, json.dumps({"message": leak}))
    with pytest.raises(GitHubError) as exc:
        client.create_pr(REPO, "ai/t1", "main", "t", "b", [])
    msg = str(exc.value)
    assert exc.value.status == 422
    assert "HTTP 422" in msg
    assert fake.issued[0] not in msg
    assert "ghp_abcdefghijklmnop" not in msg and "github_pat_11ABCDEF_xyz" not in msg
    assert "ghs_***" in msg and "ghp_***" in msg and "gith***" in msg


def test_non_json_response(client: GitHubClient, fake: FakeGitHub) -> None:
    client.installation_repos()
    fake.fail[("GET", "/installation/repositories")] = (200, "<html>")
    with pytest.raises(GitHubError, match="non-JSON"):
        client.installation_repos()


def test_empty_response_body(client: GitHubClient, fake: FakeGitHub) -> None:
    client.installation_repos()
    fake.fail[("GET", "/installation/repositories")] = (200, "")
    with pytest.raises(GitHubError, match="unexpected"):
        client.installation_repos()


def test_scrub_and_repr() -> None:
    assert scrub("x ghs_abcdef y") == "x ghs_*** y"
    assert scrub("eyJhbGciOiJSUzI1NiJ9.eyJpc3MiOiIxIn0.c2ln") == "eyJh***"
    assert scrub("no secrets here") == "no secrets here"
    c = GitHubClient(StaticToken("ghp_secret0000"), api_url="http://x.example/")
    assert "ghp_secret0000" not in repr(c)
    assert c.api_url == "http://x.example"


def test_client_rejects_non_http_url() -> None:
    with pytest.raises(GitHubError, match="http"):
        GitHubClient(StaticToken("t"), api_url="ftp://x")


# --- idempotency ---------------------------------------------------------------

FP = "0123456789abcdef"
MARK = FINGERPRINT_MARKER.format(fp=FP)


def test_find_open_pr_none(client: GitHubClient, fake: FakeGitHub) -> None:
    assert client.find_open_pr(REPO, FP) is None


def test_find_open_pr_matches_marker(client: GitHubClient, fake: FakeGitHub) -> None:
    fake.add_pr(REPO, "ai/other", "body <!-- grafter:fingerprint=ffffffffffffffff -->")
    fake.add_pr(REPO, "ai/t1", f"text\n{MARK}\nfooter")
    pr = client.find_open_pr(REPO, FP)
    assert pr is not None and pr["number"] == 2
    _, path, query, _ = fake.calls[-1]
    assert path == f"/repos/{REPO}/pulls"
    assert query["state"] == ["open"] and query["per_page"] == ["100"]


def test_find_open_pr_ignores_non_ai_heads_and_closed(
    client: GitHubClient, fake: FakeGitHub
) -> None:
    fake.add_pr(REPO, "feature/spoof", MARK)
    fake.add_pr(REPO, "main", MARK)
    fake.add_pr(REPO, "ai/t1", MARK, state="closed")
    fake.add_pr(REPO, "ai/t2", None)
    assert client.find_open_pr(REPO, FP) is None


def test_find_open_pr_needs_exact_marker(client: GitHubClient, fake: FakeGitHub) -> None:
    fake.add_pr(REPO, "ai/t1", f"<!-- grafter:fingerprint={FP}0 -->")
    fake.add_pr(REPO, "ai/t2", f"grafter:fingerprint={FP}")
    assert client.find_open_pr(REPO, FP) is None


def test_find_open_pr_paginates(client: GitHubClient, fake: FakeGitHub) -> None:
    for i in range(150):
        fake.add_pr(REPO, f"ai/t{i}", "no marker")
    fake.add_pr(REPO, "ai/target", MARK)
    pr = client.find_open_pr(REPO, FP)
    assert pr is not None and pr["head"]["ref"] == "ai/target"
    pages = [c[2]["page"] for c in fake.calls if c[1] == f"/repos/{REPO}/pulls"]
    assert pages == [["1"], ["2"]]


def test_find_open_pr_rejects_bad_fingerprint(client: GitHubClient, fake: FakeGitHub) -> None:
    with pytest.raises(GitHubError, match="fingerprint"):
        client.find_open_pr(REPO, "")
    assert fake.calls == []
