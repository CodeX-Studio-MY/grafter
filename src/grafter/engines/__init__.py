from __future__ import annotations

import importlib.util
from typing import TYPE_CHECKING

from grafter.engines.base import AgentEngine, RunResult, Task

if TYPE_CHECKING:
    from grafter.config import Config

__all__ = ["AgentEngine", "RunResult", "Task", "get_engine"]

ENGINES = ("noop", "openhands")
MASTER_KEY = "LITELLM_MASTER_KEY"


def get_engine(name: str, cfg: Config | None = None) -> AgentEngine:
    """The engine configured as `engine:`. Raises ValueError for unknown names (or when an
    engine needs `cfg` and gets none), `SecretError` when the gateway master key is missing,
    and ImportError when `grafter[openhands]` is not installed."""
    if name == "noop":
        from grafter.engines.noop import NoopEngine

        return NoopEngine()
    if name == "openhands":
        if cfg is None:
            raise ValueError("engine 'openhands' needs the config")
        if importlib.util.find_spec("openhands") is None:
            raise ImportError("engine 'openhands' needs `pip install grafter[openhands]`")
        from grafter.credentials import load_secret
        from grafter.engines.openhands import OpenHandsEngine
        from grafter.engines.openhands_sdk import sandbox_factory
        from grafter.gateway import GatewayClient

        master = load_secret(MASTER_KEY)
        gateway = GatewayClient(cfg.llm_gateway_url, master)
        return OpenHandsEngine(cfg, gateway, sandbox_factory(cfg.openhands))
    raise ValueError(f"unknown engine: {name!r} (known: {', '.join(ENGINES)})")
