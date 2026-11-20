from grafter.engines.base import AgentEngine, RunResult, Task

__all__ = ["AgentEngine", "RunResult", "Task", "get_engine"]

ENGINES = ("noop", "openhands")


def get_engine(name: str) -> AgentEngine:
    """The engine configured as `engine:`. Raises NotImplementedError for engines not built
    yet and ValueError for unknown names."""
    if name == "noop":
        from grafter.engines.noop import NoopEngine

        return NoopEngine()
    if name == "openhands":
        raise NotImplementedError("engine 'openhands' lands in 1C3")
    raise ValueError(f"unknown engine: {name!r} (known: {', '.join(ENGINES)})")
