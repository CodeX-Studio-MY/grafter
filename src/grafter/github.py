"""GitHub App client (D5: GitHub only in v1). Standard library HTTP plus PyJWT.

Grafter authenticates as a GitHub App installation: a short-lived RS256 JWT signed with
the App's private key buys an installation token (`ghs_...`, valid ~1 h). Tokens and the
key never appear in reprs or error messages.
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

import jwt as pyjwt

from grafter.prbody import marker

API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
# Refresh the installation token this many seconds before GitHub says it expires.
TOKEN_REFRESH_MARGIN = 300.0

_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{4,}|github_pat_[A-Za-z0-9_]{4,}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"
)


class GitHubError(RuntimeError):
    """GitHub is unreachable, rejected a request, or a request was refused locally."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def redact(token: str) -> str:
    return token[:4] + "***" if token else ""


def scrub(text: str) -> str:
    """Mask GitHub tokens (`ghs_`, `ghp_`, `github_pat_`, ...) and JWTs in `text`."""
    return _TOKEN_RE.sub(lambda m: redact(m.group(0)), text)


def _check_url(api_url: str) -> str:
    if not api_url.startswith(("http://", "https://")):
        raise GitHubError(f"GitHub API url must be http(s): {api_url!r}")
    return api_url.rstrip("/")


def _request(
    api_url: str,
    method: str,
    path: str,
    bearer: str,
    body: Any = None,
    params: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> Any:
    url = api_url + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": "grafter",
        "Authorization": f"Bearer {bearer}",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = scrub(e.read()[:300].decode(errors="replace"))
        raise GitHubError(f"{method} {path} -> HTTP {e.code}: {detail}", status=e.code) from None
    except (OSError, http.client.HTTPException) as e:  # URLError, timeouts, dropped conns
        reason = scrub(str(getattr(e, "reason", e)))
        raise GitHubError(f"GitHub unreachable at {api_url}: {reason}") from None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise GitHubError(f"{method} {path} -> non-JSON response") from None


class AppAuth:
    """Installation-token provider for one GitHub App installation."""

    def __init__(
        self,
        app_id: int,
        installation_id: int,
        private_key_pem: str,
        api_url: str = API_URL,
        clock: Callable[[], float] = time.time,
        timeout: float = 30.0,
    ) -> None:
        self.app_id = app_id
        self.installation_id = installation_id
        self.api_url = _check_url(api_url)
        self._key = private_key_pem
        self._clock = clock
        self.timeout = timeout
        self._token: str | None = None
        self._expires_at = 0.0

    def __repr__(self) -> str:
        return (
            f"AppAuth(app_id={self.app_id}, installation_id={self.installation_id}, "
            f"api_url={self.api_url!r})"
        )

    def jwt(self) -> str:
        """App JWT: RS256, backdated 60 s for clock drift, valid 10 min in total."""
        now = int(self._clock())
        claims = {"iat": now - 60, "exp": now + 540, "iss": str(self.app_id)}
        try:
            return pyjwt.encode(claims, self._key, algorithm="RS256")
        except (ValueError, TypeError, pyjwt.PyJWTError):
            raise GitHubError("cannot sign App JWT: invalid private key") from None

    def token(self) -> str:
        """Installation token, cached until 5 min before it expires."""
        if self._token and self._clock() < self._expires_at - TOKEN_REFRESH_MARGIN:
            return self._token
        path = f"/app/installations/{self.installation_id}/access_tokens"
        resp = _request(self.api_url, "POST", path, self.jwt(), timeout=self.timeout)
        token = resp.get("token") if isinstance(resp, dict) else None
        expires = resp.get("expires_at") if isinstance(resp, dict) else None
        if not isinstance(token, str) or not token or not isinstance(expires, str):
            raise GitHubError("unexpected installation token response")
        try:
            expires_at = datetime.fromisoformat(expires).timestamp()
        except ValueError:
            raise GitHubError("unexpected installation token response: bad expires_at") from None
        self._token, self._expires_at = token, expires_at
        return token


class TokenProvider(Protocol):
    def token(self) -> str: ...


_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
PER_PAGE = 100
MAX_PAGES = 100  # 10,000 items; a guard against a server that never stops paging


def _check_repo(repo: str) -> str:
    if not _REPO_RE.fullmatch(repo) or ".." in repo:
        raise GitHubError(f"invalid repo name (want owner/name): {repo!r}")
    return repo


class GitHubClient:
    """The GitHub REST calls Grafter needs. Opens PRs only from `ai/*` branches."""

    def __init__(self, auth: TokenProvider, api_url: str = API_URL, timeout: float = 30.0) -> None:
        self.auth = auth
        self.api_url = _check_url(api_url)
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"GitHubClient(api_url={self.api_url!r})"

    def _request(
        self,
        method: str,
        path: str,
        body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        return _request(self.api_url, method, path, self.auth.token(), body, params, self.timeout)

    def _paginate(
        self, path: str, params: dict[str, Any] | None = None, key: str | None = None
    ) -> list[dict[str, Any]]:
        """GET every page of a list endpoint (`page` param until a short page)."""
        items: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            resp = self._request(
                "GET", path, params={**(params or {}), "per_page": PER_PAGE, "page": page}
            )
            if key is not None:
                resp = resp.get(key) if isinstance(resp, dict) else None
            batch = resp
            if not isinstance(batch, list) or not all(isinstance(i, dict) for i in batch):
                raise GitHubError(f"GET {path} -> unexpected response shape")
            items.extend(batch)
            if len(batch) < PER_PAGE:
                break
        return items

    def installation_repos(self) -> list[str]:
        """Full names (`owner/name`) of every repo the installation can access."""
        repos = self._paginate("/installation/repositories", key="repositories")
        return [str(r["full_name"]) for r in repos]

    def ensure_labels(self, repo: str, labels: dict[str, str]) -> list[str]:
        """Create the labels `repo` lacks (GitHub matches names case-insensitively).
        Returns the names created."""
        path = f"/repos/{_check_repo(repo)}/labels"
        existing = {str(lb.get("name", "")).lower() for lb in self._paginate(path)}
        created: list[str] = []
        for name, color in labels.items():
            if name.lower() not in existing:
                self._request("POST", path, {"name": name, "color": color})
                created.append(name)
        return created

    def create_pr(
        self, repo: str, head: str, base: str, title: str, body: str, labels: list[str]
    ) -> dict[str, Any]:
        """Open a PR from an `ai/*` branch, then label it."""
        _check_repo(repo)
        if not head.startswith("ai/") or head == "ai/":
            raise GitHubError(f"refusing PR from non-ai/ branch: {head!r}")
        if not base.strip():
            raise GitHubError("PR base branch is empty")
        pr = self._request(
            "POST",
            f"/repos/{repo}/pulls",
            {"title": title, "head": head, "base": base, "body": body},
        )
        if not isinstance(pr, dict) or not isinstance(pr.get("number"), int):
            raise GitHubError("unexpected create-PR response")
        if labels:
            self._request("POST", f"/repos/{repo}/issues/{pr['number']}/labels", {"labels": labels})
        return pr

    def find_open_pr(self, repo: str, fingerprint: str) -> dict[str, Any] | None:
        """The open PR from an `ai/*` branch whose body carries `fingerprint`'s marker."""
        path = f"/repos/{_check_repo(repo)}/pulls"
        try:
            wanted = marker(fingerprint)
        except ValueError as e:
            raise GitHubError(str(e)) from None
        for pr in self._paginate(path, {"state": "open"}):
            head = pr.get("head")
            ref = head.get("ref") if isinstance(head, dict) else None
            body = pr.get("body")
            if (
                pr.get("state") == "open"
                and isinstance(ref, str)
                and ref.startswith("ai/")
                and isinstance(body, str)
                and wanted in body.splitlines()
            ):
                return pr
        return None
