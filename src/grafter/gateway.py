"""Client for the LiteLLM gateway (D15, D16, D18). Standard library only.

Grafter holds the gateway's master key. Each run gets its own short-lived virtual key,
limited to the models it needs and capped in USD. Sandboxes only ever see that key.
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

_KEY_RE = re.compile(r"sk-[A-Za-z0-9_-]{4,}")


@dataclass(frozen=True)
class Completion:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class GatewayError(RuntimeError):
    """The gateway is unreachable or rejected a request."""


def redact(key: str) -> str:
    return key[:4] + "***" if key else ""


def _scrub(text: str) -> str:
    return _KEY_RE.sub(lambda m: redact(m.group(0)), text)


class GatewayClient:
    def __init__(self, url: str, master_key: str, timeout: float = 30.0) -> None:
        if not url.startswith(("http://", "https://")):
            raise GatewayError(f"gateway url must be http(s): {url!r}")
        self.url = url.rstrip("/")
        self._master_key = master_key
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"GatewayClient(url={self.url!r}, master_key={redact(self._master_key)!r})"

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None, key: str | None = None
    ) -> Any:
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = _scrub(e.read()[:300].decode(errors="replace"))
            raise GatewayError(f"{method} {path} -> HTTP {e.code}: {detail}") from None
        except (OSError, http.client.HTTPException) as e:  # URLError, timeouts, dropped conns
            reason = getattr(e, "reason", e)
            raise GatewayError(f"gateway unreachable at {self.url}: {reason}") from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw.decode(errors="replace")

    def health(self) -> None:
        """Raise GatewayError unless the gateway answers its liveness probe."""
        self._request("GET", "/health/liveliness")

    def create_run_key(
        self, run_id: str, models: Iterable[str], max_budget_usd: float, duration: str = "1d"
    ) -> str:
        """Create a virtual key for one run: only `models`, at most `max_budget_usd`,
        expiring (and its budget resetting) after `duration`."""
        body = {
            "models": list(models),
            "max_budget": max_budget_usd,
            "budget_duration": duration,
            "duration": duration,
            "key_alias": f"grafter-{run_id}",
            "metadata": {"grafter_run_id": run_id},
        }
        resp = self._request("POST", "/key/generate", body, key=self._master_key)
        key = resp.get("key") if isinstance(resp, dict) else None
        if not isinstance(key, str) or not key:
            raise GatewayError("gateway returned no key")
        return key

    def delete_key(self, key: str) -> None:
        self._request("POST", "/key/delete", {"keys": [key]}, key=self._master_key)

    def chat(self, key: str, model: str, prompt: str, max_tokens: int = 64) -> str:
        return self.complete(key, model, [{"role": "user", "content": prompt}], max_tokens).text

    def complete(
        self, key: str, model: str, messages: list[dict[str, str]], max_tokens: int
    ) -> Completion:
        """One chat completion with a run key; returns the text and its token usage."""
        body = {"model": model, "messages": messages, "max_tokens": max_tokens}
        resp = self._request("POST", "/v1/chat/completions", body, key=key)
        try:
            content = resp["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise GatewayError("unexpected chat response shape") from None
        usage = resp.get("usage") or {}
        return Completion(
            text=str(content),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
        )

    def key_spend(self, key: str) -> float:
        """USD the gateway has booked against `key`: the authoritative cost (D16)."""
        resp = self._request("GET", "/key/info", key=key)
        try:
            spend = resp["info"]["spend"]
            return float(spend)
        except (KeyError, TypeError, ValueError):
            raise GatewayError("unexpected key info response: no spend") from None
